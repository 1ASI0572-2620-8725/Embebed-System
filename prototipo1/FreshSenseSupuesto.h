#pragma once

// =============================================================================
// ARCHIVO DEL SUPUESTO (separado del codigo que ya funciona)
// FreshSenseSupuesto.h
// =============================================================================
// Lee por USB lo que manda servidor.py:
//   SUPUESTO temp hum ppm  -> mueve LED / buzzer / motor
//   REAL                   -> actuadores vuelven a los sensores
//   VENTILADOR OFF|AUTO    -> apaga el motor a mano, o lo devuelve a los sensores
// No reemplaza SensorReading (AM2302 + MQ135 siguen midiendo).
// =============================================================================

class SupuestoUsb {
 public:
  void tick() {
    while (Serial.available() > 0) {
      char letra = Serial.read();
      if (letra == '\n' || letra == '\r') {
        aplicarLinea();
        linea = "";
      } else if (linea.length() < 80) {
        linea += letra;
      }
    }
  }

  bool consumirRecien() {
    if (!recien) {
      return false;
    }
    recien = false;
    return true;
  }

  bool activo() const { return encendido; }
  bool ventiladorApagado() const { return apagadoManual; }
  float temperatureC() const { return temp; }
  float humidityPct() const { return hum; }
  float ethylenePpm() const { return ppm; }

  void imprimirEstado() const {
    Serial.println("SupuestoLectura");
    if (!encendido) {
      Serial.println("{\"activo\":false}");
      return;
    }
    Serial.print("{\"activo\":true,\"temperatureC\":");
    Serial.print(temp, 1);
    Serial.print(",\"humidityPct\":");
    Serial.print(hum, 1);
    Serial.print(",\"ethylenePpm\":");
    Serial.print(ppm, 1);
    Serial.println("}");
  }

 private:
  void aplicarLinea() {
    linea.trim();
    if (linea.length() == 0) {
      return;
    }
    if (linea.startsWith("VENTILADOR")) {
      bool ahora = linea.indexOf("OFF") >= 0;
      apagadoManual = ahora;
      if (!ahora) {
        recien = true;
      }
      Serial.println(ahora ? "Ventilador manual OFF." : "Ventilador manual AUTO.");
      return;
    }
    if (linea.startsWith("REAL")) {
      if (encendido) {
        recien = true;
      }
      encendido = false;
      Serial.println("ACK REAL");
      return;
    }
    if (!linea.startsWith("SUPUESTO")) {
      return;
    }
    float temperatura = 0.0f;
    float humedad = 0.0f;
    float etileno = 0.0f;
    int leidos = sscanf(linea.c_str(), "SUPUESTO %f %f %f", &temperatura, &humedad, &etileno);
    if (leidos != 3) {
      return;
    }
    if (!encendido || distinto(temp, temperatura) || distinto(hum, humedad) || distinto(ppm, etileno)) {
      recien = true;
    }
    temp = temperatura;
    hum = humedad;
    ppm = etileno;
    encendido = true;
    Serial.print("ACK SUPUESTO ");
    Serial.print(temp, 1);
    Serial.print(" ");
    Serial.print(hum, 1);
    Serial.print(" ");
    Serial.println(ppm, 1);
  }

  static bool distinto(float a, float b) {
    float d = a - b;
    if (d < 0.0f) {
      d = -d;
    }
    return d > 0.05f;
  }

  bool encendido = false;
  bool recien = false;
  bool apagadoManual = false;
  float temp = 6.4f;
  float hum = 82.0f;
  float ppm = 5.0f;
  String linea;
};

