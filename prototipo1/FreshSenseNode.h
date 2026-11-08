#pragma once

// =============================================================================
// ARCHIVO NORMAL (el que ya funciona) — FreshSenseNode.h
// =============================================================================
// Sensores AM2302/MQ135, LED, buzzer, rele, SensorReading, WiFi/POST.
// NO contiene el supuesto. El supuesto esta en FreshSenseSupuesto.h
// y se conecta desde prototipo1.ino.
// =============================================================================

#ifndef FRESHSENSE_REAL
#define FRESHSENSE_REAL 1
#endif

#include <stdio.h>
#include <string.h>

#include <ArduinoJson.h>
#include <DHTesp.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>

// --- Pines GPIO --------------------------------------------------------------
// AM2302 usa el mismo protocolo que DHT22. DAT en GPIO 4 (seguro al boot).
// MQ135 AO en GPIO 34 (solo entrada analogica; no sirve como salida).
#if FRESHSENSE_REAL
static const int CLIMATE_PIN = 4;      // AM2302 DAT
#else
static const int CLIMATE_PIN = 12;     // Solo Wokwi
#endif
static const int ETHYLENE_PIN = 34;    // MQ135 AO
static const int LED_ALERT_PIN = 27;   // LED alerta HIGH_ETHYLENE
static const int BUZZER_PIN = 25;      // Buzzer MH-FMD I/O
static const int FAN_PIN = 26;         // Relé IN → motor / ventilador

// --- Polaridad de actuadores -------------------------------------------------
// Relé azul HW-482: en ESTA placa IN en ALTO cierra COM-NO (motor ON).
// Con true el motor iba al revés; aquí debe ser false.
static const bool RELAY_ACTIVE_LOW = false;
// Buzzer MH-FMD: leyenda 低电平触发 → suena con pin en LOW.
static const bool BUZZER_ACTIVE_LOW = true;

// Identificador que el servidor asigna al vincular el dispositivo (US31).
// Hay que reemplazarlo por el UUID real del alta.
static const char *DEVICE_ID = "00000000-0000-0000-0000-000000000001";

// Token que DeviceTokenValidator revisa antes de aceptar la lectura (TS41).
static const char *DEVICE_TOKEN = "replace-with-device-token";

static const char *FIRMWARE_VERSION = "1.0";

// Lecturas van al Edge (Freshsense-EDGE): misma ruta /api/v1/sensor-readings.
// Cambia TU_IP_PC por la IP de la computadora donde corre:
//   flask --app src.app run --host 0.0.0.0 --port 5000
// El heartbeat sigue al backend publicado del equipo.
static const char *READINGS_URL = "http://TU_IP_PC:5000/api/v1/sensor-readings";
static const char *HEARTBEAT_HOST = "https://35-224-123-160.sslip.io";

// WiFi. En la placa escribe el nombre y la clave de tu red.
// Si dejas TU_RED_WIFI, el LED, la bocina y el ventilador igual funcionan:
// solo se queda sin envio al servidor.
#if FRESHSENSE_REAL
static const char *WIFI_SSID = "TU_RED_WIFI";
static const char *WIFI_PASSWORD = "TU_CLAVE_WIFI";
static const int WIFI_CHANNEL = 0;
#else
static const char *WIFI_SSID = "Wokwi-GUEST";
static const char *WIFI_PASSWORD = "";
static const int WIFI_CHANNEL = 6;
#endif

// America/Lima, sin horario de verano.
static const long GMT_OFFSET_SEC = -5L * 3600L;
static const int DAYLIGHT_OFFSET_SEC = 0;

// PERIOD_MS: POST lectura + heartbeat a internet (si hay WiFi/token).
static const unsigned long PERIOD_MS = 60000;  // al ritmo del gas (1 min)
#if FRESHSENSE_REAL
// AM2302 entrega temp+humedad juntos → lectura de clima cada 3 min.
static const unsigned long CLIMATE_PERIOD_MS = 3UL * 60UL * 1000UL;  // 180000
// MQ135 (etileno) cada 1 min.
static const unsigned long GAS_PERIOD_MS = 1UL * 60UL * 1000UL;      // 60000
#else
static const unsigned long CLIMATE_PERIOD_MS = PERIOD_MS;
static const unsigned long GAS_PERIOD_MS = PERIOD_MS;
#endif

