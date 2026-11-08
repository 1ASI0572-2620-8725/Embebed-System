# =============================================================================
# servidor.py — localhost FreshSense prototipo 1
# =============================================================================
# Qué hace:
#   1) Abre el COM del ESP32 (CP2102, 115200) y lee el JSON SensorReading.
#   2) Sirve la página en http://127.0.0.1:8080
#   3) Arriba: medición REAL (AM2302 pin 4 + MQ135 pin 34, cada ~2 s)
#   4) Abajo: supuesto — manda "SUPUESTO t h ppm" o "REAL" por USB
#      El supuesto mueve LED/buzzer/motor; NO reemplaza la medición de arriba.
#
# Alertas = estandar FS-FRUIT-1 (misma tabla que FreshSenseNode.h).
#   Independiente: basta UN sensor en SEMI o CRITICO.
#   SEMI   → AT_RISK → LED continuo
#   CRITICO → SPOILED → LED parpadeo + buzzer
#
# Pines: AM2302=4 | MQ135=34 | LED=27 | Buzzer=25 | Rele=26
#
# Uso:
#   py -m pip install pyserial
#   py servidor\servidor.py
# =============================================================================

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PUERTO = 8080

# =============================================================================
# ESTANDAR FS-FRUIT-1 — refrigeracion generica de frutas (cualquier tipo)
# =============================================================================
ESTANDAR = {
    "id": "FS-FRUIT-1",
    "nombre": "Refrigeracion generica de frutas (FAO / UC Davis)",
    "lecturas": {
        "clima_segundos": 180,
        "etileno_segundos": 60,
        "nota": "AM2302 temp+hum cada 3 min; MQ135 etileno cada 1 min",
    },
    "actuadores": {
        "led": "temperatura y humedad",
        "ventilador": "etileno",
        "buzzer": "con LED en CRITICO de temp/hum",
    },
    "temperatura": {
        "normal": {"min": 0.0, "max": 8.0},
        "semi": {"bajo_min": -1.0, "alto_max": 12.0},
        "critico": {"bajo_max_excl": -1.0, "alto_min_excl": 12.0},
        "unidad": "C",
        "descripcion": "NORMAL 0..8 | SEMI (-1..0) o (8..12] | CRITICO < -1 o > 12",
    },
    "humedad": {
        "normal": {"min": 90.0, "max": 95.0},
        "semi": {"bajo_min": 85.0, "alto_max": 98.0},
        "critico": {"bajo_max_excl": 85.0, "alto_min_excl": 98.0},
        "unidad": "%",
        "descripcion": "NORMAL 90..95 | SEMI 85..90 o 95..98 | CRITICO < 85 o > 98",
    },
    "etileno": {
        "normal": {"min": 0.0, "max": 1.0},
        "semi": {"max": 5.0},
        "critico": {"min_excl": 5.0},
        "unidad": "ppm",
        "descripcion": "NORMAL 0..1 (FAO) | SEMI >1..5 | CRITICO > 5 → ventilador",
    },
}

# Compat nombres viejos (ventilador / docs)
UMBRAL_TEMPERATURA_C = ESTANDAR["temperatura"]["normal"]["max"]
UMBRAL_ETILENO_PPM = ESTANDAR["etileno"]["semi"]["max"]


def nivel_temperatura(t):
    t = float(t)
    if t > 12.0 or t < -1.0:
        return "CRITICO"
    if t > 8.0 or t < 0.0:
        return "SEMI"
    return "OK"


def nivel_humedad(h):
    h = float(h)
    if h < 85.0 or h > 98.0:
        return "CRITICO"
    if h < 90.0 or h > 95.0:
        return "SEMI"
    return "OK"


def nivel_etileno(e):
    e = float(e)
    if e > 5.0:
        return "CRITICO"
    if e > 1.0:
        return "SEMI"
    return "OK"

# Estado compartido entre el hilo USB y el navegador.
candado = threading.Lock()
estado = {
    "conectado": False,
    "puerto": "",
    "lectura": None,  # medicion REAL (SensorReading del ESP32)
    # --- SUPUESTO (apartado aparte; no reemplaza "lectura") ---
    "supuesto": {
        "activo": False,
        "nombre": "",
        "temperatureC": 4.0,
        "humidityPct": 92.0,
        "ethylenePpm": 2.0,
    },
}
puerto_serial = None


