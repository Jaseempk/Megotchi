/*
 * Megotchi watch — B2: standalone WiFi turn (no Mac in the loop).
 *
 * Hold screen → beep → record (ES7210 mics, 16 kHz mono) → release → beep →
 * multipart POST to /api/talk_device over HTTPS → reply text/expression in
 * headers printed to serial; reply PCM audio is received and counted but not
 * yet played (that's B3, ~40 lines).
 *
 * Beep language (works without the serial cable):
 *   660+880  boot ready       880 record start    440 record end
 *   880+1100 turn OK          330+330 network/server error
 *
 * NOTE: TLS uses setInsecure() (no cert validation) — fine for dev, must be
 * swapped for a CA bundle before real use.
 */
#include "Wire.h"
#include "ESP_I2S.h"
#include "WiFi.h"
#include "WiFiClientSecure.h"
#include "HTTPClient.h"
#include "Arduino_DriveBus_Library.h"
#include "pin_config.h"                 // defines XPOWERS_CHIP_AXP2101
#include "XPowersLib.h"
#include "esp_check.h"
#include "es8311.h"
#include "es7210_mini.h"
#include "face.h"
#include "secrets.h"
#include <math.h>
#include <time.h>

#define SAMPLE_RATE    16000
#define VOICE_VOLUME   72
#define MAX_SECONDS    15
#define CHUNK_BYTES    4096
#define I2C_NUM        0
#define PA_PIN         46
#define BOUND          "MegotchiWatchBoundary7439"

// hands-free follow-up listening (VAD) — thresholds are calibration targets
#define FOLLOW_WAIT_MS 6000    // reply-then-silence = conversation over
#define VAD_ON_RMS     900     // speech-start RMS
#define VAD_OFF_RMS    500     // hysteresis floor
#define VAD_ON_CHUNKS  2       // ≥130 ms of voice confirms start
#define VAD_OFF_CHUNKS 12      // ~770 ms of quiet = kid finished

I2SClass i2s;

std::shared_ptr<Arduino_IIC_DriveBus> IIC_Bus =
    std::make_shared<Arduino_HWIIC>(IIC_SDA, IIC_SCL, &Wire);
void Arduino_IIC_Touch_Interrupt(void);
std::unique_ptr<Arduino_IIC> FT3168(new Arduino_FT3x68(
    IIC_Bus, FT3168_DEVICE_ADDRESS, DRIVEBUS_DEFAULT_VALUE, TP_INT,
    Arduino_IIC_Touch_Interrupt));
void Arduino_IIC_Touch_Interrupt(void) { FT3168->IIC_Interrupt_Flag = true; }
XPowersPMU pmu;                                 // AXP2101 battery/power chip

int16_t *rec_buf = nullptr;                     // raw stereo → extracted mono
uint8_t *body = nullptr;                        // assembled multipart request
static uint8_t chunk[CHUNK_BYTES];

// ---- codec init (identical to B1, which is proven) ----
esp_err_t codec_init(void) {
  es8311_handle_t es = es8311_create(I2C_NUM, ES8311_ADDRRES_0);
  ESP_RETURN_ON_FALSE(es, ESP_FAIL, "b2", "es8311 create failed");
  const es8311_clock_config_t clk = {
      .mclk_inverted = false, .sclk_inverted = false,
      .mclk_from_mclk_pin = true,
      .mclk_frequency = SAMPLE_RATE * 256, .sample_frequency = SAMPLE_RATE};
  ESP_ERROR_CHECK(es8311_init(es, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16));
  ESP_RETURN_ON_ERROR(es8311_sample_frequency_config(
      es, clk.mclk_frequency, clk.sample_frequency), "b2", "sample freq");
  ESP_RETURN_ON_ERROR(es8311_voice_volume_set(es, VOICE_VOLUME, NULL), "b2", "volume");
  return ESP_OK;
}