// Wokwi: la posicion inicial del sensor de gas lee cerca de 3628 (35 ppm).
static const float GAS_RAW_AT_REFERENCE = 3628.0f;
static const float GAS_PPM_AT_REFERENCE = 35.0f;

// Placa: el MQ135 no trae ppm de fabrica. Al terminar el calentamiento se
// guarda la lectura del aire de la sala. ethylenePpm mide cuanto sube esa
// lectura. 200 cuentas por encima de la zona muerta equivalen a 20 ppm,
// que es el aviso HIGH_ETHYLENE.
static const float GAS_DEADBAND_COUNTS = 30.0f;
static const float GAS_PPM_PER_COUNT = 0.10f;
static const int GAS_WARMUP_SEC = 30;

// Rangos fisicos que usa isValid(), el metodo de SensorReading en el informe.
static const float TEMP_MIN_C = -40.0f;
static const float TEMP_MAX_C = 80.0f;
static const float HUMIDITY_MIN = 0.0f;
static const float HUMIDITY_MAX = 100.0f;
static const float ETHYLENE_MIN_PPM = 0.0f;
static const float ETHYLENE_MAX_PPM = 10000.0f;

static const unsigned long BEEP_HALF_MS = 400;

// LED: apagado / continuo (AT_RISK) / parpadeo (SPOILED)
enum LedFrescura {
  LED_APAGADO = 0,
  LED_CONTINUO = 1,
  LED_PARPADEO = 2
};

// =============================================================================
// ESTANDAR FreshSense FS-FRUIT-1 (FAO / UC Davis — camara mixta de frutas)
// Fuentes: FAO postcosecha; UC Davis Postharvest (grupos 0-2 C / 7-10 C / 13-18 C).
// Camara generica de frutas: ~0-8 C y 90-95 % HR; etileno objetivo < 1 ppm.
// =============================================================================
//
// LECTURAS
//   Clima (AM2302 temp+humedad): cada 3 minutos
//   Gas (MQ135 etileno):         cada 1 minuto
//
// TEMPERATURA
//   NORMAL:        0.0 .. 8.0 C     (camara mixta practica)
//   SEMI alto:     > 8.0 .. 12.0 C
//   CRITICO alto:  > 12.0 C
//   SEMI bajo:     -1.0 .. < 0.0 C
//   CRITICO bajo:  < -1.0 C
//
// HUMEDAD (FAO: mayoria de frutas 90-95 %)
//   NORMAL:        90 .. 95 %
//   SEMI bajo:     85 .. < 90 %
//   CRITICO bajo:  < 85 %
//   SEMI alto:     > 95 .. 98 %
//   CRITICO alto:  > 98 %
//
// ETILENO (FAO: mantener < 1 ppm en camara)
//   NORMAL:        0 .. 1 ppm
//   SEMI:          > 1 .. 5 ppm     → ventilador ON (extraer gas)
//   CRITICO:       > 5 ppm          → ventilador ON (urgencia)
//
// ACTUADORES (independientes)
//   LED     ← solo temperatura y humedad
//             SEMI → continuo | CRITICO → parpadeo + buzzer
//   Ventilador ← solo etileno (SEMI o CRITICO)
// =============================================================================

enum NivelSensor {
  SENSOR_OK = 0,
  SENSOR_SEMI = 1,
  SENSOR_CRITICO = 2
};

// Temperatura
static const float TEMP_OK_MIN_C = 0.0f;
static const float TEMP_OK_MAX_C = 8.0f;
static const float TEMP_SEMI_HIGH_MAX_C = 12.0f;
static const float TEMP_SEMI_LOW_MIN_C = -1.0f;

// Humedad
static const float HUM_OK_MIN = 90.0f;
static const float HUM_OK_MAX = 95.0f;
static const float HUM_SEMI_LOW_MIN = 85.0f;
static const float HUM_SEMI_HIGH_MAX = 98.0f;

// Etileno
static const float ETHYLENE_OK_MAX_PPM = 1.0f;
static const float ETHYLENE_SEMI_MAX_PPM = 5.0f;

static const float TEMP_FAN_C = TEMP_OK_MAX_C;           // legacy
static const float ETHYLENE_ALERT_PPM = ETHYLENE_OK_MAX_PPM;  // fan desde >1 ppm

