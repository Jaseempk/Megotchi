/*
 * face.h — Megotchi's face on the 410×502 AMOLED (CO5300 over QSPI).
 *
 * Same character as the landing page / web app: cream screen, big rounded
 * ink eyes with glints, blush cheeks, warm smile. Geometry is the SVG scaled
 * ~2.2×.
 *
 * Expressiveness techniques:
 *  - dynamic regions (eyes, mouth) render into PSRAM canvases and blit —
 *    flicker-free animation, ~5-8 ms per region update
 *  - blinks squash-and-stretch (lids close → eye widens slightly), with
 *    natural randomized timing and occasional double-blinks
 *  - idle saccades: eased glance to a random point, hold, ease back
 *  - THINK: eyes drift up + pondering dots; LISTEN: eyes widen + honey ring
 *  - SPEAK: mouth height tracks real audio RMS (fast attack, slow decay),
 *    quick 3-frame blinks so audio never starves
 */
#pragma once
#include "Arduino_GFX_Library.h"
#include "pin_config.h"

#define FACE_BRIGHTNESS 150   // 0-255; AMOLED, so lower also saves real battery

#define FC_CREAM RGB565(255, 253, 246)
#define FC_INK   RGB565(70, 53, 44)
#define FC_BLUSH RGB565(255, 185, 163)
#define FC_HONEY RGB565(255, 196, 94)
#define FC_SOFT  RGB565(122, 104, 92)

enum FaceState { FACE_IDLE, FACE_LISTEN, FACE_THINK, FACE_SPEAK };

// ---- panel ----
static Arduino_DataBus *f_bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
static Arduino_CO5300 *f_gfx = new Arduino_CO5300(
    f_bus, LCD_RESET, 0, LCD_WIDTH, LCD_HEIGHT, 22, 0, 0, 0);

// ---- dynamic-region canvases (draw off-screen, blit = no flicker) ----
#define EYE_X 90
#define EYE_Y 140
#define EYE_W 230
#define EYE_H 150
#define MTH_X 140
#define MTH_Y 266
#define MTH_W 130
#define MTH_H 92
#define DOT_X 278
#define DOT_Y 84
#define DOT_W 76
#define DOT_H 66
static Arduino_Canvas *cv_eyes  = new Arduino_Canvas(EYE_W, EYE_H, f_gfx, EYE_X, EYE_Y);
static Arduino_Canvas *cv_mouth = new Arduino_Canvas(MTH_W, MTH_H, f_gfx, MTH_X, MTH_Y);
static Arduino_Canvas *cv_dots  = new Arduino_Canvas(DOT_W, DOT_H, f_gfx, DOT_X, DOT_Y);

static FaceState f_state = FACE_IDLE;
static float f_eye_open = 1.0f;                 // 1 = open, ~0 = closed
static float f_eye_dx = 0, f_eye_dy = 0;        // saccade offset (px)
static float f_mouth = 0;                       // 0..1 open amount
static bool  f_wide = false, f_up = false;
static uint32_t f_next_blink = 2500, f_next_glance = 5000, f_glance_back = 0;
static float f_gl_tx = 0, f_gl_ty = 0;

// ---- expressions: modifiers consulted by every draw call ----
enum MouthStyle : uint8_t { M_SMILE, M_BIG, M_SOFT, M_O, M_FROWN };
struct Expr {
  float eye_h, eye_w;   // eye size scaling
  float lid;            // openness cap (0.6 = half-lidded)
  int dy_l, dy_r;       // per-eye vertical offset — asymmetry = head-tilt
  float cheek;          // blush scale, 0 = off
  uint8_t mouth;
};
static const struct { const char *name; Expr e; } EXPRS[] = {
    {"neutral",   {1.00f, 1.00f, 1.00f,  0, 0, 1.0f, M_SMILE}},
    {"happy",     {0.95f, 1.05f, 0.72f,  0, 0, 1.1f, M_BIG}},   // squinty smile-eyes
    {"excited",   {1.15f, 1.10f, 1.00f,  0, 0, 1.2f, M_BIG}},
    {"curious",   {1.05f, 1.05f, 1.00f, -7, 2, 1.0f, M_SOFT}},  // one eye raised
    {"gentle",    {0.90f, 1.00f, 0.60f,  0, 0, 0.9f, M_SOFT}},  // half-lidded
    {"surprised", {1.22f, 1.15f, 1.00f,  0, 0, 1.0f, M_O}},
    {"sad",       {0.85f, 1.00f, 0.80f,  6, 6, 0.0f, M_FROWN}}, // droop, no blush
};
static Expr f_ex = {1.00f, 1.00f, 1.00f, 0, 0, 1.0f, M_SMILE};
static uint32_t f_expr_until = 0;               // when to ease back to neutral