bool touched(void) {
  return FT3168->IIC_Read_Device_Value(
      FT3168->Arduino_IIC_Touch::Value_Information::TOUCH_FINGER_NUMBER) > 0;
}

void beep(int freq, int ms) {
  static int16_t b[256];
  const int total = SAMPLE_RATE * ms / 1000;
  int idx = 0;
  for (int i = 0; i < total; i++) {
    int16_t s = (int16_t)(5000.0f * sinf(2.0f * PI * freq * i / SAMPLE_RATE));
    b[idx++] = s; b[idx++] = s;
    if (idx == 256) { i2s.write((uint8_t *)b, sizeof(b)); idx = 0; }
  }
  if (idx) i2s.write((uint8_t *)b, idx * 2);
}
void err_beep(void) { beep(330, 120); delay(40); beep(330, 120); }

void wav_header(uint8_t *h, uint32_t data_bytes) {
  uint32_t byte_rate = SAMPLE_RATE * 2, riff = 36 + data_bytes, fmt_len = 16;
  uint32_t sr = SAMPLE_RATE;
  uint16_t pcm = 1, ch = 1, bits = 16, align = 2;
  memcpy(h, "RIFF", 4);         memcpy(h + 4, &riff, 4);
  memcpy(h + 8, "WAVEfmt ", 8); memcpy(h + 16, &fmt_len, 4);
  memcpy(h + 20, &pcm, 2);      memcpy(h + 22, &ch, 2);
  memcpy(h + 24, &sr, 4);       memcpy(h + 28, &byte_rate, 4);
  memcpy(h + 32, &align, 2);    memcpy(h + 34, &bits, 2);
  memcpy(h + 36, "data", 4);    memcpy(h + 40, &data_bytes, 4);
}

String pct_decode(const String &s) {
  String o;
  for (unsigned i = 0; i < s.length(); i++) {
    if (s[i] == '%' && i + 2 < s.length()) {
      o += (char)strtol(s.substring(i + 1, i + 3).c_str(), nullptr, 16);
      i += 2;
    } else o += s[i];
  }
  return o;
}