def decidir(temperatura, humedad, etileno):
    """FS-FRUIT-1: LED<-temp/hum | ventilador<-etileno. Independiente por sensor."""
    n_t = nivel_temperatura(temperatura)
    n_h = nivel_humedad(humedad)
    n_e = nivel_etileno(etileno)
    orden = {"OK": 0, "SEMI": 1, "CRITICO": 2}
    peor_clima = max((n_t, n_h), key=lambda n: orden[n])
    peor = max((peor_clima, n_e), key=lambda n: orden[n])
    ventilador = n_e != "OK"  # gas SEMI o CRITICO

    if peor_clima == "CRITICO":
        led = "parpadeo"
        buzzer = True
    elif peor_clima == "SEMI":
        led = "continuo"
        buzzer = False
    else:
        led = "apagado"
        buzzer = False

    if peor == "CRITICO":
        frescura = "SPOILED"
    elif peor == "SEMI":
        frescura = "AT_RISK"
    else:
        frescura = "FRESH"

    return {
        "highEthylene": n_e != "OK",
        "tempRisk": n_t != "OK",
        "humidityRisk": n_h != "OK",
        "niveles": {
            "temperatura": n_t,
            "humedad": n_h,
            "etileno": n_e,
        },
        "frescura": frescura,
        "led": led,
        "buzzer": buzzer,
        "ventilador": ventilador,
        "humidityPct": float(humedad),
    }


def lectura_visible():
    """JSON que consume la página: enVivo (sensores) y supuesto (aparte)."""
    with candado:
        # --- SUPUESTO: copia para no mezclar con la medicion real ---
        supuesto = dict(estado["supuesto"])
        lectura = estado["lectura"]
        conectado = estado["conectado"]
        puerto = estado["puerto"]
    # --- SUPUESTO: calcula FRESH/AT_RISK/SPOILED solo para el panel de abajo ---
    if supuesto["activo"]:
        supuesto["alertas"] = decidir(
            supuesto["temperatureC"],
            supuesto["humidityPct"],
            supuesto["ethylenePpm"],
        )
    else:
        supuesto["alertas"] = None
    return {
        "conectado": conectado,
        "puerto": puerto,
        "enVivo": bloque_en_vivo(lectura),  # ARRIBA: sensores reales
        "supuesto": supuesto,                 # ABAJO: supuesto (no tapa enVivo)
        "pines": {
            "am2302": 4,
            "mq135": 34,
            "led": 27,
            "buzzer": 25,
            "rele": 26,
            "relayActiveLow": False,
            "buzzerActiveLow": True,
        },
        "umbrales": {
            "temperatureC": UMBRAL_TEMPERATURA_C,
            "ethylenePpm": UMBRAL_ETILENO_PPM,
        },
        "estandar": ESTANDAR,
    }


def bloque_en_vivo(lectura):
    """Bloque de medición real para el panel de arriba."""
    if not lectura:
        return None
    temperatura = float(lectura.get("temperatureC", 0))
    humedad = float(lectura.get("humidityPct", 0))
    etileno = float(lectura.get("ethylenePpm", 0))
    return {
        "temperatureC": temperatura,
        "humidityPct": humedad,
        "ethylenePpm": etileno,
        "cuando": lectura.get("timestamp", ""),
        "alertas": decidir(temperatura, humedad, etileno),
    }


# =============================================================================
# --- AQUI VA EL SUPUESTO (localhost) ---
# Guarda el caso de prueba y manda "SUPUESTO t h ppm" o "REAL" al ESP32.
# No toca estado["lectura"] / enVivo (medicion real).
# En la placa lo recibe FreshSenseSupuesto.h
# =============================================================================
def aplicar_supuesto(cuerpo):
    """Guarda el supuesto y lo envía a la placa. No toca enVivo."""
    with candado:
        previo = dict(estado["supuesto"])
        if bool(cuerpo.get("activo")):
            estado["supuesto"] = {
                "activo": True,
                "nombre": str(cuerpo.get("nombre", "")),
                "temperatureC": float(cuerpo.get("temperatureC", previo["temperatureC"])),
                "humidityPct": float(cuerpo.get("humidityPct", previo["humidityPct"])),
                "ethylenePpm": float(cuerpo.get("ethylenePpm", previo["ethylenePpm"])),
            }
        else:
            previo["activo"] = False
            previo["nombre"] = ""
            estado["supuesto"] = previo
        actual = dict(estado["supuesto"])
    enviar_a_la_placa(actual)  # --- SUPUESTO: escribe por USB ---
    return lectura_visible()