static void draw_eyes(void) {
  cv_eyes->fillRect(0, 0, EYE_W, EYE_H, FC_CREAM);
  const float open = fmaxf(0.10f, fminf(f_eye_open, f_ex.lid));
  const float w0 = (f_wide ? 50 : 46) * f_ex.eye_w;
  const float h0 = (f_wide ? 88 : 78) * f_ex.eye_h;
  const float h = h0 * open;
  const float w = w0 * (1.0f + (1.0f - open) * 0.18f);  // squash → widen
  const int dy = (f_up ? -9 : 0) + (int)f_eye_dy;
  for (int cx : {41, 189}) {                    // canvas-space eye centers
    const int edy = dy + ((cx == 41) ? f_ex.dy_l : f_ex.dy_r);
    const int x = cx + (int)f_eye_dx - (int)(w / 2);
    const int y = 75 + edy - (int)(h / 2);
    const int r = (int)(fminf(w, h) / 2);
    cv_eyes->fillRoundRect(x, y, (int)w, (int)h, r, FC_INK);
    if (open > 0.5f)                            // glint only when open
      cv_eyes->fillCircle(cx + (int)f_eye_dx - 9, 75 + edy - (int)(h * 0.28f),
                          8, FC_CREAM);
  }
  cv_eyes->flush();
}

static void draw_mouth(void) {
  cv_mouth->fillRect(0, 0, MTH_W, MTH_H, FC_CREAM);
  if (f_state == FACE_SPEAK && f_mouth > 0.04f) {
    // articulate: opens tall AND narrows (O-shape on loud syllables)
    const int ry = 4 + (int)(32 * f_mouth);
    const int rx = 26 - (int)(8 * f_mouth);
    cv_mouth->fillEllipse(205 - MTH_X, 306 - MTH_Y, rx, ry, FC_INK);
  } else if (f_state == FACE_SPEAK) {
    // closed between words — a short line, not the resting smile
    cv_mouth->fillRoundRect(205 - MTH_X - 16, 306 - MTH_Y - 3, 32, 7, 3, FC_INK);
  } else {
    switch (f_ex.mouth) {
      case M_BIG:
        cv_mouth->fillArc(205 - MTH_X, 282 - MTH_Y, 50, 41, 20, 160, FC_INK);
        break;
      case M_SOFT:
        cv_mouth->fillArc(205 - MTH_X, 286 - MTH_Y, 40, 34, 35, 145, FC_INK);
        break;
      case M_O:
        cv_mouth->fillEllipse(205 - MTH_X, 306 - MTH_Y, 12, 14, FC_INK);
        break;
      case M_FROWN:  // same geometry as the smile, flipped: upper arc segment
        cv_mouth->fillArc(205 - MTH_X, 336 - MTH_Y, 46, 38, 205, 335, FC_INK);
        break;
      default:
        cv_mouth->fillArc(205 - MTH_X, 284 - MTH_Y, 46, 38, 25, 155, FC_INK);
    }
  }
  cv_mouth->flush();
}

static void draw_cheeks(void) {
  f_gfx->fillRect(68, 276, 76, 50, FC_CREAM);
  f_gfx->fillRect(268, 276, 76, 50, FC_CREAM);
  if (f_ex.cheek > 0.05f) {
    const int rx = (int)(30 * f_ex.cheek), ry = (int)(19 * f_ex.cheek);
    f_gfx->fillEllipse(105, 300, rx, ry, FC_BLUSH);
    f_gfx->fillEllipse(305, 300, rx, ry, FC_BLUSH);
  }
}