inline NivelSensor nivelTemperatura(float temperatureC) {
  if (temperatureC > TEMP_SEMI_HIGH_MAX_C || temperatureC < TEMP_SEMI_LOW_MIN_C) {
    return SENSOR_CRITICO;
  }
  if (temperatureC > TEMP_OK_MAX_C || temperatureC < TEMP_OK_MIN_C) {
    return SENSOR_SEMI;
  }
  return SENSOR_OK;
}

inline NivelSensor nivelHumedad(float humidityPct) {
  if (humidityPct < HUM_SEMI_LOW_MIN || humidityPct > HUM_SEMI_HIGH_MAX) {
    return SENSOR_CRITICO;
  }
  if (humidityPct < HUM_OK_MIN || humidityPct > HUM_OK_MAX) {
    return SENSOR_SEMI;
  }
  return SENSOR_OK;
}

inline NivelSensor nivelEtileno(float ethylenePpm) {
  if (ethylenePpm > ETHYLENE_SEMI_MAX_PPM) {
    return SENSOR_CRITICO;
  }
  if (ethylenePpm > ETHYLENE_OK_MAX_PPM) {
    return SENSOR_SEMI;
  }
  return SENSOR_OK;
}

inline NivelSensor peorDe(NivelSensor a, NivelSensor b) {
  return (b > a) ? b : a;
}

// LED solo por temp+hum. Ventilador solo por etileno. Frescura = peor de los 3.
inline void evaluarActuadores(NivelSensor t, NivelSensor h, NivelSensor e,
                              const char *&frescura, LedFrescura &led, bool &ventiladorGas) {
  NivelSensor peorClima = peorDe(t, h);
  NivelSensor peorTodo = peorDe(peorClima, e);
  ventiladorGas = (e != SENSOR_OK);  // SEMI o CRITICO de gas → motor

  if (peorClima == SENSOR_CRITICO) {
    led = LED_PARPADEO;
  } else if (peorClima == SENSOR_SEMI) {
    led = LED_CONTINUO;
  } else {
    led = LED_APAGADO;
  }

  if (peorTodo == SENSOR_CRITICO) {
    frescura = "SPOILED";
  } else if (peorTodo == SENSOR_SEMI) {
    frescura = "AT_RISK";
  } else {
    frescura = "FRESH";
  }
}

// Lee temperatureC y humidityPct del AM2302.
class ClimateSensor {
 public:
  // El AM2302 necesita unos 2 s antes de la primera lectura valida.
  void begin() {
    pinMode(CLIMATE_PIN, INPUT_PULLUP);
    sensor.setup(CLIMATE_PIN, DHTesp::DHT22);
    delay(sensor.getMinimumSamplingPeriod());
  }

  bool read(float &temperatureC, float &humidityPct) {
    TempAndHumidity sample = sensor.getTempAndHumidity();
    if (sensor.getStatus() != DHTesp::ERROR_NONE) {
      Serial.print("AM2302: ");
      Serial.println(sensor.getStatusString());
      return false;
    }
    if (isnan(sample.temperature) || isnan(sample.humidity)) {
      Serial.println("AM2302: lectura vacia.");
      return false;
    }
    temperatureC = sample.temperature;
    humidityPct = sample.humidity;
    return true;
  }

 private:
  DHTesp sensor;
};

// Lee ethylenePpm. El informe exige esa magnitud en cada lectura.
// En la placa el MQ135 llena ese campo: no es un sensor de etileno,
// es el sensor de aire del pedido, leido por el pin AO.
class EthyleneSensor {
 public:
  void begin() {
    analogReadResolution(12);
    analogSetPinAttenuation(ETHYLENE_PIN, ADC_11db);
    delay(10);
    lastRaw = sampleRaw();
  }

  // El calefactor del MQ135 tiene que tomar temperatura antes de fijar
  // la base. Si no, el LED y la bocina se disparan con el sensor frio.
  void warmUp() {
    Serial.println("MQ135 calentando. El sensor se pone tibio: es normal.");
    for (int second = 1; second <= GAS_WARMUP_SEC; second++) {
      delay(1000);
      lastRaw = sampleRaw();
      Serial.print("  ");
      Serial.print(second);
      Serial.print(" s, crudo ");
      Serial.println(lastRaw, 0);
    }
    baseline = lastRaw;
    ready = true;
    Serial.print("MQ135 base de aire: ");
    Serial.println(baseline, 0);
  }

