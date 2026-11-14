// =============================================================================
// prototipo1.ino - une DOS archivos separados
// =============================================================================

// ---------------------------------------------------------------------------
// CODIGO NORMAL (el que ya funciona)
// FreshSenseNode.h = sensores, LED, buzzer, rele, SensorReading, estandar FS-FRUIT-1
// ---------------------------------------------------------------------------
#include "FreshSenseNode.h"

FreshSenseNode node;

// ---------------------------------------------------------------------------
// -----
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// CODIGO DEL SUPUESTO (archivo aparte)
// FreshSenseSupuesto.h = solo SUPUESTO / REAL por USB
// ---------------------------------------------------------------------------
#include "FreshSenseSupuesto.h"

SupuestoUsb supuesto;

// ---------------------------------------------------------------------------

void setup() {
  node.begin();
}

void loop() {
  // --- supuesto (FreshSenseSupuesto.h) ---
  supuesto.tick();
  node.fijarVentiladorApagado(supuesto.ventiladorApagado());
  bool recien = supuesto.consumirRecien();

  // --- codigo normal (FreshSenseNode.h) ---
  node.tick(
      supuesto.activo(),
      supuesto.temperatureC(),
      supuesto.humidityPct(),
      supuesto.ethylenePpm(),
      recien
  );

  static unsigned long lastEcho = 0;
  if (recien || millis() - lastEcho >= 2000) {
    lastEcho = millis();
    supuesto.imprimirEstado();
  }
}