static void draw_ring(bool on, int phase) {    // LISTEN attention ring
  f_gfx->fillArc(205, 245, 200, 168, 0, 360, FC_CREAM);
  if (on) {
    const int r1 = 178 + phase * 6;
    f_gfx->fillArc(205, 245, r1, r1 - 7, 0, 360, FC_HONEY);
  }
}

static void draw_dots_clear(void) {            // erase the THINK dots region
  cv_dots->fillRect(0, 0, DOT_W, DOT_H, FC_CREAM);
  cv_dots->flush();
}

// One animation frame of the pondering dots: pop-in entrance, then a gentle
// staggered bounce (typing-indicator rhythm). Runs on the face task core.
static void draw_think_frame(uint32_t frame) {
  static const int cx[3] = {14, 37, 60}, cy[3] = {48, 32, 17}, cr[3] = {5, 6, 8};
  const float ph = millis() * 0.0057f;          // ~1.1 s bounce period
  const float pop = fminf(1.0f, frame * 0.25f); // 4-frame pop-in
  cv_dots->fillRect(0, 0, DOT_W, DOT_H, FC_CREAM);
  for (int i = 0; i < 3; i++) {
    const float dy = sinf(ph - i * 0.55f) * 3.6f;
    const int r = (int)fmaxf(1.0f, cr[i] * pop);
    cv_dots->fillCircle(cx[i], cy[i] + (int)dy, r, FC_SOFT);
  }
  cv_dots->flush();
}

// ---- face animation task: owns THINK-state drawing on the other core, so
// the dots keep bouncing while the main core blocks inside the HTTPS call.
// ALL drawing (both cores) goes through f_mux — the QSPI bus is not
// re-entrant and two concurrent draws crash the chip.
static SemaphoreHandle_t f_mux = nullptr;
#define FACE_LOCK()   do { if (f_mux) xSemaphoreTake(f_mux, portMAX_DELAY); } while (0)
#define FACE_UNLOCK() do { if (f_mux) xSemaphoreGive(f_mux); } while (0)

static void face_task(void *arg) {
  uint32_t frame = 0;
  FaceState prev = FACE_IDLE;
  for (;;) {
    if (f_state == FACE_THINK) {
      if (prev != FACE_THINK) frame = 0;
      FACE_LOCK();
      if (f_state == FACE_THINK)                // re-check inside the lock
        draw_think_frame(frame++);
      FACE_UNLOCK();
      prev = FACE_THINK;
      vTaskDelay(pdMS_TO_TICKS(85));
    } else {
      prev = f_state;
      vTaskDelay(pdMS_TO_TICKS(40));
    }
  }
}

static void face_blink(bool quick = false) {
  static const float slow_seq[] = {0.6f, 0.22f, 0.08f, 0.3f, 0.7f, 1.0f};
  static const float fast_seq[] = {0.25f, 0.08f, 1.0f};
  const float *seq = quick ? fast_seq : slow_seq;
  const int n = quick ? 3 : 6;
  for (int i = 0; i < n; i++) {
    f_eye_open = seq[i];
    draw_eyes();
    delay(quick ? 10 : 16);
  }
  f_eye_open = 1.0f;
}

void face_state(FaceState s) {
  if (s == f_state) return;
  const FaceState prev = f_state;
  FACE_LOCK();                                  // excludes the face task
  f_state = s;
  f_wide = (s == FACE_LISTEN);
  f_up = (s == FACE_THINK);
  f_eye_dx = f_eye_dy = 0;
  f_eye_open = 1.0f;
  f_mouth = 0;
  if (prev == FACE_LISTEN || s == FACE_LISTEN) draw_ring(s == FACE_LISTEN, 0);
  if (prev == FACE_THINK) draw_dots_clear();    // leaving THINK: erase dots
  draw_eyes();
  draw_mouth();
  FACE_UNLOCK();
}

