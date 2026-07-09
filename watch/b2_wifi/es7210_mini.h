/*
 * es7210_mini — minimal Arduino port of Espressif's ES7210 ADC driver.
 *
 * The watch's dual mics feed the ES7210 (NOT the ES8311, which only does
 * speaker DAC here). The ES7210 sits on the same I2S bus as a slave TX and
 * stays in reset until configured over I2C — that's why recordings were
 * digital zero before this. Register sequence is a 1:1 copy of
 * es7210_config_codec() from espressif/esp-bsp (Apache-2.0), specialised to:
 * 16 kHz, MCLK=256×fs (4.096 MHz), standard I2S, 16-bit, TDM off,
 * mic bias 2.87 V, mic gain 30 dB.
 *
 * I2C goes through arduino-esp32's HAL (i2cWrite), sharing the bus with
 * Wire — same approach as the vendor's es8311.c.
 */
#pragma once
#include "esp32-hal-i2c.h"
#include "Wire.h"

static uint8_t es7210_addr = 0;   // set by es7210_mini_init after probing

static bool es7210_wr(uint8_t reg, uint8_t val) {
  const uint8_t buf[2] = {reg, val};
  return i2cWrite(0, es7210_addr, buf, 2, 1000) == ESP_OK;
}

// Probe 0x40-0x43 (address depends on the AD0/AD1 strapping), then run the
// full config. Returns true when the mic ADC is up.
static bool es7210_mini_init(void) {
  for (uint8_t a = 0x40; a <= 0x43; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { es7210_addr = a; break; }
  }
  if (!es7210_addr) return false;

  static const uint8_t seq[][2] = {
      {0x00, 0xFF},               // software reset
      {0x00, 0x32},               // release reset
      {0x09, 0x30}, {0x0A, 0x30}, // power-up timing
      {0x23, 0x2A}, {0x22, 0x0A}, // HPF ADC1/2
      {0x21, 0x2A}, {0x20, 0x0A}, // HPF ADC3/4
      {0x11, 0x60},               // SDP: standard I2S, 16-bit
      {0x12, 0x00},               // TDM off
      {0x40, 0xC3},               // analog power + VMID
      {0x41, 0x70}, {0x42, 0x70}, // mic bias 2.87V
      {0x43, 0x1A}, {0x44, 0x1A}, // mic1/2 gain 30dB (0x0A | 0x10)
      {0x45, 0x1A}, {0x46, 0x1A}, // mic3/4 gain 30dB
      {0x47, 0x08}, {0x48, 0x08}, // mic power on
      {0x49, 0x08}, {0x4A, 0x08},
      // clocking for 16 kHz @ MCLK 4.096 MHz (coeff row: div=1, dll=1, dbl=1)
      {0x07, 0x20},               // OSR
      {0x02, 0xC1},               // adc_div=1 | doubler<<6 | dll<<7
      {0x04, 0x01}, {0x05, 0x00}, // LRCK divider 0x0100 = 256
      {0x06, 0x04},               // power down DLL
      {0x4B, 0x0F}, {0x4C, 0x0F}, // mic bias + ADC + PGA power on
      {0x00, 0x71},               // enable device...
      {0x00, 0x41},               // ...and run
  };
  for (auto &rv : seq)
    if (!es7210_wr(rv[0], rv[1])) return false;
  return true;
}
