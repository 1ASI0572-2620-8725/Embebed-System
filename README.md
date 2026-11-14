# Embebed System — FreshSense (prototipo físico)

Nodo IoT de **cámara de frío para frutas**. Mide temperatura, humedad y etileno; avisa con LED/buzzer y ventila según el estándar **FS-FRUIT-1** (FAO / UC Davis).

Organización: [1ASI0572-2620-8725](https://github.com/1ASI0572-2620-8725)

## Qué hace

| Señal | Lectura | NORMAL | SEMI | CRÍTICO | Actuador |
|---|---|---|---|---|---|
| Temperatura | cada **3 min** (AM2302) | 0–8 °C | (−1…0) o (8…12] | &lt;−1 o &gt;12 | LED y buzzer |
| Humedad | cada **3 min** (AM2302) | 90–95 % | 85–90 o 95–98 | &lt;85 o &gt;98 | LED y buzzer |
| Etileno | cada **1 min** (MQ135) | 0–1 ppm | &gt;1–5 | &gt;5 | LED, buzzer y ventilador |

- **LED fijo** = el peor de los tres está en SEMI. El buzzer sigue apagado.
- **LED parpadeo + buzzer** = cualquiera de los tres está en CRÍTICO.
- **Ventilador** solo por etileno: SEMI más lento, CRÍTICO a tope. Temperatura y humedad no lo encienden.
- Independiente: **basta un sensor** anómalo para avisar.  

Frescura agregada: **FRESH** / **AT_RISK** / **SPOILED**.

## Archivos (qué hace cada uno)

| Ruta | Rol |
|---|---|
| `prototipo1/prototipo1.ino` | Une código normal + supuesto; `setup` / `loop` |
| `prototipo1/FreshSenseNode.h` | **Código normal**: sensores, FS-FRUIT-1, LED, relé, WiFi/POST |
| `prototipo1/FreshSenseSupuesto.h` | **Código del supuesto**: lee `SUPUESTO` / `REAL` por USB |
| `servidor/servidor.py` | **Localhost** http://127.0.0.1:8080 — medición + supuestos |
| `docs/diagrama-de-clases.md` | Diagrama Mermaid explicado |
| `docs/class-diagram.puml` | Mismo diagrama en PlantUML |

## Hardware y pines

ESP32-S 38 pines, USB-C, CP2102. **No** se pincha en la protoboard.

| Pieza | Pin | Notas |
|---|---|---|
| AM2302 DAT | GPIO **4** | 3V3; no GPIO 12 |
| MQ135 AO | GPIO **34** | 5V; 220 Ω en serie; DO vacío |
| LED | GPIO **27** | 220 Ω; pata larga = ánodo |
| Buzzer MH-FMD I/O | GPIO **25** | Activo LOW |
| Relé IN | GPIO **26** | `RELAY_ACTIVE_LOW = true` (IN en bajo cierra COM-NO) |
| Pines 6–12 | vacíos | Memoria / strapping |

### Tabla de conexiones (fuente de verdad)

| Color | Desde | Hasta |
|---|---|---|
| Rojo HM | ESP32 5V | fila 1, raya roja lado j |
| Naranja HM | ESP32 3V3 | fila 1, raya roja lado a |
| Negro HM | ESP32 GND | fila 1, raya azul lado j |
| Morado MM | azul j fila 2 | azul a fila 1 |
| Naranja HM | AM2302 VCC | fila 2, raya roja lado a |
| Negro HM | AM2302 GND | fila 3, azul lado j |
| Verde HM | AM2302 DAT ↔ pin 4 | a6 / b6 |
| Rojo HM | MQ135 VCC | fila 2, raya roja lado j |
| Negro HM | MQ135 GND | fila 4, azul lado j |
| Blanco + 220Ω | MQ135 AO ↔ pin 34 | a18 — e18–f18 — g18 |
| Amarillo + 220Ω | pin 27 ↔ LED | a20 — e20–f20 — g20 |
| Morado HM | LED pata corta | fila 5 azul j |
| Rojo / Negro / Verde | Buzzer VCC / GND / I/O | roja j3 / azul j6 / a25↔pin25 |
| Rojo / Gris / Azul | Relé + / − / IN | roja j4 / azul j7 / a27↔pin26 |
| Marrón / Gris | Motor vía relé | COM←5V; NO→chapa; otra chapa→GND fila8 |

NC vacío. DO vacío. Raya roja j = 5V; raya roja a = 3V3; **nunca unirlas**.

## Cómo hacerlo funcionar

### 1. Arduino (grabar el ESP32)

1. Instala [Arduino IDE 2](https://www.arduino.cc/en/software).
2. URL de placas: `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. Placa **esp32** (Espressif) + librerías:
   - **DHT sensor library for ESPx** (DHTesp)
   - **ArduinoJson 6.21.5** (no v7)
4. Abre la carpeta `prototipo1` (los tres archivos juntos).
5. Placa: **ESP32 Dev Module** (no ESP32P4).
6. Puerto: **Silicon Labs CP210x** (ej. COM3). Nunca COM1 ni Bluetooth.
7. Upload speed **115200**.
8. Cierra Monitor Serie y `servidor.py` → Subir.
9. Al boot: LED + pitido + giro corto del motor (self-test). MQ135 calienta ~30 s.

Si falla el flash: desconecta el rojo de 5V (o cables), graba, reconecta, pulsa **EN**.

### 2. Localhost (probar en el PC)

```powershell
py -m pip install pyserial
py servidor\servidor.py
```

Abre **http://127.0.0.1:8080**

- **Arriba**: medición real (AM2302 + MQ135). El supuesto no la reemplaza.
- **Abajo**: supuestos de prueba → mandan `SUPUESTO t h ppm` o `REAL` al ESP32.

Cierra el Monitor Serie de Arduino antes de abrir el servidor (si no: Acceso denegado en el COM).

### 3. WiFi (opcional)

En `FreshSenseNode.h`, cambia `WIFI_SSID` / `WIFI_PASSWORD`. Si dejas `TU_RED_WIFI`, sensores y actuadores locales igual funcionan.

## Diagrama de clases

Ver [docs/diagrama-de-clases.md](docs/diagrama-de-clases.md) y [docs/class-diagram.puml](docs/class-diagram.puml).

## Licencia / curso

Prototipo del curso asociado a la organización **1ASI0572-2620-8725**.