// Apply a named expression (unknown names fall back to neutral, so a new
// server-side expression can never break the firmware).
void face_expr(const char *name) {
  const Expr *e = &EXPRS[0].e;
  if (name)
    for (auto &x : EXPRS)
      if (strcasecmp(x.name, name) == 0) { e = &x.e; break; }
  FACE_LOCK();
  f_ex = *e;
  draw_cheeks();
  draw_eyes();
  draw_mouth();
  FACE_UNLOCK();
}

// Keep the current expression this long after the reply, then ease home.
void face_expr_hold(uint32_t ms) { f_expr_until = millis() + ms; }

// Idle life: blinks + eased saccades. Call often; cheap when nothing is due.
// Runs on the main core only; lock guards against a late THINK-task frame.
void face_idle_tick(void) {
  if (f_state != FACE_IDLE) return;
  FACE_LOCK();
  if (f_state != FACE_IDLE) { FACE_UNLOCK(); return; }
  const uint32_t now = millis();
  if (f_expr_until && now >= f_expr_until) {    // ease home: cut on the blink
    f_expr_until = 0;
    f_eye_open = 0.15f; draw_eyes(); delay(35);
    f_ex = EXPRS[0].e;                          // neutral
    f_eye_open = 1.0f;
    draw_cheeks(); draw_eyes(); draw_mouth();
  }
  if (f_glance_back && now >= f_glance_back) {  // ease back to center
    for (float k = 1.0f; k >= 0; k -= 0.34f) {
      const float e = k * k;                    // ease-in home
      f_eye_dx = f_gl_tx * e; f_eye_dy = f_gl_ty * e;
      draw_eyes();
      delay(14);
    }
    f_eye_dx = f_eye_dy = 0;
    f_glance_back = 0;
    draw_eyes();
  } else if (!f_glance_back && now >= f_next_glance) {
    f_gl_tx = (float)random(-8, 9);
    f_gl_ty = (float)random(-4, 5);
    for (float k = 0.34f; k <= 1.01f; k += 0.33f) {
      const float e = 1.0f - (1.0f - k) * (1.0f - k);   // ease-out
      f_eye_dx = f_gl_tx * e; f_eye_dy = f_gl_ty * e;
      draw_eyes();
      delay(14);
    }
    f_glance_back = now + random(600, 1500);
    f_next_glance = now + random(3500, 8000);
  }
  if (now >= f_next_blink) {
    face_blink();
    f_next_blink = millis() + random(2400, 5200);
    if (random(5) == 0) { delay(130); face_blink(); }   // double-blink
  }
  FACE_UNLOCK();
}

// LISTEN: pulse the attention ring (call from the record loop).
void face_listen_tick(void) {
  static uint32_t last = 0;
  static int ph = 0;
  if (millis() - last < 350) return;
  last = millis();
  ph = (ph + 1) % 3;
  FACE_LOCK();
  draw_ring(true, ph);
  FACE_UNLOCK();
}

// SPEAK: envelope-driven articulation — instant attack (syllables snap),
// two-frame decay, and true closure between words.
void face_mouth(float level) {
  if (level >= f_mouth) f_mouth = level;        // attack: instant
  else f_mouth = f_mouth * 0.42f + level * 0.58f;  // decay: fast
  if (f_mouth < 0.04f) f_mouth = 0;
  FACE_LOCK();
  draw_mouth();
  if (millis() >= f_next_blink) {               // quick blink: audio-safe
    face_blink(true);
    f_next_blink = millis() + random(2600, 5000);
  }
  FACE_UNLOCK();
}

bool face_begin(void) {
  if (!f_gfx->begin()) return false;
  f_gfx->setBrightness(FACE_BRIGHTNESS);
  bool ok = cv_eyes->begin(GFX_SKIP_OUTPUT_BEGIN);
  ok &= cv_mouth->begin(GFX_SKIP_OUTPUT_BEGIN);
  ok &= cv_dots->begin(GFX_SKIP_OUTPUT_BEGIN);
  f_gfx->fillScreen(FC_CREAM);
  draw_cheeks();
  draw_eyes();
  draw_mouth();
  f_mux = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(face_task, "face", 8192, nullptr, 1, nullptr, 0);
  return ok;
}