bool wifi_connect(void) {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                         // latency > power for now

  // 1) Credentials a previous firmware saved in NVS may still be on the chip
  Serial.println("[b2] trying wifi saved on the chip…");
  WiFi.begin();
  for (int i = 0; i < 24 && WiFi.status() != WL_CONNECTED; i++) delay(250);

  // 2) Fall back to secrets.h (skip if still placeholder)
  if (WiFi.status() != WL_CONNECTED && strcmp(WIFI_SSID, "YOUR_WIFI_NAME") != 0) {
    Serial.printf("[b2] joining %s…\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(250);
  }
  if (WiFi.status() != WL_CONNECTED) { Serial.println("[b2] wifi FAILED"); return false; }
  Serial.printf("[b2] wifi ok: %s (%s, %d dBm)\n", WiFi.SSID().c_str(),
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

int local_hour(void) {
  struct tm t;
  if (!getLocalTime(&t, 800)) return -1;        // NTP not synced yet
  return t.tm_hour;
}

// Extract the louder mic channel in place; returns mono sample count.
static size_t pick_mono(size_t raw) {
  int64_t e_l = 0, e_r = 0;
  for (size_t i = 0; i + 1 < raw; i += 2) { e_l += abs(rec_buf[i]); e_r += abs(rec_buf[i + 1]); }
  const int off = (e_r > e_l) ? 1 : 0;
  size_t mono = 0;
  for (size_t i = off; i < raw; i += 2) rec_buf[mono++] = rec_buf[i];
  return mono;
}

// Download reply PCM into PSRAM, then speak it (shared by talk + greet).
// Returns bytes of audio actually played (0/small = nothing worth hearing).
size_t play_reply(HTTPClient &http, uint32_t t0) {
  WiFiClient *stream = http.getStreamPtr();
  const int len = http.getSize();
  const size_t cap = SAMPLE_RATE * MAX_SECONDS * 2 + 2048;
  size_t got = 0;
  uint32_t last_data = millis();
  while (http.connected() && (len < 0 || got < (size_t)len) && got < cap
         && millis() - last_data < 20000) {
    size_t avail = stream->available();
    if (!avail) { delay(5); continue; }
    if (avail > CHUNK_BYTES) avail = CHUNK_BYTES;
    const int r = stream->read(body + got, avail);
    if (r > 0) { got += r; last_data = millis(); }
  }
  const float dt = (millis() - t0) / 1000.0f;
  Serial.printf("[b4] 200 in %.1fs — \"%s\" (%s), speaking %.1fs…\n",
                dt, pct_decode(http.header("X-Reply")).c_str(),
                http.header("X-Expression").c_str(), got / 32000.0f);
  http.end();

  // ---- lip-sync, done right: we hold the WHOLE reply, so precompute an
  // articulation envelope (20 ms frames, peak-based, per-reply normalized,
  // noise-gated so the mouth truly CLOSES between words), then drive the
  // mouth off the playback wall-clock — not the write position, which runs
  // ahead of the speaker by the DMA depth.
  const int16_t *m = (const int16_t *)body;
  const size_t frames = got / 2;
  if (frames < 512) { face_state(FACE_IDLE); return got; }

  #define ENV_FRAME 320                          // 20 ms @ 16 kHz
  #define MOUTH_LEAD_MS 40                       // visual lead over DMA
  static uint8_t env[1024];                      // up to ~20 s of reply
  const size_t ne = min((size_t)1024, frames / ENV_FRAME + 1);
  float peak_all = 1.0f;
  for (size_t e = 0; e < ne; e++) {              // pass 1: frame peaks
    int32_t pk = 0;
    const size_t base = e * ENV_FRAME;
    const size_t lim = min(base + ENV_FRAME, frames);
    for (size_t k = base; k < lim; k++) {
      const int32_t v = abs(m[k]);
      if (v > pk) pk = v;
    }
    env[e] = (uint8_t)min((int32_t)255, pk >> 7);  // rough 0..255
    if (pk > peak_all) peak_all = (float)pk;
  }
  for (size_t e = 0; e < ne; e++) {              // pass 2: normalize + shape
    float v = (env[e] * 128.0f) / peak_all;      // undo >>7, scale to peak
    v = powf(fminf(1.0f, v), 0.62f);             // syllables pop open
    if (v < 0.13f) v = 0;                        // gate: silence = closed
    env[e] = (uint8_t)(v * 255.0f);
  }

  face_expr(pct_decode(http.header("X-Expression")).c_str());
  face_state(FACE_SPEAK);
  static int16_t st[2048];                       // 512 frames stereo (32 ms)
  const uint32_t t_play = millis();
  size_t i = 0;
  uint32_t last_draw = 0;
  while (i < frames) {
    const size_t blk = min((size_t)512, frames - i);
    size_t o = 0;
    for (size_t k = 0; k < blk; k++) { st[o++] = m[i + k]; st[o++] = m[i + k]; }
    i2s.write((uint8_t *)st, o * 2);             // blocks → paces the loop
    i += blk;
    if (millis() - last_draw >= 33) {            // ~30 fps, wall-clock synced
      last_draw = millis();
      const int32_t played = ((int32_t)(millis() - t_play) - MOUTH_LEAD_MS) * 16;
      int idx = played / (ENV_FRAME);
      if (idx < 0) idx = 0;
      if (idx >= (int)ne) idx = ne - 1;
      face_mouth(env[idx] / 255.0f);
    }
  }
  // DMA tail is still sounding — keep the mouth honest to the end
  const uint32_t t_end = t_play + frames / 16 + 80;
  while (millis() < t_end) {
    const int32_t played = ((int32_t)(millis() - t_play) - MOUTH_LEAD_MS) * 16;
    int idx = played / ENV_FRAME;
    if (idx < 0) idx = 0;
    if (idx >= (int)ne) idx = ne - 1;
    face_mouth(env[idx] / 255.0f);
    delay(25);
  }
  face_state(FACE_IDLE);
  face_expr_hold(3500);   // expression lingers, then eases home on a blink
  return got;
}

// Hands-free follow-up: after Megotchi speaks, the mic opens briefly (ring
// showing — never silently). Returns mono samples captured (0 = nothing).
size_t vad_capture(void) {
  face_state(FACE_LISTEN);
  i2s.readBytes((char *)chunk, CHUNK_BYTES);    // drain our own speech tail
  i2s.readBytes((char *)chunk, CHUNK_BYTES);

  static uint8_t prevc[CHUNK_BYTES];            // pre-roll: chunk before speech
  size_t prev_br = 0;
  const size_t max_raw = SAMPLE_RATE * MAX_SECONDS * 2;
  size_t raw = 0;
  int on = 0, off = 0, logged = 0;
  bool talking = false;
  const uint32_t t0 = millis();

  while (true) {
    const size_t br = i2s.readBytes((char *)chunk, CHUNK_BYTES);
    if (!br) break;
    const int16_t *s = (const int16_t *)chunk;
    const size_t n16 = br / 2;
    int64_t sl = 0, sr = 0;
    for (size_t i = 0; i + 1 < n16; i += 2) {
      sl += (int64_t)s[i] * s[i];
      sr += (int64_t)s[i + 1] * s[i + 1];
    }
    const int rms = (int)sqrtf(
        (float)((sl > sr ? sl : sr) / (int64_t)(n16 / 2 ? n16 / 2 : 1)));
    if (logged++ % 8 == 0 && logged < 80)       // calibration breadcrumbs
      Serial.printf("[vad] rms=%d%s\n", rms, talking ? " talking" : "");

    if (!talking) {
      if (rms >= VAD_ON_RMS) {
        if (on == 0 && prev_br) {               // prepend pre-roll chunk
          size_t take = prev_br / 2;
          if (take > max_raw) take = max_raw;
          memcpy(rec_buf, prevc, take * 2);
          raw = take;
        }
        size_t take = n16;
        if (raw + take > max_raw) take = max_raw - raw;
        memcpy(rec_buf + raw, chunk, take * 2);
        raw += take;
        if (++on >= VAD_ON_CHUNKS) { talking = true; off = 0; }
      } else {
        on = 0; raw = 0;
        memcpy(prevc, chunk, br);
        prev_br = br;
        if (millis() - t0 > FOLLOW_WAIT_MS) { face_state(FACE_IDLE); return 0; }
      }
    } else {
      size_t take = n16;
      if (raw + take > max_raw) take = max_raw - raw;
      memcpy(rec_buf + raw, chunk, take * 2);
      raw += take;
      if (rms < VAD_OFF_RMS) { if (++off >= VAD_OFF_CHUNKS) break; }
      else off = 0;
      if (raw >= max_raw) break;
    }
    face_listen_tick();
  }
  const size_t mono = pick_mono(raw);
  Serial.printf("[vad] captured %.1fs\n", mono / (float)SAMPLE_RATE);
  return mono;
}

// Hands-free conversation: keep listening after each reply until silence.
// A loop, deliberately not recursion; capped so ambient noise ≠ endless spend.
void followup_conversation(bool ok) {
  int rounds = 0;
  while (ok && rounds++ < 10) {
    const size_t m = vad_capture();
    if (m < SAMPLE_RATE / 4) break;             // silence/too short = done
    face_state(FACE_THINK);
    ok = send_turn(m);
  }
  face_state(FACE_IDLE);
}

// Short tap: Megotchi speaks first (time-of-day hello + memory callbacks).
bool do_greet(void) {
  if (!wifi_connect()) { err_beep(); return false; }
  const uint32_t t0 = millis();
  WiFiClientSecure client;
  client.setInsecure();                         // DEV ONLY
  client.setTimeout(30000);
  HTTPClient http;
  if (!http.begin(client, GREET_URL)) { err_beep(); return false; }
  http.setTimeout(45000);
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  const char *keys[] = {"X-Reply", "X-Expression", "X-Note"};
  http.collectHeaders(keys, 3);
  char form[128];
  snprintf(form, sizeof(form), "child=%s&hour=%d&fmt=pcm16",
           CHILD_NAME, local_hour());
  Serial.printf("[b4] tap → greet (%s)\n", form);
  face_state(FACE_THINK);
  const int code = http.POST((uint8_t *)form, strlen(form));
  if (code != 200) {
    Serial.printf("[b4] greet HTTP %d\n", code);
    http.end();
    face_state(FACE_IDLE);
    if (code < 0) err_beep();                   // network fail; 429 stays quiet
    return false;
  }
  const size_t played = play_reply(http, t0);   // gap rule → empty body
  face_state(FACE_IDLE);
  return played > 1024;
}

bool send_turn(size_t samples) {
  const uint32_t data_bytes = samples * 2;

  // ---- assemble multipart body (same shape the Mac's curl sent in B1) ----
  size_t n = 0;
  n += sprintf((char *)body + n,
               "--%s\r\nContent-Disposition: form-data; name=\"child\"\r\n\r\n"
               "%s\r\n", BOUND, CHILD_NAME);
  n += sprintf((char *)body + n,
               "--%s\r\nContent-Disposition: form-data; name=\"secs\"\r\n\r\n"
               "%.1f\r\n", BOUND, samples / (float)SAMPLE_RATE);
  n += sprintf((char *)body + n,
               "--%s\r\nContent-Disposition: form-data; name=\"audio\"; "
               "filename=\"rec.wav\"\r\nContent-Type: audio/wav\r\n\r\n", BOUND);
  wav_header(body + n, data_bytes); n += 44;
  memcpy(body + n, rec_buf, data_bytes); n += data_bytes;
  n += sprintf((char *)body + n, "\r\n--%s--\r\n", BOUND);

  // ---- HTTPS POST, retried once on connection-level failure ----
  // (body buffer doubles as the download buffer inside play_reply — safe,
  //  because by then the request has been fully sent)
  for (int attempt = 0; attempt < 2; attempt++) {
    if (!wifi_connect()) break;
    const uint32_t t0 = millis();
    WiFiClientSecure client;
    client.setInsecure();                       // DEV ONLY — see header note
    client.setTimeout(30000);
    HTTPClient http;
    if (!http.begin(client, SERVER_URL)) break;
    http.setTimeout(45000);
    http.addHeader("Content-Type", "multipart/form-data; boundary=" BOUND);
    const char *keys[] = {"X-Reply", "X-Expression", "X-Note"};
    http.collectHeaders(keys, 3);

    Serial.printf("[b4] uploading %u KB%s…\n", (unsigned)(n / 1024),
                  attempt ? " (retry)" : "");
    const int code = http.POST(body, n);
    if (code == 200) { play_reply(http, t0); return true; }
    Serial.printf("[b4] HTTP %d  note: %s\n", code,
                  pct_decode(http.header("X-Note")).c_str());
    http.end();
    if (code >= 0) break;                       // server answered: don't retry
    WiFi.disconnect();                          // connection died: rejoin + retry
    delay(400);
  }
  err_beep();
  return false;
}

void setup() {
  Serial.begin(115200);
  if (!face_begin()) Serial.println("[c1] display init failed");
  rec_buf = (int16_t *)ps_malloc(SAMPLE_RATE * MAX_SECONDS * 2 * sizeof(int16_t));
  body = (uint8_t *)ps_malloc(SAMPLE_RATE * MAX_SECONDS * 2 + 2048);
  if (!rec_buf || !body) { Serial.println("[b2] PSRAM alloc failed!"); while (1) delay(1000); }

  pinMode(TP_RESET, OUTPUT);
  digitalWrite(TP_RESET, LOW); delay(10);
  digitalWrite(TP_RESET, HIGH); delay(80);

  i2s.setPins(41, 45, 40, 42, 16);
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                 I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    Serial.println("[b2] I2S init failed!"); while (1) delay(1000);
  }
  Wire.begin(IIC_SDA, IIC_SCL);
  if (pmu.begin(Wire, AXP2101_SLAVE_ADDRESS, IIC_SDA, IIC_SCL)) {
    pmu.setChargingLedMode(XPOWERS_CHG_LED_CTRL_CHG);   // LED shows charging
    Serial.printf("[b2] battery: %s | %d%% | %.2fV | %s\n",
                  pmu.isBatteryConnect() ? "connected" : "NOT CONNECTED",
                  pmu.getBatteryPercent(), pmu.getBattVoltage() / 1000.0f,
                  pmu.isCharging() ? "charging" : "not charging");
  } else {
    Serial.println("[b2] AXP2101 power chip not found");
  }
  pinMode(PA_PIN, OUTPUT);
  digitalWrite(PA_PIN, HIGH);
  if (codec_init() != ESP_OK) Serial.println("[b2] es8311 init failed!");
  if (es7210_mini_init())
    Serial.printf("[b2] ES7210 mic ADC up at 0x%02X\n", es7210_addr);
  else
    Serial.println("[b2] ES7210 init FAILED — mic will be silent");
  while (FT3168->begin() == false) { Serial.println("[b2] touch retry…"); delay(500); }

  wifi_connect();                               // join early; retried per turn
  configTime(TZ_OFFSET_H * 3600, 0, "pool.ntp.org");   // local hour for greetings
  beep(660, 60); beep(880, 60);
  Serial.println("[b4] ready — tap = hello, hold = talk");
}

void do_record_turn(void) {
  face_state(FACE_LISTEN);
  beep(880, 80);
  i2s.readBytes((char *)chunk, CHUNK_BYTES);    // drain stale DMA audio
  i2s.readBytes((char *)chunk, CHUNK_BYTES);    // (incl. our own speech echo)
  const size_t max_raw = SAMPLE_RATE * MAX_SECONDS * 2;
  size_t raw = 0;
  while (touched() && raw < max_raw) {
    size_t br = i2s.readBytes((char *)chunk, CHUNK_BYTES);
    size_t take = br / 2;
    if (raw + take > max_raw) take = max_raw - raw;
    memcpy(rec_buf + raw, chunk, take * 2);
    raw += take;
    face_listen_tick();                         // pulse the attention ring
  }
  beep(440, 80);
  face_state(FACE_THINK);

  const size_t mono = pick_mono(raw);
  Serial.printf("[b4] recorded %.1fs\n", mono / (float)SAMPLE_RATE);
  bool ok = false;
  if (mono > SAMPLE_RATE / 4) ok = send_turn(mono);
  else Serial.println("[b4] too short, ignored");
  followup_conversation(ok);                    // hands-free from here on
}

void idle_checks(void) {
  static uint32_t last_check = 0, last_warn = 0;
  if (millis() - last_check < 60000) return;
  last_check = millis();
  const int pct = pmu.getBatteryPercent();
  if (pct >= 0 && pct < 15 && !pmu.isCharging()
      && (last_warn == 0 || millis() - last_warn > 600000)) {
    last_warn = millis();
    Serial.printf("[b4] battery low: %d%%\n", pct);
    beep(523, 120); beep(392, 200);             // sad descending = feed me
  }
}

void loop() {
  idle_checks();
  face_idle_tick();                             // blinks + saccades
  if (!touched()) { delay(30); return; }

  const uint32_t t0 = millis();                 // tap vs hold: decide at 260 ms
  while (touched() && millis() - t0 < 260) delay(15);
  if (!touched()) {                             // short tap → Megotchi speaks first
    if (do_greet()) followup_conversation(true);  // …and the kid just answers
  } else {
    do_record_turn();                           // held → record until release
  }

  while (touched()) delay(30);                  // swallow holds through playback
}
