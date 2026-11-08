# Diagrama de clases — Embebed System (FreshSense)

Dispositivo IoT de cámara de frío para **frutas** (estándar FS-FRUIT-1).

- Código normal: `prototipo1/FreshSenseNode.h`
- Supuesto USB: `prototipo1/FreshSenseSupuesto.h` (archivo aparte)
- Entrada: `prototipo1/prototipo1.ino`
- Página local: `servidor/servidor.py`

## Dibujo

```mermaid
classDiagram
  direction TB

  class FreshSenseNode {
    +begin()
    +tick(supuestoActivo, temp, hum, ppm, recien)
    -publish()
    -selfTest()
  }

  class SupuestoUsb {
    +tick()
    +consumirRecien() bool
    +activo() bool
    +temperatureC() float
    +humidityPct() float
    +ethylenePpm() float
    +imprimirEstado()
  }

  class ClimateSensor {
    +begin()
    +read(temperatureC, humidityPct) bool
  }

  class EthyleneSensor {
    +begin()
    +warmUp()
    +readPpm() float
    +rawCounts() float
  }

  class ReadingClock {
    +begin()
    +stamp() String
  }

  class SensorReading {
    +deviceId String
    +timestamp String
    +temperatureC float
    +humidityPct float
    +ethylenePpm float
    +meta String
    +isValid() bool
    +toJson() String
  }

  class AlertActuators {
    +begin()
    +setEstado(LedFrescura)
    +tick()
  }

  class ColdRoomFan {
    +begin()
    +setRunning(running)
  }

  class DeviceLink {
    +connectWifi() bool
    +postReading(payload) int
    +postHeartbeat(payload) int
    +rssi() int
  }

  FreshSenseNode *-- ClimateSensor : tiene
  FreshSenseNode *-- EthyleneSensor : tiene
  FreshSenseNode *-- ReadingClock : tiene
  FreshSenseNode *-- AlertActuators : tiene
  FreshSenseNode *-- ColdRoomFan : tiene
  FreshSenseNode *-- DeviceLink : tiene
  FreshSenseNode ..> SensorReading : arma en cada ciclo
  FreshSenseNode ..> SupuestoUsb : recibe mando desde .ino

  ClimateSensor ..> SensorReading : temperatureC y humidityPct
  EthyleneSensor ..> SensorReading : ethylenePpm
  ReadingClock ..> SensorReading : timestamp

  SensorReading ..> AlertActuators : LED por temp y humedad
  SensorReading ..> ColdRoomFan : ventilador por etileno
  SensorReading ..> DeviceLink : POST periodico
  SupuestoUsb ..> AlertActuators : supuesto no tapa medicion
  SupuestoUsb ..> ColdRoomFan : supuesto mueve actuadores
```

## Qué hace cada clase

| Clase | Archivo | Trabajo |
|---|---|---|
| `FreshSenseNode` | `FreshSenseNode.h` | Orquesta lecturas (clima 3 min, gas 1 min), FS-FRUIT-1 y actuadores |
| `SupuestoUsb` | `FreshSenseSupuesto.h` | Lee `SUPUESTO` / `REAL` por USB; no reemplaza SensorReading |
| `ClimateSensor` | `FreshSenseNode.h` | AM2302 GPIO 4 → temp + humedad |
| `EthyleneSensor` | `FreshSenseNode.h` | MQ135 GPIO 34 → ppm |
| `AlertActuators` | `FreshSenseNode.h` | LED 27 + buzzer 25 según temp/hum (SEMI continuo, CRÍTICO parpadeo) |
| `ColdRoomFan` | `FreshSenseNode.h` | Relé 26 → motor si etileno SEMI/CRÍTICO (> 1 ppm) |
| `SensorReading` | `FreshSenseNode.h` | Agregado del informe + `isValid()` + JSON |
| `DeviceLink` | `FreshSenseNode.h` | WiFi opcional + POST lectura/heartbeat |
| `ReadingClock` | `FreshSenseNode.h` | Timestamp Lima / reloj local |

## Reglas FS-FRUIT-1 (resumen)

- **LED** ← temperatura y humedad (independiente; basta un sensor anómalo)
- **Ventilador** ← etileno (> 1 ppm)
- **FRESH / AT_RISK / SPOILED** según el peor nivel de los tres sensores