  float readPpm() {
#if FRESHSENSE_REAL
    float raw = sampleRaw();
    lastRaw = raw;
    if (!ready) {
      baseline = raw;
      ready = true;
    }
    if (raw < baseline) {
      baseline = raw;
    }
    float delta = raw - baseline - GAS_DEADBAND_COUNTS;
    if (delta < 0.0f) {
      delta = 0.0f;
    }
    return delta * GAS_PPM_PER_COUNT;
#else
    int raw = analogRead(ETHYLENE_PIN);
    if (raw < 0) {
      raw = 0;
    }
    lastRaw = raw;
    return raw * (GAS_PPM_AT_REFERENCE / GAS_RAW_AT_REFERENCE);
#endif
  }

  float rawCounts() const {
    return lastRaw;
  }

  float baselineCounts() const {
    return baseline;
  }

 private:
  float sampleRaw() {
    long sum = 0;
    for (int i = 0; i < 16; i++) {
      sum += analogRead(ETHYLENE_PIN);
      delay(2);
    }
    return sum / 16.0f;
  }

  float baseline = 0.0f;
  float lastRaw = 0.0f;
  bool ready = false;
};

// Reloj de la lectura. El campo del informe se llama timestamp.
class ReadingClock {
 public:
  void begin() {
    configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, "pool.ntp.org", "time.nist.gov");
  }

  // ISO-8601 con la hora de Lima. Si el NTP aun no responde, usa un reloj local.
  String stamp() {
    struct tm now;
    if (!getLocalTime(&now, 1500) || now.tm_year < 120) {
      time_t fallback = 1791459120L + (millis() / 1000L);
      gmtime_r(&fallback, &now);
    }
    char buffer[25];
    strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S", &now);
    return String(buffer);
  }
};

// Agregado SensorReading del informe: guarda las tres magnitudes,
// descarta una lectura imposible y arma el JSON del recurso.
class SensorReading {
 public:
  String deviceId;
  String timestamp;
  float temperatureC;
  float humidityPct;
  float ethylenePpm;
  String meta;

  // isValid() del informe: las tres magnitudes tienen que ser fisicamente posibles.
  bool isValid() const {
    if (temperatureC < TEMP_MIN_C || temperatureC > TEMP_MAX_C) {
      return false;
    }
    if (humidityPct < HUMIDITY_MIN || humidityPct > HUMIDITY_MAX) {
      return false;
    }
    if (ethylenePpm < ETHYLENE_MIN_PPM || ethylenePpm > ETHYLENE_MAX_PPM) {
      return false;
    }
    return true;
  }

  // Campos de SensorReadingResource: deviceId, timestamp, temperatureC,
  // humidityPct, ethylenePpm, meta.
  String toJson() const {
    String temperatureText(temperatureC, 1);
    String humidityText(humidityPct, 1);
    String ethyleneText(ethylenePpm, 1);

    StaticJsonDocument<384> document;
    document["deviceId"] = deviceId;
    document["timestamp"] = timestamp;
    document["temperatureC"] = serialized(temperatureText);
    document["humidityPct"] = serialized(humidityText);
    document["ethylenePpm"] = serialized(ethyleneText);
    document["meta"] = meta;

    String payload;
    serializeJsonPretty(document, payload);
    return payload;
  }
};

// LED rojo (GPIO 27) + buzzer (GPIO 25) segun frescura FS-FRUIT-1:
//   FRESH     → LED apagado
//   AT_RISK  → LED rojo CONTINUO = se va a malograr
//   SPOILED   → LED rojo PARPADEA + buzzer = ya se malogro
class AlertActuators {
 public:
  void begin() {
    pinMode(LED_ALERT_PIN, OUTPUT);
    pinMode(BUZZER_PIN, OUTPUT);
    setEstado(LED_APAGADO);
  }