def guardar_lectura(lectura):
    """Guarda un SensorReading que llegó por USB (o POST de prueba)."""
    with candado:
        estado["lectura"] = lectura
        estado["conectado"] = True


# --- SUPUESTO: linea USB hacia FreshSenseSupuesto.h en el ESP32 ---
def enviar_a_la_placa(supuesto):
    """Escribe SUPUESTO t h ppm\\n o REAL\\n al ESP32 por USB."""
    global puerto_serial
    if puerto_serial is None:
        return
    if supuesto["activo"]:
        linea = "SUPUESTO %.1f %.1f %.1f\n" % (
            supuesto["temperatureC"],
            supuesto["humidityPct"],
            supuesto["ethylenePpm"],
        )
    else:
        linea = "REAL\n"
    try:
        puerto_serial.write(linea.encode("utf-8"))
        puerto_serial.flush()
    except Exception:
        cerrar_puerto()


def cerrar_puerto():
    """Cierra el COM y marca desconectado."""
    global puerto_serial
    if puerto_serial is not None:
        try:
            puerto_serial.close()
        except Exception:
            pass
        puerto_serial = None
    with candado:
        estado["conectado"] = False
        estado["puerto"] = ""


def _prioridad_puerto(encontrado):
    """Prioriza Silicon Labs CP210x; evita COM1 y Bluetooth."""
    nombre = (encontrado.device or "").upper()
    desc = (encontrado.description or "").lower()
    manuf = (getattr(encontrado, "manufacturer", None) or "").lower()
    hwid = (getattr(encontrado, "hwid", None) or "").lower()
    blob = " ".join([nombre, desc, manuf, hwid])
    if "bluetooth" in blob or nombre == "COM1":
        return -1
    if "cp210" in blob or "silicon" in blob or "10c4" in hwid:
        return 100
    if "usb" in blob and "serial" in blob:
        return 50
    if nombre.startswith("COM"):
        return 10
    return 0


def buscar_puerto():
    """Abre el primer COM válido del ESP32 a 115200 (prefiere CP210x)."""
    import serial
    from serial.tools import list_ports

    global puerto_serial
    candidatos = sorted(
        list_ports.comports(),
        key=_prioridad_puerto,
        reverse=True,
    )
    for encontrado in candidatos:
        if _prioridad_puerto(encontrado) < 0:
            continue
        nombre = encontrado.device
        try:
            serie = serial.Serial(nombre, 115200, timeout=0.1)
        except Exception:
            continue
        puerto_serial = serie
        with candado:
            estado["conectado"] = True
            estado["puerto"] = nombre
        print("USB abierto:", nombre, "-", encontrado.description or "")
        return


def hilo_serial():
    """Hilo daemon: busca COM y parsea JSON SensorReading del firmware."""
    buffer = ""
    while True:
        if puerto_serial is None:
            try:
                buscar_puerto()
            except Exception:
                pass
            time.sleep(1.5)
            continue
        try:
            trozo = puerto_serial.read(512)
        except Exception:
            cerrar_puerto()
            buffer = ""
            time.sleep(1)
            continue
        if not trozo:
            continue
        buffer += trozo.decode("utf-8", errors="ignore")
        if len(buffer) > 8000:
            buffer = buffer[-4000:]
        # El firmware imprime JSON pretty; re.S permite saltos de línea.
        for bloque in re.findall(r"\{.*?\}", buffer, flags=re.S):
            try:
                dato = json.loads(bloque)
            except json.JSONDecodeError:
                continue
            # Solo SensorReading real (tiene deviceId + temperatureC).
            if "temperatureC" not in dato or "deviceId" not in dato:
                continue
            if "supuesto=1" in str(dato.get("meta", "")):
                continue
            guardar_lectura(dato)
            buffer = buffer[buffer.rfind(bloque) + len(bloque) :]


