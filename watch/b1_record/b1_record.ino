/*
 * Megotchi watch — B1: touch-to-record.
 *
 * Hold a finger anywhere on the screen → high beep → it records the mics
 * (16 kHz mono) into PSRAM → release → low beep → the WAV is hex-dumped
 * over USB serial for the Mac to capture, play back and run through STT.
 *
 * Codec plumbing is lifted from Waveshare's 08_ES8311 demo (pins, clocks,
 * volume); touch from their 06_LVGL example (FT3168 via Arduino_DriveBus).
 * No display, no WiFi yet — B2 adds the network, B3 adds playback of the
 * server reply, C adds the face.
 */
#include "Wire.h"
#include "ESP_I2S.h"
#include "Arduino_DriveBus_Library.h"
#include "pin_config.h"
#include "esp_check.h"
#include "es8311.h"
#include "es7210_mini.h"
#include <math.h>

#define SAMPLE_RATE    16000
#define VOICE_VOLUME   60                       // ES8311 dB curve: 60 ≈ polite
#define MIC_GAIN       (es8311_mic_gain_t)(4)   // 0-7
#define MAX_SECONDS    15
#define CHUNK_BYTES    4096                     // stereo read chunk (~64 ms)
#define I2C_NUM        0
#define PA_PIN         46                       // speaker amp enable

I2SClass i2s;

// ---- touch (FT3168, same construction as vendor 06_LVGL example) ----
std::shared_ptr<Arduino_IIC_DriveBus> IIC_Bus =
    std::make_shared<Arduino_HWIIC>(IIC_SDA, IIC_SCL, &Wire);
void Arduino_IIC_Touch_Interrupt(void);
std::unique_ptr<Arduino_IIC> FT3168(new Arduino_FT3x68(
    IIC_Bus, FT3168_DEVICE_ADDRESS, DRIVEBUS_DEFAULT_VALUE, TP_INT,
    Arduino_IIC_Touch_Interrupt));
void Arduino_IIC_Touch_Interrupt(void) { FT3168->IIC_Interrupt_Flag = true; }

int16_t *rec_buf = nullptr;                     // raw stereo, PSRAM
static uint8_t chunk[CHUNK_BYTES];

esp_err_t codec_init(void) {
  es8311_handle_t es = es8311_create(I2C_NUM, ES8311_ADDRRES_0);
  ESP_RETURN_ON_FALSE(es, ESP_FAIL, "b1", "es8311 create failed");
  const es8311_clock_config_t clk = {
      .mclk_inverted = false,
      .sclk_inverted = false,
      .mclk_from_mclk_pin = true,
      .mclk_frequency = SAMPLE_RATE * 256,
      .sample_frequency = SAMPLE_RATE};
  ESP_ERROR_CHECK(es8311_init(es, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16));
  ESP_RETURN_ON_ERROR(es8311_sample_frequency_config(
      es, clk.mclk_frequency, clk.sample_frequency), "b1", "sample freq");
  ESP_RETURN_ON_ERROR(es8311_microphone_config(es, false), "b1", "mic cfg");
  ESP_RETURN_ON_ERROR(es8311_voice_volume_set(es, VOICE_VOLUME, NULL), "b1", "volume");
  ESP_RETURN_ON_ERROR(es8311_microphone_gain_set(es, MIC_GAIN), "b1", "mic gain");
  return ESP_OK;
}

bool touched(void) {
  int32_t fingers = FT3168->IIC_Read_Device_Value(
      FT3168->Arduino_IIC_Touch::Value_Information::TOUCH_FINGER_NUMBER);
  return fingers > 0;
}

void beep(int freq, int ms) {
  static int16_t b[256];                        // stereo interleaved
  const int total = SAMPLE_RATE * ms / 1000;
  int idx = 0;
  for (int i = 0; i < total; i++) {
    int16_t s = (int16_t)(5000.0f * sinf(2.0f * PI * freq * i / SAMPLE_RATE));
    b[idx++] = s;                               // L
    b[idx++] = s;                               // R
    if (idx == 256) { i2s.write((uint8_t *)b, sizeof(b)); idx = 0; }
  }
  if (idx) i2s.write((uint8_t *)b, idx * 2);
}