  void setEstado(LedFrescura estado) {
    modo = estado;
    if (modo == LED_APAGADO) {
      digitalWrite(LED_ALERT_PIN, LOW);
      ledEncendido = false;
      buzzerOn = false;
      writeBuzzer(false);
    } else if (modo == LED_CONTINUO) {
      // Se va a malograr: luz roja fija, sin pitido.
      digitalWrite(LED_ALERT_PIN, HIGH);
      ledEncendido = true;
      buzzerOn = false;
      writeBuzzer(false);
    }
    // SPOILED: tick() se encarga del parpadeo + buzzer.
  }

  // Compatibilidad con self-test y llamadas viejas.
  void setAlert(bool active) {
    setEstado(active ? LED_CONTINUO : LED_APAGADO);
  }

  // Parpadeo de LED + buzzer solo en SPOILED.
  void tick() {
    if (modo != LED_PARPADEO) {
      return;
    }
    unsigned long now = millis();
    if (now - lastBeepAt < BEEP_HALF_MS) {
      return;
    }
    lastBeepAt = now;
    ledEncendido = !ledEncendido;
    digitalWrite(LED_ALERT_PIN, ledEncendido ? HIGH : LOW);
    buzzerOn = ledEncendido;
    writeBuzzer(buzzerOn);
  }

 private:
  void writeBuzzer(bool sounding) {
    bool pinHigh = BUZZER_ACTIVE_LOW ? !sounding : sounding;
    digitalWrite(BUZZER_PIN, pinHigh ? HIGH : LOW);
  }

  LedFrescura modo = LED_APAGADO;
  bool ledEncendido = false;
  bool buzzerOn = false;
  unsigned long lastBeepAt = 0;
};

// Ventilador de la camara: motor DC + helice via rele HW-482 (no KY-019).
// GPIO 26 solo manda al pin IN. La corriente del motor va por COM (medio)
// y NO (arriba) desde la raya 5V. NC queda vacio.
// Ventilador por ETILENO (SEMI/CRITICO > 1 ppm), no por temperatura.
class ColdRoomFan {
 public:
  void begin() {
    pinMode(FAN_PIN, OUTPUT);
    setRunning(false);
  }

  // running true → motor gira. Con RELAY_ACTIVE_LOW false, IN va a HIGH.
  void setRunning(bool running) {
    bool pinHigh = RELAY_ACTIVE_LOW ? !running : running;
    digitalWrite(FAN_PIN, pinHigh ? HIGH : LOW);
  }
};

// Envia la telemetria y el heartbeat.
// FRESH, AT_RISK y SPOILED los calcula el servidor.
// Esta clase no enciende el LED, la bocina ni el ventilador.
class DeviceLink {
 public:
  bool connectWifi() {
#if FRESHSENSE_REAL
    if (strcmp(WIFI_SSID, "TU_RED_WIFI") == 0 || WIFI_SSID[0] == '\0') {
      Serial.println("WiFi pendiente: cambia WIFI_SSID y WIFI_PASSWORD.");
      Serial.println("El circuito local funciona igual.");
      return false;
    }
#endif
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, WIFI_CHANNEL);
    Serial.print("WiFi conectando a ");
    Serial.println(WIFI_SSID);
    for (int attempt = 0; attempt < 40 && WiFi.status() != WL_CONNECTED; attempt++) {
      delay(250);
      Serial.print(".");
    }
    Serial.println();
    if (WiFi.status() == WL_CONNECTED) {
      Serial.print("WiFi listo. IP ");
      Serial.println(WiFi.localIP());
      return true;
    }
    Serial.println("WiFi sin conexion. Sensores y actuadores siguen.");
    return false;
  }

  int rssi() const {
    if (WiFi.status() != WL_CONNECTED) {
      return 0;
    }
    return WiFi.RSSI();
  }

  // POST /api/v1/sensor-readings (TS41).
  int postReading(const String &payload) {
    return post(READINGS_URL, payload);
  }

  // POST /api/v1/devices/{id}/heartbeat (US06).
  int postHeartbeat(const String &payload) {
    String url = String(HEARTBEAT_HOST) + "/api/v1/devices/" + DEVICE_ID + "/heartbeat";
    return post(url.c_str(), payload);
  }

 private:
  int post(const char *url, const String &payload) {
    if (WiFi.status() != WL_CONNECTED) {
      return -1;
    }

    // Edge local: http://  |  Backend publicado: https://
    // El cliente WiFi debe vivir hasta despues de http.POST.
    HTTPClient http;
    if (strncmp(url, "https://", 8) == 0) {
      WiFiClientSecure secure;
      secure.setInsecure();
      if (!http.begin(secure, url)) {
        return -2;
      }
      http.addHeader("Content-Type", "application/json");
      http.addHeader("X-Device-Token", DEVICE_TOKEN);
      int code = http.POST(payload);
      http.end();
      return code;
    }

    WiFiClient plain;
    if (!http.begin(plain, url)) {
      return -2;
    }
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Token", DEVICE_TOKEN);
    int code = http.POST(payload);
    http.end();
    return code;
  }
};

