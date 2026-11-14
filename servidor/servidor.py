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
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PUERTO = 8080

# --- EDGE: cada lectura REAL que llega por USB se reenvia a Freshsense-EDGE ---
# Cambia con la variable de entorno FRESHSENSE_EDGE_URL si hace falta.
EDGE_URL = os.environ.get(
    "FRESHSENSE_EDGE_URL", "http://127.0.0.1:5000/api/v1/sensor-readings"
)
EDGE_TOKEN = os.environ.get("FRESHSENSE_EDGE_TOKEN", "replace-with-device-token")
DEVICE_ID = "00000000-0000-0000-0000-000000000001"
EDGE_ACTIVO = os.environ.get("FRESHSENSE_EDGE", "1") != "0"
edge_estado = {"ultimo_codigo": None, "ultimo_error": "", "enviadas": 0, "ultima_hora": ""}

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
        "led": "el peor de temperatura, humedad y etileno",
        "buzzer": "solo si ese peor valor es critico",
        "ventilador": "solo etileno: semi medio, critico al maximo",
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
serial_lock = threading.Lock()
comando_lock = threading.Lock()
estado = {
    "conectado": False,
    "puerto": "",
    "revision": 0,
    "ack": "",
    "ack_en": 0.0,
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
    """LED y buzzer = el peor de los tres. Ventilador solo por etileno."""
    n_t = nivel_temperatura(temperatura)
    n_h = nivel_humedad(humedad)
    n_e = nivel_etileno(etileno)
    orden = {"OK": 0, "SEMI": 1, "CRITICO": 2}
    peor_clima = max((n_t, n_h), key=lambda n: orden[n])
    peor = max((peor_clima, n_e), key=lambda n: orden[n])
    ventilador = n_e != "OK"

    if peor == "CRITICO":
        led = "parpadeo"
        buzzer = True
    elif peor == "SEMI":
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
        "velocidad": "maxima" if n_e == "CRITICO" else ("media" if n_e == "SEMI" else "apagado"),
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
        revision = estado["revision"]
        ack = estado.get("ack") or ""
        ack_en = estado.get("ack_en") or 0.0
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
        "revision": revision,
        "placa": {
            "confirmo": _ack_vigente(supuesto, ack, ack_en),
            "ack": ack,
        },
        "pines": {
            "am2302": 4,
            "mq135": 34,
            "led": 27,
            "buzzer": 25,
            "rele": 26,
            "relayActiveLow": True,
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
def redondear(valor, respaldo):
    try:
        return round(float(valor), 1)
    except (TypeError, ValueError):
        return round(float(respaldo), 1)


def _ack_esperado(supuesto):
    if supuesto.get("activo"):
        return "ACK SUPUESTO %.1f %.1f %.1f" % (
            supuesto["temperatureC"],
            supuesto["humidityPct"],
            supuesto["ethylenePpm"],
        )
    return "ACK REAL"


def _ack_vigente(supuesto, ack, ack_en, ventana=8.0):
    if not ack or (time.time() - float(ack_en or 0)) > ventana:
        return False
    return ack.strip() == _ack_esperado(supuesto)


def aplicar_supuesto(cuerpo):
    """Guarda el supuesto, lo manda a la placa y espera su ACK. No toca enVivo."""
    with comando_lock:
        with candado:
            previo = dict(estado["supuesto"])
            if bool(cuerpo.get("activo")):
                estado["supuesto"] = {
                    "activo": True,
                    "nombre": str(cuerpo.get("nombre", "")).strip() or "Supuesto",
                    "temperatureC": redondear(cuerpo.get("temperatureC"), previo["temperatureC"]),
                    "humidityPct": redondear(cuerpo.get("humidityPct"), previo["humidityPct"]),
                    "ethylenePpm": redondear(cuerpo.get("ethylenePpm"), previo["ethylenePpm"]),
                }
            else:
                previo["activo"] = False
                previo["nombre"] = ""
                estado["supuesto"] = previo
            estado["revision"] += 1
            actual = dict(estado["supuesto"])
        escribir_placa(comando_de(actual))
        limite = time.time() + 1.6
        proximo = time.time() + 0.35
        while time.time() < limite:
            with candado:
                if _ack_vigente(actual, estado.get("ack") or "", estado.get("ack_en") or 0):
                    break
            time.sleep(0.05)
            if time.time() >= proximo:
                escribir_placa(comando_de(actual))
                proximo = time.time() + 0.35
    if actual["activo"]:
        enviar_al_edge(lectura_de_supuesto(actual))
    return lectura_visible()


def lectura_de_supuesto(supuesto):
    """Arma el SensorReading del caso de prueba para que el Edge lo guarde."""
    alertas = decidir(
        supuesto["temperatureC"],
        supuesto["humidityPct"],
        supuesto["ethylenePpm"],
    )
    nombre = str(supuesto.get("nombre") or "supuesto").replace(";", " ")
    return {
        "deviceId": DEVICE_ID,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "temperatureC": float(supuesto["temperatureC"]),
        "humidityPct": float(supuesto["humidityPct"]),
        "ethylenePpm": float(supuesto["ethylenePpm"]),
        "meta": "origen=supuesto;nombre=%s;frescura=%s;led=%s;buzzer=%s;ventilador=%s;velocidad=%s"
        % (
            nombre,
            alertas["frescura"],
            alertas["led"],
            "on" if alertas["buzzer"] else "off",
            "on" if alertas["ventilador"] else "off",
            alertas["velocidad"],
        ),
    }


def completar_para_edge(lectura):
    """Agrega frescura y actuadores para que el Edge guarde el mismo estado que la página."""
    dato = dict(lectura)
    if "frescura=" in str(dato.get("meta") or ""):
        return dato
    try:
        alertas = decidir(
            float(dato["temperatureC"]),
            float(dato["humidityPct"]),
            float(dato["ethylenePpm"]),
        )
    except (KeyError, TypeError, ValueError):
        return dato
    extra = "frescura=%s;led=%s;buzzer=%s;ventilador=%s;velocidad=%s" % (
        alertas["frescura"],
        alertas["led"],
        "on" if alertas["buzzer"] else "off",
        "on" if alertas["ventilador"] else "off",
        alertas["velocidad"],
    )
    previo = str(dato.get("meta") or "").strip(";")
    dato["meta"] = extra if not previo else previo + ";" + extra
    return dato


def guardar_lectura(lectura):
    """Guarda un SensorReading que llegó por USB (o POST de prueba)."""
    with candado:
        estado["lectura"] = lectura
        estado["conectado"] = True


# --- EDGE: POST /api/v1/sensor-readings (mismo JSON que arma el firmware) ---
def enviar_al_edge(lectura):
    """Reenvia la lectura real al Edge local sin bloquear el hilo USB."""
    if not EDGE_ACTIVO:
        return

    def _enviar():
        try:
            cuerpo = json.dumps(completar_para_edge(lectura)).encode("utf-8")
            peticion = urllib.request.Request(
                EDGE_URL,
                data=cuerpo,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Device-Token": EDGE_TOKEN,
                },
            )
            with urllib.request.urlopen(peticion, timeout=5) as resp:
                edge_estado["ultimo_codigo"] = resp.status
                edge_estado["ultimo_error"] = ""
        except urllib.error.HTTPError as exc:
            edge_estado["ultimo_codigo"] = exc.code
            edge_estado["ultimo_error"] = exc.read().decode("utf-8", "ignore")[:200]
        except Exception as exc:
            edge_estado["ultimo_codigo"] = None
            edge_estado["ultimo_error"] = str(exc)
        edge_estado["enviadas"] += 1
        edge_estado["ultima_hora"] = time.strftime("%H:%M:%S")
        print(
            "EDGE <- lectura temp=%s hum=%s eth=%s -> HTTP %s %s"
            % (
                lectura.get("temperatureC"),
                lectura.get("humidityPct"),
                lectura.get("ethylenePpm"),
                edge_estado["ultimo_codigo"],
                edge_estado["ultimo_error"],
            )
        )

    threading.Thread(target=_enviar, daemon=True).start()


# --- SUPUESTO: linea USB hacia FreshSenseSupuesto.h en el ESP32 ---
def comando_de(supuesto):
    """Linea USB que entiende FreshSenseSupuesto.h."""
    if supuesto.get("activo"):
        return "SUPUESTO %.1f %.1f %.1f\n" % (
            supuesto["temperatureC"],
            supuesto["humidityPct"],
            supuesto["ethylenePpm"],
        )
    return "REAL\n"


def escribir_placa(linea):
    """Escribe una linea al ESP32. El hilo USB lee el ACK."""
    global puerto_serial
    with serial_lock:
        serie = puerto_serial
        if serie is None:
            return False
        try:
            serie.write(linea.encode("utf-8"))
            serie.flush()
            return True
        except Exception:
            serie_rota = serie
    try:
        serie_rota.close()
    except Exception:
        pass
    with serial_lock:
        if puerto_serial is serie_rota:
            puerto_serial = None
    with candado:
        estado["conectado"] = False
        estado["puerto"] = ""
    return False


def mantener_placa():
    """Repite el caso vigente para que un reset del ESP32 no vuelva solo a los sensores."""
    while True:
        time.sleep(3)
        with comando_lock:
            with candado:
                actual = dict(estado["supuesto"])
                ack = estado.get("ack") or ""
                ack_en = estado.get("ack_en") or 0
            if _ack_vigente(actual, ack, ack_en):
                continue
            escribir_placa(comando_de(actual))


def cerrar_puerto():
    """Cierra el COM y marca desconectado."""
    global puerto_serial
    with serial_lock:
        serie = puerto_serial
        puerto_serial = None
    if serie is not None:
        try:
            serie.close()
        except Exception:
            pass
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
        with serial_lock:
            puerto_serial = serie
        with candado:
            estado["conectado"] = True
            estado["puerto"] = nombre
        print("USB abierto:", nombre, "-", encontrado.description or "")
        return


def guardar_acks(buffer):
    """Saca las lineas ACK del buffer y deja el resto para el JSON."""
    partes = buffer.split("\n")
    incompleto = partes[-1]
    quedan = []
    acks = []
    for linea in partes[:-1]:
        limpia = linea.strip()
        if limpia.startswith("ACK "):
            acks.append(limpia)
        else:
            quedan.append(linea)
    nuevo = "\n".join(quedan)
    if nuevo:
        nuevo += "\n"
    nuevo += incompleto
    if acks:
        with candado:
            estado["ack"] = acks[-1]
            estado["ack_en"] = time.time()
    return nuevo


def hilo_serial():
    """Hilo daemon: busca COM y parsea JSON SensorReading del firmware."""
    buffer = ""
    while True:
        with serial_lock:
            serie = puerto_serial
        if serie is None:
            try:
                buscar_puerto()
            except Exception:
                pass
            time.sleep(1.5)
            continue
        try:
            with serial_lock:
                if puerto_serial is None:
                    continue
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
        buffer = guardar_acks(buffer)
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
            enviar_al_edge(dato)  # --- EDGE: reenvio de la lectura real ---
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
  .supuestos button { text-align: left; display: grid; gap: 2px; min-width: 260px; line-height: 1.25; }
  .supuestos button small { font-weight: 400; opacity: 0.92; }
  .supuestos button.elegido { outline: 3px solid #16343c; background: #0e4f5e; }
  .aviso { font-size: 14px; margin: 8px 0 0; }
  .aviso.ok { color: #1d7a45; }
  .aviso.no { color: #b42318; }
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
  Abajo: supuesto por USB — no sustituye la medición. El relé enciende el motor con IN en bajo.</p>
</header>
<main>
  <section>
    <h2>Placa</h2>
    <p class="nota" id="placa">Buscando el ESP32 (Silicon Labs CP210x) en el USB.</p>
  </section>

  <section>
    <h2>Medición en tiempo real</h2>
    <p class="nota">Sale del AM2302 y del MQ135 cada ~2 s. El supuesto de abajo no cambia estos números.
    Si cualquier valor llega a crítico, el LED parpadea y suena el buzzer. El ventilador solo depende del etileno: semi más lento, crítico a tope, y apagado si el gas está en 0–1 ppm.</p>
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
    <p class="nota">Cada botón manda esos tres números al ESP32 y espera confirmación. El texto del botón es exactamente lo que deben hacer el LED, el buzzer y el ventilador.
    La medición de arriba no cambia. El caso también se guarda en el Edge.
    <code>Quitar supuesto</code> devuelve el LED, el buzzer y el ventilador a los sensores reales.</p>
    <div class="supuestos" id="casos"></div>
    <div class="rejilla" style="margin-top:12px">
      <label>Temperatura °C<input id="in-temp" type="number" value="4" step="0.1"></label>
      <label>Humedad %<input id="in-hum" type="number" value="92" step="0.1"></label>
      <label>Etileno ppm<input id="in-gas" type="number" value="0.4" step="0.1"></label>
    </div>
    <p class="nota" id="previo">Escribe los números y mira aquí qué haría la placa antes de aplicarlos.</p>
    <p><button id="aplicar">Aplicar este supuesto</button></p>
    <p class="frescura" id="sup-frescura">Sin supuesto</p>
    <p class="aviso" id="sup-placa"></p>
    <div class="rejilla">
      <div class="dato"><span>Temperatura del supuesto</span><strong id="sup-temp">-</strong></div>
      <div class="dato"><span>Humedad del supuesto</span><strong id="sup-hum">-</strong></div>
      <div class="dato"><span>Etileno del supuesto</span><strong id="sup-gas">-</strong></div>
    </div>
    <div class="activos" id="sup-activos"></div>
  </section>

  <section>
    <h2>Estandar FS-FRUIT-1 (cualquier fruta)</h2>
    <p class="nota">Misma tabla que el firmware. Un SEMI deja la fruta en riesgo (LED fijo). Cualquier CRÍTICO la da por malograda (LED parpadeo y buzzer). El ventilador no mira la temperatura ni la humedad.</p>
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
      <li><strong>LED y buzzer</strong> siguen el peor de temperatura, humedad y etileno. Crítico: LED parpadea y suena el buzzer. Semi: LED fijo.</li>
      <li><strong>Ventilador</strong> solo por etileno: semi = más lento, crítico = potencia máxima.</li>
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

function nivelDe(temperatura, humedad, etileno) {
  const nT = (temperatura > 12 || temperatura < -1) ? "CRITICO" : (temperatura > 8 || temperatura < 0) ? "SEMI" : "OK";
  const nH = (humedad < 85 || humedad > 98) ? "CRITICO" : (humedad < 90 || humedad > 95) ? "SEMI" : "OK";
  const nE = etileno > 5 ? "CRITICO" : etileno > 1 ? "SEMI" : "OK";
  const orden = { OK: 0, SEMI: 1, CRITICO: 2 };
  const peor = [nT, nH, nE].sort((a, b) => orden[b] - orden[a])[0];
  return {
    niveles: { temperatura: nT, humedad: nH, etileno: nE },
    frescura: peor === "CRITICO" ? "SPOILED" : peor === "SEMI" ? "AT_RISK" : "FRESH",
    led: peor === "CRITICO" ? "parpadeo" : peor === "SEMI" ? "continuo" : "apagado",
    buzzer: peor === "CRITICO",
    ventilador: nE !== "OK",
    velocidad: nE === "CRITICO" ? "maxima" : nE === "SEMI" ? "media" : "apagado"
  };
}

function frase(alertas) {
  const led = alertas.led === "parpadeo" ? "LED parpadeo" : alertas.led === "continuo" ? "LED fijo" : "LED apagado";
  const buzz = alertas.buzzer ? "buzzer sonando" : "buzzer apagado";
  const fan = alertas.velocidad === "maxima" ? "ventilador al máximo" : alertas.velocidad === "media" ? "ventilador a media" : "ventilador apagado";
  return led + " · " + buzz + " · " + fan;
}

function textoNivel(codigo) {
  if (codigo === "SEMI") return "semi";
  if (codigo === "CRITICO") return "crítico";
  return "normal";
}

const CASOS = [
  { nombre: "Cámara fría", t: 4, h: 92, g: 0.4 },
  { nombre: "Frío semi", t: 10, h: 92, g: 0.4 },
  { nombre: "Humedad semi", t: 4, h: 87, g: 0.4 },
  { nombre: "Frío crítico", t: 14, h: 92, g: 0.4 },
  { nombre: "Etileno semi", t: 4, h: 92, g: 3 },
  { nombre: "Etileno crítico", t: 4, h: 92, g: 8 },
  { nombre: "Clima y etileno semi", t: 10, h: 87, g: 3 }
];

function armarCasos() {
  const caja = $("casos");
  CASOS.forEach((caso) => {
    const efecto = nivelDe(caso.t, caso.h, caso.g);
    const boton = document.createElement("button");
    boton.dataset.t = String(caso.t);
    boton.dataset.h = String(caso.h);
    boton.dataset.g = String(caso.g);
    boton.dataset.n = caso.nombre;
    boton.innerHTML = "<strong>" + caso.nombre + "</strong>"
      + "<small>" + caso.t.toFixed(1) + " °C · " + caso.h.toFixed(1) + " % · " + caso.g.toFixed(1) + " ppm · " + efecto.frescura + "</small>"
      + "<small>" + frase(efecto) + "</small>";
    boton.addEventListener("click", () => {
      $("in-temp").value = caso.t;
      $("in-hum").value = caso.h;
      $("in-gas").value = caso.g;
      mostrarPrevio();
      mandar({
        activo: true,
        nombre: caso.nombre,
        temperatureC: caso.t,
        humidityPct: caso.h,
        ethylenePpm: caso.g
      });
    });
    caja.appendChild(boton);
  });
  const quitar = document.createElement("button");
  quitar.id = "real";
  quitar.className = "secundario";
  quitar.textContent = "Quitar supuesto";
  quitar.addEventListener("click", () => mandar({ activo: false }));
  caja.appendChild(quitar);
}

function pastillas(alertas) {
  const ledClase = alertas.led === "parpadeo" ? "parpadeo"
    : (alertas.led === "continuo" || alertas.led === true) ? "encendido" : "";
  const partes = [
    ["LED", textoLed(alertas.led), ledClase],
    ["Buzzer", alertas.buzzer ? "encendido" : "apagado", alertas.buzzer ? "encendido" : ""],
    ["Ventilador", alertas.velocidad === "maxima" ? "máxima" : (alertas.velocidad === "media" ? "media" : "apagado"), alertas.ventilador ? "aire" : ""]
  ];
  return partes.map(([nombre, texto, clase]) =>
    '<span class="pastilla ' + clase + '">' + nombre + ": " + texto + "</span>"
  ).join("");
}

function pintar(dato) {
  $("placa").textContent = dato.conectado
    ? "Placa en " + dato.puerto + " (CP210x). Medición arriba = sensores. El ventilador gira con el relé en bajo."
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
  marcarElegido(supuesto);
  if (!supuesto.activo || !supuesto.alertas) {
    $("sup-temp").textContent = "-";
    $("sup-hum").textContent = "-";
    $("sup-gas").textContent = "-";
    $("sup-frescura").textContent = "Sin supuesto. Los actuadores siguen la medición real.";
    $("sup-frescura").className = "frescura";
    $("sup-activos").innerHTML = "";
    pintarPlaca(dato, null);
    return;
  }
  const niveles = supuesto.alertas.niveles;
  $("sup-temp").textContent = supuesto.temperatureC.toFixed(1) + " °C";
  $("sup-hum").textContent = supuesto.humidityPct.toFixed(1) + " %";
  $("sup-gas").textContent = supuesto.ethylenePpm.toFixed(1) + " ppm";
  $("sup-frescura").textContent = supuesto.nombre + ": " + supuesto.alertas.frescura
    + " · temperatura " + textoNivel(niveles.temperatura)
    + " · humedad " + textoNivel(niveles.humedad)
    + " · etileno " + textoNivel(niveles.etileno);
  $("sup-frescura").className = "frescura " + supuesto.alertas.frescura;
  $("sup-activos").innerHTML = pastillas(supuesto.alertas);
  pintarPlaca(dato, supuesto);
}

function casiIgual(a, b) {
  return Math.abs(Number(a) - Number(b)) < 0.05;
}

function marcarElegido(supuesto) {
  document.querySelectorAll("#casos button[data-t]").forEach((boton) => {
    const activo = !!(supuesto && supuesto.activo)
      && casiIgual(boton.dataset.t, supuesto.temperatureC)
      && casiIgual(boton.dataset.h, supuesto.humidityPct)
      && casiIgual(boton.dataset.g, supuesto.ethylenePpm)
      && boton.dataset.n === supuesto.nombre;
    boton.classList.toggle("elegido", activo);
  });
}

function pintarPlaca(dato, supuesto) {
  const aviso = $("sup-placa");
  if (!dato.conectado) {
    aviso.className = "aviso no";
    aviso.textContent = "La placa no está en el USB, así que este caso no puede aplicarse.";
    return;
  }
  if (dato.placa && dato.placa.confirmo) {
    aviso.className = "aviso ok";
    aviso.textContent = supuesto
      ? "La placa confirmó este caso: " + frase(supuesto.alertas) + "."
      : "La placa confirmó que volvió a los sensores reales.";
    return;
  }
  aviso.className = "aviso no";
  aviso.textContent = "Enviado. La placa todavía no confirmó; se reintenta solo.";
}

function mostrarPrevio() {
  const t = Number($("in-temp").value);
  const h = Number($("in-hum").value);
  const g = Number($("in-gas").value);
  if (![t, h, g].every(Number.isFinite)) {
    $("previo").textContent = "Escribe temperatura, humedad y etileno.";
    return;
  }
  const efecto = nivelDe(t, h, g);
  $("previo").textContent = "Si aplicas estos números: " + efecto.frescura + " · " + frase(efecto)
    + ". Temperatura " + textoNivel(efecto.niveles.temperatura)
    + ", humedad " + textoNivel(efecto.niveles.humedad)
    + ", etileno " + textoNivel(efecto.niveles.etileno) + ".";
}

let revisionVista = 0;

async function pedir() {
  const respuesta = await fetch("/api/estado");
  const dato = await respuesta.json();
  if ((dato.revision || 0) < revisionVista) return;
  pintar(dato);
}

async function mandar(cuerpo) {
  const respuesta = await fetch("/api/supuesto", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(cuerpo)
  });
  const dato = await respuesta.json();
  if ((dato.revision || 0) < revisionVista) return;
  revisionVista = dato.revision || 0;
  pintar(dato);
}

$("aplicar").addEventListener("click", () => {
  mandar({
    activo: true,
    nombre: "Números elegidos",
    temperatureC: Number($("in-temp").value),
    humidityPct: Number($("in-hum").value),
    ethylenePpm: Number($("in-gas").value)
  });
});
["in-temp", "in-hum", "in-gas"].forEach((id) => {
  $(id).addEventListener("input", mostrarPrevio);
});
armarCasos();
mostrarPrevio();
setInterval(pedir, 1000);
pedir();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    hilo = threading.Thread(target=hilo_serial, daemon=True)
    hilo.start()
    threading.Thread(target=mantener_placa, daemon=True).start()
    print("FreshSense prototipo 1 → http://%s:%d" % (HOST, PUERTO))
    print("Cierra el Monitor Serie de Arduino. Prefiere COM Silicon Labs CP210x.")
    print("Pines: AM2302=4 MQ135=34 LED=27 Buzzer=25 Rele=26 (RELAY_ACTIVE_LOW true)")
    ThreadingHTTPServer((HOST, PUERTO), Sitio).serve_forever()