void wav_header(uint8_t *h, uint32_t data_bytes) {
  uint32_t byte_rate = SAMPLE_RATE * 2;         // mono s16
  uint32_t riff = 36 + data_bytes;
  memcpy(h, "RIFF", 4);        memcpy(h + 4, &riff, 4);
  memcpy(h + 8, "WAVEfmt ", 8);
  uint32_t fmt_len = 16;       memcpy(h + 16, &fmt_len, 4);
  uint16_t pcm = 1, ch = 1, bits = 16, align = 2;
  memcpy(h + 20, &pcm, 2);     memcpy(h + 22, &ch, 2);
  uint32_t sr = SAMPLE_RATE;   memcpy(h + 24, &sr, 4);
  memcpy(h + 28, &byte_rate, 4);
  memcpy(h + 32, &align, 2);   memcpy(h + 34, &bits, 2);
  memcpy(h + 36, "data", 4);   memcpy(h + 40, &data_bytes, 4);
}

void print_hex(const uint8_t *d, size_t n) {
  static const char *H = "0123456789abcdef";
  char line[129];
  size_t li = 0;
  for (size_t i = 0; i < n; i++) {
    line[li++] = H[d[i] >> 4];
    line[li++] = H[d[i] & 15];
    if (li == 128) { line[128] = 0; Serial.println(line); li = 0; }
  }
  if (li) { line[li] = 0; Serial.println(line); }
}

void dump_wav(size_t samples) {
  uint32_t data_bytes = samples * 2;
  uint8_t hdr[44];
  wav_header(hdr, data_bytes);
  Serial.printf("\n---WAV-BEGIN %u---\n", (unsigned)(44 + data_bytes));
  print_hex(hdr, 44);
  print_hex((const uint8_t *)rec_buf, data_bytes);
  Serial.println("---WAV-END---");
}

void setup() {
  Serial.begin(115200);
  rec_buf = (int16_t *)ps_malloc(SAMPLE_RATE * MAX_SECONDS * 2 * sizeof(int16_t));
  if (!rec_buf) { Serial.println("[b1] PSRAM alloc failed!"); while (1) delay(1000); }
  pinMode(TP_RESET, OUTPUT);                    // wake the touch controller
  digitalWrite(TP_RESET, LOW); delay(10);
  digitalWrite(TP_RESET, HIGH); delay(80);

  i2s.setPins(41, 45, 40, 42, 16);              // same as vendor demo
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT,
                 I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)) {
    Serial.println("[b1] I2S init failed!");
    while (1) delay(1000);
  }
  Wire.begin(IIC_SDA, IIC_SCL);
  pinMode(PA_PIN, OUTPUT);
  digitalWrite(PA_PIN, HIGH);
  if (codec_init() != ESP_OK) Serial.println("[b1] codec init failed!");
  if (es7210_mini_init())
    Serial.printf("[b1] ES7210 mic ADC up at 0x%02X\n", es7210_addr);
  else
    Serial.println("[b1] ES7210 mic ADC init FAILED — recordings will be silent");
  while (FT3168->begin() == false) {
    Serial.println("[b1] FT3168 touch init failed, retrying…");
    delay(1000);
  }
  beep(660, 60); beep(880, 60);                 // ready jingle
  Serial.println("[b1] ready — hold the screen and speak, release to send");
}

void loop() {
  if (!touched()) { delay(30); return; }

  beep(880, 80);                                // record start
  const size_t max_raw = SAMPLE_RATE * MAX_SECONDS * 2;   // stereo samples
  size_t raw = 0;
  while (touched() && raw < max_raw) {
    size_t br = i2s.readBytes((char *)chunk, CHUNK_BYTES);
    size_t take = br / 2;
    if (raw + take > max_raw) take = max_raw - raw;
    memcpy(rec_buf + raw, chunk, take * 2);     // keep BOTH channels
    raw += take;
  }
  beep(440, 80);                                // record end

  // The mic lives on only one I2S slot (board-dependent) — pick the live one.
  int64_t e_l = 0, e_r = 0;
  for (size_t i = 0; i + 1 < raw; i += 2) {
    e_l += abs(rec_buf[i]);
    e_r += abs(rec_buf[i + 1]);
  }
  const int off = (e_r > e_l) ? 1 : 0;
  size_t mono = 0;
  for (size_t i = off; i < raw; i += 2)         // in-place extract (fwd-safe)
    rec_buf[mono++] = rec_buf[i];
  Serial.printf("[b1] recorded %.1fs — using %s channel (L=%lld R=%lld)\n",
                mono / (float)SAMPLE_RATE, off ? "RIGHT" : "LEFT",
                (long long)e_l, (long long)e_r);
  if (mono > SAMPLE_RATE / 4) dump_wav(mono);   // ignore sub-¼s taps
  else Serial.println("[b1] too short, ignored");

  while (touched()) delay(30);                  // wait for full release
}