// Junta la medicion, la validacion y los dos envios del informe.
class FreshSenseNode {
 public:
  void begin() {
    Serial.begin(115200);
    delay(400);
    Serial.println();
    Serial.println("FreshSense listo para medir.");
    actuators.begin();
    fan.begin();
#if FRESHSENSE_REAL
    selfTest();
#endif
    climate.begin();
    ethylene.begin();
#if FRESHSENSE_REAL
    ethylene.warmUp();
#endif
    bool online = link.connectWifi();
    if (online) {
      clock.begin();
    } else {
      Serial.println("Sin reloj de red: el timestamp sale de un reloj local.");
    }
    nextClimateAt = millis();
    nextGasAt = millis();
    nextPostAt = millis();
    lastTempC = 4.0f;
    lastHumPct = 92.0f;
    lastPpm = 0.0f;
    hayClima = false;
    hayGas = false;
#if FRESHSENSE_REAL
    Serial.println("FS-FRUIT-1: clima (temp+hum) cada 3 min; etileno cada 1 min.");
    Serial.println("LED <- temp/humedad | Ventilador <- etileno (>1 ppm).");
#else
    Serial.println("Lectura Wokwi periodica.");
#endif
  }

  // Args del supuesto: prototipo1.ino (FreshSenseSupuesto.h).
  void tick(bool supuestoActivo = false,
            float supuestoTemp = 0.0f,
            float supuestoHum = 92.0f,
            float supuestoPpm = 0.0f,
            bool supuestoRecien = false) {
    actuators.tick();
    unsigned long now = millis();
    bool leerClima = supuestoRecien || (now >= nextClimateAt);
    bool leerGas = supuestoRecien || (now >= nextGasAt);
    if (!leerClima && !leerGas) {
      return;
    }
    if (leerClima) {
      nextClimateAt = now + CLIMATE_PERIOD_MS;
    }
    if (leerGas) {
      nextGasAt = now + GAS_PERIOD_MS;
    }
    publish(supuestoActivo, supuestoTemp, supuestoHum, supuestoPpm, leerClima, leerGas);
  }

 private:
#if FRESHSENSE_REAL
  // Destello del LED, un pitido y un giro corto (cableado OK antes del MQ135).
  void selfTest() {
    Serial.println("Prueba: LED, bocina y motor.");
    digitalWrite(LED_ALERT_PIN, HIGH);
    digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_LOW ? LOW : HIGH);
    delay(300);
    digitalWrite(LED_ALERT_PIN, LOW);
    digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_LOW ? HIGH : LOW);
    fan.setRunning(true);
    delay(500);
    fan.setRunning(false);
    actuators.setEstado(LED_APAGADO);
    Serial.println("Prueba hecha. LED <- temp/hum | Ventilador <- etileno.");
    Serial.println("Si la bocina siguio sonando, pon BUZZER_ACTIVE_LOW en true y graba de nuevo.");
    Serial.println("Si el motor siguio girando, pon RELAY_ACTIVE_LOW en true y graba de nuevo.");
  }