class Sitio(BaseHTTPRequestHandler):
    """HTTP: GET / y /api/estado ; POST /api/supuesto."""

    def log_message(self, formato, *args):
        return

    def enviar(self, codigo, cuerpo, tipo):
        datos = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        if self.path.startswith("/api/estado"):
            self.enviar(200, json.dumps(lectura_visible()), "application/json")
            return
        self.enviar(200, PAGINA, "text/html; charset=utf-8")

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", "0"))
        crudo = self.rfile.read(largo).decode("utf-8", errors="replace")
        try:
            cuerpo = json.loads(crudo) if crudo else {}
        except json.JSONDecodeError:
            self.enviar(400, '{"error":"json"}', "application/json")
            return
        # --- SUPUESTO: POST /api/supuesto (botones de la pagina) ---
        if self.path.startswith("/api/supuesto"):
            self.enviar(200, json.dumps(aplicar_supuesto(cuerpo)), "application/json")
            return
        if "temperatureC" in cuerpo:
            guardar_lectura(cuerpo)
            self.enviar(201, '{"ok":true}', "application/json")
            return
        self.enviar(404, '{"error":"ruta"}', "application/json")


PAGINA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prototipo 1 · FreshSense</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; font-family: "Segoe UI", sans-serif; background: #e7eef0; color: #1b2830; }
  header { background: #16343c; color: #f4fbfb; padding: 28px 32px; }
  header p { margin: 8px 0 0; max-width: 820px; line-height: 1.45; }
  main { max-width: 980px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
  section { background: #fff; border-radius: 12px; padding: 18px 20px; }
  section.aparte { border: 2px solid #1d6f8a; }
  h2 { margin: 0 0 12px; font-size: 18px; }
  .rejilla { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .dato { background: #f3f7f8; border-radius: 10px; padding: 14px; }
  .dato span { display: block; font-size: 13px; color: #52636b; }
  .dato strong { font-size: 28px; }
  .frescura { font-size: 22px; font-weight: 650; }
  .FRESH { color: #1d7a45; }
  .AT_RISK { color: #a15c00; }
  .SPOILED { color: #b42318; }
  .activos { display: flex; gap: 8px; flex-wrap: wrap; }
  .pastilla { border-radius: 999px; padding: 6px 12px; background: #e6ebed; }
  .pastilla.encendido { background: #b42318; color: white; }
  .pastilla.parpadeo { background: #b42318; color: white; outline: 2px dashed #5c0f0a; }
  .pastilla.aire { background: #1d6f8a; color: white; }
  button, .supuestos button { border: 0; border-radius: 8px; padding: 10px 12px; background: #1d6f8a; color: white; cursor: pointer; }
  .supuestos { display: flex; gap: 8px; flex-wrap: wrap; }
  .secundario { background: #d7e1e4; color: #16343c; }
  label { display: grid; gap: 4px; font-size: 14px; }
  input { width: 100%; }
  .nota { color: #52636b; font-size: 14px; line-height: 1.45; }
  ul { margin: 0; padding-left: 18px; line-height: 1.5; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e1e8ea; vertical-align: top; }
  th { color: #52636b; font-weight: 600; }
  code { background: #f3f7f8; padding: 1px 5px; border-radius: 4px; }
  @media (max-width: 700px) { .rejilla { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Prototipo 1 · FreshSense</h1>
  <p>Nodo de cámara de frío. Arriba: AM2302 (GPIO 4) + MQ135 (GPIO 34) en tiempo real.
  Abajo: supuesto por USB — no sustituye la medición. Relé con <code>RELAY_ACTIVE_LOW = false</code>.</p>
</header>
<main>
  <section>
    <h2>Placa</h2>
    <p class="nota" id="placa">Buscando el ESP32 (Silicon Labs CP210x) en el USB.</p>
  </section>

  <section>
    <h2>Medición en tiempo real</h2>
    <p class="nota">Sale del AM2302 y del MQ135 cada ~2 s. El supuesto de abajo no cambia estos números.
    En habitación ~26 °C es normal ver AT_RISK y ventilador ON (&gt; 8 °C). Etileno ~0 ppm en aire limpio tras calibración es normal.</p>
    <div class="rejilla">
      <div class="dato"><span>Temperatura</span><strong id="temp">-</strong></div>
      <div class="dato"><span>Humedad</span><strong id="hum">-</strong></div>
      <div class="dato"><span>Etileno</span><strong id="gas">-</strong></div>
    </div>
    <p class="frescura" id="frescura">Esperando sensores</p>
    <p class="nota" id="cuando"></p>
    <div class="activos" id="activos"></div>
  </section>

  <!-- ========== AQUI VA EL SUPUESTO (HTML) ========== -->
  <section class="aparte">
    <h2>Supuesto de prueba</h2>
    <p class="nota">Manda <code>SUPUESTO temp hum ppm</code> al ESP32 (FreshSenseSupuesto.h). Solo mueve LED (27), buzzer (25) y motor vía relé (26).
    La medición de arriba sigue siendo la de los sensores. <code>Quitar supuesto</code> manda <code>REAL</code>.</p>
    <div class="supuestos">
      <button data-t="4" data-h="92" data-g="2" data-n="Cámara fría">Cámara fría (FRESH)</button>
      <button data-t="10" data-h="92" data-g="2" data-n="Frio semi">Frio semi (AT_RISK)</button>
      <button data-t="4" data-h="92" data-g="3" data-n="Etileno semi">Etileno semi (AT_RISK)</button>
      <button data-t="4" data-h="87" data-g="2" data-n="Humedad semi">Humedad semi (AT_RISK)</button>
      <button data-t="14" data-h="92" data-g="2" data-n="Frio critico">Frio critico (SPOILED)</button>
      <button data-t="4" data-h="92" data-g="8" data-n="Etileno critico">Etileno critico (SPOILED)</button>
      <button data-t="10" data-h="87" data-g="3" data-n="Varios semi">Varios semi (sigue AT_RISK)</button>
      <button class="secundario" id="real">Quitar supuesto</button>
    </div>
    <div class="rejilla" style="margin-top:12px">
      <label>Temperatura °C<input id="in-temp" type="number" value="4" step="0.1"></label>
      <label>Humedad %<input id="in-hum" type="number" value="92" step="0.1"></label>
      <label>Etileno ppm<input id="in-gas" type="number" value="2" step="0.1"></label>
    </div>
    <p><button id="aplicar">Aplicar este supuesto</button></p>
    <p class="frescura" id="sup-frescura">Sin supuesto</p>
    <div class="rejilla">
      <div class="dato"><span>Temperatura del supuesto</span><strong id="sup-temp">-</strong></div>
      <div class="dato"><span>Humedad del supuesto</span><strong id="sup-hum">-</strong></div>
      <div class="dato"><span>Etileno del supuesto</span><strong id="sup-gas">-</strong></div>
    </div>
    <div class="activos" id="sup-activos"></div>
  </section>

  <section>
    <h2>Estandar FS-FRUIT-1 (cualquier fruta)</h2>
    <p class="nota">Misma tabla que el firmware. SEMI = se va a malograr. CRITICO = ya se malogro (o 2 SEMI).</p>
    <table>
      <thead><tr><th>Sensor</th><th>NORMAL</th><th>SEMI-CRITICO</th><th>CRITICO</th></tr></thead>
      <tbody>
        <tr><td>Temperatura (cada 3 min)</td><td>0 .. 8 °C</td><td>(-1 .. 0) o (8 .. 12] °C</td><td>&lt; -1 o &gt; 12 °C</td></tr>
        <tr><td>Humedad (cada 3 min)</td><td>90 .. 95 %</td><td>85 .. 90 o 95 .. 98 %</td><td>&lt; 85 o &gt; 98 %</td></tr>
        <tr><td>Etileno (cada 1 min)</td><td>0 .. 1 ppm</td><td>&gt; 1 .. 5 ppm</td><td>&gt; 5 ppm</td></tr>
      </tbody>
    </table>
    <ul>
      <li>Lecturas: clima (temp+hum) cada <strong>3 min</strong>; etileno cada <strong>1 min</strong>.</li>
      <li><strong>LED</strong> solo por temp/humedad. <strong>Ventilador</strong> solo por etileno (&gt;1 ppm).</li>
      <li>Independiente: un solo sensor SEMI/CRITICO ya avisa.</li>
      <li>AM2302 GPIO 4 · MQ135 GPIO 34 · LED 27 · Buzzer 25 · Relé 26</li>
    </ul>
    <p class="nota" id="niveles-vivo"></p>
  </section>

  <section>
    <h2>Tabla de conexiones (fuente de verdad)</h2>
    <p class="nota">Raya roja lado j = 5V. Raya roja lado a = 3.3V. Nunca unir esas dos. Azules = GND (unir con morado MM).</p>
    <table>
      <thead><tr><th>Color</th><th>Desde</th><th>Hasta</th></tr></thead>
      <tbody>
        <tr><td>Rojo HM</td><td>ESP32 5V</td><td>fila 1, raya roja lado j</td></tr>
        <tr><td>Naranja HM</td><td>ESP32 3V3</td><td>fila 1, raya roja lado a</td></tr>
        <tr><td>Negro HM</td><td>ESP32 GND</td><td>fila 1, raya azul lado j</td></tr>
        <tr><td>Morado MM</td><td>fila 2 azul j</td><td>fila 1 azul a</td></tr>
        <tr><td>Naranja HM</td><td>AM2302 VCC</td><td>fila 2, raya roja lado a</td></tr>
        <tr><td>Negro HM</td><td>AM2302 GND</td><td>fila 3, raya azul lado j</td></tr>
        <tr><td>Verde HM</td><td>AM2302 DAT ↔ ESP32 pin 4</td><td>a6 / b6</td></tr>
        <tr><td>Rojo HM</td><td>MQ135 VCC</td><td>fila 2, raya roja lado j</td></tr>
        <tr><td>Negro HM</td><td>MQ135 GND</td><td>fila 4, raya azul lado j</td></tr>
        <tr><td>Blanco + 220Ω</td><td>MQ135 AO ↔ pin 34</td><td>a18 — e18–f18 — g18</td></tr>
        <tr><td>Amarillo + 220Ω</td><td>pin 27 ↔ LED</td><td>a20 — e20–f20 — g20 (pata larga)</td></tr>
        <tr><td>Morado HM</td><td>LED pata corta</td><td>fila 5 azul lado j</td></tr>
        <tr><td>Rojo / Negro / Verde</td><td>Buzzer VCC / GND / I/O</td><td>roja j fila3 / azul j fila6 / a25↔pin25</td></tr>
        <tr><td>Rojo / Gris / Azul</td><td>Relé + / − / IN</td><td>roja j fila4 / azul j fila7 / a27↔pin26</td></tr>
        <tr><td>Marrón / Gris</td><td>Motor vía relé</td><td>COM←5V ; NO→chapa izq ; chapa der→GND fila8</td></tr>
      </tbody>
    </table>
    <p class="nota">NC vacío. DO del MQ135 vacío. Self-test al boot: LED + pitido + giro corto del motor.</p>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);

function textoLed(led) {
  if (led === "continuo") return "continuo (se va a malograr)";
  if (led === "parpadeo") return "parpadeo (ya se malogro)";
  if (led === true) return "encendido"; // compat
  return "apagado";
}

function pastillas(alertas) {
  const ledClase = alertas.led === "parpadeo" ? "parpadeo"
    : (alertas.led === "continuo" || alertas.led === true) ? "encendido" : "";
  const partes = [
    ["LED", textoLed(alertas.led), ledClase],
    ["Buzzer", alertas.buzzer ? "encendido" : "apagado", alertas.buzzer ? "encendido" : ""],
    ["Ventilador", alertas.ventilador ? "encendido" : "apagado", alertas.ventilador ? "aire" : ""]
  ];
  return partes.map(([nombre, texto, clase]) =>
    '<span class="pastilla ' + clase + '">' + nombre + ": " + texto + "</span>"
  ).join("");
}

function pintar(dato) {
  $("placa").textContent = dato.conectado
    ? "Placa en " + dato.puerto + " (CP210x). Medición arriba = sensores. Relé activo-HIGH (RELAY_ACTIVE_LOW false)."
    : "ESP32 no está en el USB o el COM está ocupado. Conecta Silicon Labs CP210x, cierra el Monitor Serie y reinicia este servidor.";
  const vivo = dato.enVivo;
  if (!vivo) {
    $("temp").textContent = "-";
    $("hum").textContent = "-";
    $("gas").textContent = "-";
    $("frescura").textContent = "Esperando sensores";
    $("frescura").className = "frescura";
    $("cuando").textContent = "Si lleva rato así: revisa 5V (MQ135), 3V3 (AM2302), DAT→4, AO→34, y pulsa EN.";
    $("activos").innerHTML = "";
    if ($("niveles-vivo")) $("niveles-vivo").textContent = "";
  } else {
    $("temp").textContent = vivo.temperatureC.toFixed(1) + " °C";
    $("hum").textContent = vivo.humidityPct.toFixed(1) + " %";
    $("gas").textContent = vivo.ethylenePpm.toFixed(1) + " ppm";
    $("frescura").textContent = vivo.alertas.frescura;
    $("frescura").className = "frescura " + vivo.alertas.frescura;
    $("cuando").textContent = vivo.cuando ? "Última medición: " + vivo.cuando : "";
    if (vivo.alertas.niveles) {
      const n = vivo.alertas.niveles;
      $("niveles-vivo").textContent =
        "Niveles ahora → temp: " + n.temperatura +
        " · humedad: " + n.humedad +
        " · etileno: " + n.etileno;
    }
    if (dato.supuesto.activo) {
      $("activos").innerHTML = "";
      $("cuando").textContent += " Actuadores siguen el supuesto de abajo.";
    } else {
      $("activos").innerHTML = pastillas(vivo.alertas);
    }
  }
  const supuesto = dato.supuesto;
  if (!supuesto.activo || !supuesto.alertas) {
    $("sup-temp").textContent = "-";
    $("sup-hum").textContent = "-";
    $("sup-gas").textContent = "-";
    $("sup-frescura").textContent = "Sin supuesto. Los actuadores siguen la medición real.";
    $("sup-frescura").className = "frescura";
    $("sup-activos").innerHTML = "";
    return;
  }
  $("sup-temp").textContent = supuesto.temperatureC.toFixed(1) + " °C";
  $("sup-hum").textContent = supuesto.humidityPct.toFixed(1) + " %";
  $("sup-gas").textContent = supuesto.ethylenePpm.toFixed(1) + " ppm";
  $("sup-frescura").textContent = supuesto.nombre + ": " + supuesto.alertas.frescura;
  $("sup-frescura").className = "frescura " + supuesto.alertas.frescura;
  $("sup-activos").innerHTML = pastillas(supuesto.alertas);
}

async function pedir() {
  const respuesta = await fetch("/api/estado");
  pintar(await respuesta.json());
}

// --- SUPUESTO: envia el caso de prueba al servidor / ESP32 ---
async function mandar(cuerpo) {
  const respuesta = await fetch("/api/supuesto", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(cuerpo)
  });
  pintar(await respuesta.json());
}

// --- SUPUESTO: botones Camara fria / gas / calor / etc. ---
document.querySelectorAll(".supuestos button[data-t]").forEach((boton) => {
  boton.addEventListener("click", () => {
    $("in-temp").value = boton.dataset.t;
    $("in-hum").value = boton.dataset.h;
    $("in-gas").value = boton.dataset.g;
    mandar({
      activo: true,
      nombre: boton.dataset.n,
      temperatureC: Number(boton.dataset.t),
      humidityPct: Number(boton.dataset.h),
      ethylenePpm: Number(boton.dataset.g)
    });
  });
});

$("aplicar").addEventListener("click", () => {
  mandar({
    activo: true,
    nombre: "Numeros elegidos",
    temperatureC: Number($("in-temp").value),
    humidityPct: Number($("in-hum").value),
    ethylenePpm: Number($("in-gas").value)
  });
});

$("real").addEventListener("click", () => mandar({ activo: false }));
setInterval(pedir, 1000);
pedir();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    hilo = threading.Thread(target=hilo_serial, daemon=True)
    hilo.start()
    print("FreshSense prototipo 1 → http://%s:%d" % (HOST, PUERTO))
    print("Cierra el Monitor Serie de Arduino. Prefiere COM Silicon Labs CP210x.")
    print("Pines: AM2302=4 MQ135=34 LED=27 Buzzer=25 Rele=26 (RELAY_ACTIVE_LOW false)")
    ThreadingHTTPServer((HOST, PUERTO), Sitio).serve_forever()