#endif

  void publish(bool supuestoActivo,
               float supuestoTemp,
               float supuestoHum,
               float supuestoPpm,
               bool leerClima,
               bool leerGas) {
    if (leerClima) {
      float temperatureC = 0.0f;
      float humidityPct = 0.0f;
      if (climate.read(temperatureC, humidityPct)) {
        lastTempC = temperatureC;
        lastHumPct = humidityPct;
        hayClima = true;
      } else {
        Serial.println("Lectura de clima invalida.");
#if FRESHSENSE_REAL
        Serial.println("AM2302: VCC a 3V3, GND a tierra, DAT al pin 4.");
#endif
      }
    }

    if (leerGas) {
      lastPpm = ethylene.readPpm();
      hayGas = true;
#if FRESHSENSE_REAL
      Serial.print("MQ135 crudo ");
      Serial.print(ethylene.rawCounts(), 0);
      Serial.print(" base ");
      Serial.print(ethylene.baselineCounts(), 0);
      Serial.print(" -> ");
      Serial.print(lastPpm, 1);
      Serial.println(" ppm");
      if (ethylene.rawCounts() < 50.0f) {
        Serial.println("MQ135 mudo. VCC a 5V, GND a tierra, AO al pin 34.");
      }
#endif
    }

    float mandoTemp = supuestoActivo ? supuestoTemp : lastTempC;
    float mandoHum = supuestoActivo ? supuestoHum : lastHumPct;
    float mandoPpm = supuestoActivo ? supuestoPpm : lastPpm;
    bool hayMando = supuestoActivo || (hayClima && hayGas);

    SensorReading reading;
    bool realValida = false;
    if (hayClima && hayGas) {
      reading.deviceId = DEVICE_ID;
      reading.timestamp = clock.stamp();
      reading.temperatureC = lastTempC;
      reading.humidityPct = lastHumPct;
      reading.ethylenePpm = lastPpm;
      reading.meta = String("firmware=") + FIRMWARE_VERSION + ";rssi=" + String(link.rssi()) + ";origen=sensores";
      Serial.println("SensorReading");
      Serial.println(reading.toJson());
      realValida = reading.isValid();
    }

    if (!hayMando) {
      actuators.setEstado(LED_APAGADO);
      fan.setRunning(false);
    } else {
      NivelSensor nTemp = nivelTemperatura(mandoTemp);
      NivelSensor nHum = nivelHumedad(mandoHum);
      NivelSensor nEth = nivelEtileno(mandoPpm);
      const char *frescura = "FRESH";
      LedFrescura led = LED_APAGADO;
      bool ventilador = false;
      evaluarActuadores(nTemp, nHum, nEth, frescura, led, ventilador);
      actuators.setEstado(led);
      fan.setRunning(ventilador);

      Serial.print("FS-FRUIT-1 ");
      Serial.print(frescura);
      Serial.print(" temp=");
      Serial.print(nTemp);
      Serial.print(" hum=");
      Serial.print(nHum);
      Serial.print(" eth=");
      Serial.println(nEth);
      if (led == LED_PARPADEO) {
        Serial.println("LED parpadeo: temp/hum CRITICO.");
      } else if (led == LED_CONTINUO) {
        Serial.println("LED continuo: temp/hum SEMI.");
      } else {
        Serial.println("LED apagado: temp/hum OK.");
      }
      if (ventilador) {
        Serial.println("Ventilador ON: etileno SEMI/CRITICO (>1 ppm).");
      } else {
        Serial.println("Ventilador OFF: etileno OK.");
      }
    }

    if (!realValida || millis() < nextPostAt) {
      return;
    }
    nextPostAt = millis() + PERIOD_MS;

    int readingCode = link.postReading(reading.toJson());
    Serial.print("POST /api/v1/sensor-readings -> ");
    Serial.println(readingCode);

    StaticJsonDocument<128> heartbeat;
    heartbeat["deviceId"] = DEVICE_ID;
    heartbeat["timestamp"] = reading.timestamp;
    String heartbeatPayload;
    serializeJsonPretty(heartbeat, heartbeatPayload);

    Serial.println("Heartbeat");
    Serial.println(heartbeatPayload);
    int heartbeatCode = link.postHeartbeat(heartbeatPayload);
    Serial.print("POST /api/v1/devices/{id}/heartbeat -> ");
    Serial.println(heartbeatCode);
  }

  ClimateSensor climate;
  EthyleneSensor ethylene;
  AlertActuators actuators;
  ColdRoomFan fan;
  ReadingClock clock;
  DeviceLink link;
  unsigned long nextClimateAt;
  unsigned long nextGasAt;
  unsigned long nextPostAt;
  float lastTempC;
  float lastHumPct;
  float lastPpm;
  bool hayClima;
  bool hayGas;
};
