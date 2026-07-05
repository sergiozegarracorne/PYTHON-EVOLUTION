"""
monitor.py
----------
Monitorea todos los chats de una instancia de Evolution API (WhatsApp),
clasifica cada mensaje nuevo con un modelo local de Ollama para detectar
si describe una EMERGENCIA, y si es asi dispara una alerta de pantalla
completa + lectura por voz (Windows, SAPI5, offline).

Uso:
    python monitor.py

Se detiene con Ctrl+C. No corre como servicio, solo mientras esta abierta
la consola (segun lo definido: uso manual, no 24/7).

Requisitos:
    pip install requests pyttsx3
    (tkinter viene incluido con Python en Windows)

La configuracion (URL de Ollama, de Evolution, del webhook, ventanas de
tiempo, etc.) se lee de config.ini, junto a este archivo. Si config.ini no
existe, se crea solo con los valores por defecto la primera vez que corre.
"""

import configparser
import csv
import ctypes
import json
import os
import random
import re
import sqlite3
import string
import sys
import threading
import time
from datetime import datetime, timedelta

import requests

# hablar() y mostrar_alerta() (la ventana de emergencia) viven en
# alertas_ui.py, sin ninguna dependencia de Evolution/Ollama/SEDAPAL: las
# usan tanto este modulo (modo standalone, todo en una sola PC) como
# cliente_alertas.py (PCs de la red local que solo reciben y muestran lo
# que les manda el backend).
from alertas_ui import hablar, mostrar_alerta  # noqa: F401

try:
    import sedapal_chat  # cliente no oficial (protocolo capturado a mano) del chat de SEDAPAL
except ImportError as e:
    # solo hace falta en la PC que corre el backend (consulta de deuda);
    # los clientes de la red local (cliente_alertas.py) importan este
    # modulo solo por sus utilidades de sistema y no necesitan "websockets"
    # instalado - sin sedapal_chat, manejar_consulta_deuda() simplemente
    # queda deshabilitado en vez de romper el arranque de todo el programa
    sedapal_chat = None
    print(f"[WARN] no se pudo importar sedapal_chat (falta el paquete 'websockets'?): {e}")

# ============================================================
# RUTAS (portable: los archivos van junto al .exe/.py, no segun el
# directorio de trabajo actual, para que funcione igual sin importar
# desde donde se lance - doble clic, acceso directo, tarea programada, etc.)
# ============================================================

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ruta(nombre_archivo):
    return os.path.join(BASE_DIR, nombre_archivo)


# ============================================================
# CONFIG - se lee de config.ini (junto al .exe/.py). Si el archivo no
# existe, se crea con estos valores por defecto la primera vez que corre,
# para poder cambiar cosas (URL de Ollama, del webhook, etc.) sin tener
# que tocar el codigo ni recompilar el .exe.
# ============================================================

CONFIG_FILE = ruta("config.ini")

# OJO: estos valores por defecto NO deben tener credenciales reales - se
# escriben tal cual en config.ini la primera vez que corre un programa que
# importa este modulo (incluido cliente_alertas.py, que se instala en PCs
# de la red local y NUNCA deberia terminar con la API key de Evolution/Groq
# en el disco). Las credenciales reales van SOLO en el config.ini de la PC
# del backend (esta gitignoreado, no se toca desde aca).
_CONFIG_DEFAULTS = {
    "evolution": {
        "api_url": "",   # sin / al final
        "api_key": "",
        "instance": "",
    },
    "ollama": {
        # lista de servidores de IA en orden de preferencia, separados por
        # coma. Se prueba el primero, y si no responde (caido, sin API key,
        # modelo no descargado, etc.) se prueba el siguiente. Dos formatos:
        #   - Ollama (nativo):        url|modelo
        #   - Groq (u otro compatible con OpenAI /chat/completions):
        #                             tipo|url|modelo|api_key
        # Para agregar otro servidor, solo se agrega una entrada mas a la
        # lista - no hace falta tocar el codigo.
        "servidores": "",
    },
    "webhook": {
        "host": "0.0.0.0",
        "port": "8500",
        "ruta": "/webhook",
    },
    "monitor": {
        # al iniciar, revisa mensajes de las ultimas X horas
        "catchup_hours": "3",
        # cada cuanto vuelve a preguntar por mensajes nuevos (solo en monitor.py, polling)
        "poll_interval_seconds": "20",
        # mensajes de texto seguidos del mismo numero con menos de esto entre
        # uno y otro se juntan y se evaluan como uno solo
        "ventana_agrupacion_segundos": "20",
        # igual, pero cuando hay una foto/video de por medio (la gente manda
        # la foto pegada al mensaje, no espera medio minuto)
        "ventana_agrupacion_media_segundos": "5",
        # la ventana de alerta se cierra sola cuando la barra llega al final
        "alerta_auto_cierre_segundos": "30",
        # cada cuanto se revisa si la sesion de WhatsApp en Evolution sigue
        # abierta (para detectar que cerraron sesion en el celular)
        "intervalo_verificacion_conexion": "30",
        # cuantos chequeos seguidos en estado distinto de "open" hacen falta
        # antes de mostrar la alerta de reconexion (evita falsas alarmas por
        # un corte de red/VPN de un instante)
        "confirmaciones_antes_de_alertar": "2",
    },
    "sedapal": {
        # ID del grupo de WhatsApp (ej: 120363409696652327@g.us) cuyos
        # miembros pueden usar la consulta de deuda de SEDAPAL. Se
        # administra agregando/sacando gente del grupo desde WhatsApp,
        # sin tocar contactos.csv ni el codigo. Vacio = usa contactos.csv
        # en su lugar (como antes).
        "grupo_autorizado": "",
    },
    "ia": {
        # prompt que se le manda a la IA para clasificar cada mensaje. Se
        # puede editar aca sin tocar el codigo ni recompilar el .exe - solo
        # hay que mantener el literal "{{MENSAJE}}" en algun lugar del texto
        # (ahi se reemplaza por el mensaje real) y que la instruccion siga
        # pidiendo un JSON con las claves emergencia/tipo/resumen/direccion.
        "prompt_clasificacion": (
            "Eres un ingeniero sanitario peruano que atiende reportes de emergencia "
            "por WhatsApp. La gente suele escribir apurada, sin conectores, sin "
            "tildes y en frases sueltas (ej: 'incendio mercado esquina angamos ayuda "
            "urgente'). Interpreta el sentido igual y redacta tu resumen como una "
            "oracion clara y completa, en tus propias palabras, sin copiar el texto "
            "tal cual.\n"
            "Determina si el mensaje describe una emergencia real (accidente, "
            "seguridad, incendio, aniego, riesgo inminente, dano en la via "
            "publica, etc). MUY IMPORTANTE: el tono del mensaje NO indica la "
            "gravedad - la gente suele reportar peligros reales con un tono "
            "tranquilo o administrativo (ej: 'a ver si pueden enviar personal para "
            "esta novedad'), eso NO significa que sea rutinario. Juzga la gravedad "
            "por lo que describe, no por como esta redactado.\n"
            "Son emergencia (dano en la via publica / riesgo inminente) casos "
            "como: buzon o caja de registro SIN TAPA, con la tapa rota, hundida, "
            "robada o floja (riesgo de que alguien caiga o un vehiculo lo dane), "
            "hueco o zanja abierta sin senalizar en la pista o vereda, tuberia "
            "rota con fuga o aniego en la via, cables expuestos, poste o muro a "
            "punto de caer, derrumbe. Estos son urgentes AUNQUE nadie use la "
            "palabra 'emergencia' o 'urgente' y el mensaje solo pida 'enviar "
            "personal' o 'revisar'.\n"
            "Si el mensaje es ambiguo, chismes, saludos, una consulta "
            "administrativa (cotizaciones, tramites, horarios) o no tiene "
            "relacion con un peligro fisico real, marca emergencia como false.\n"
            "Responde UNICAMENTE con un JSON valido (sin texto adicional, sin "
            "markdown), con el formato exacto:\n"
            '{"emergencia": true o false, '
            '"tipo": "incendio|accidente|seguridad|aniego|via_publica|otro", '
            '"resumen": "oracion clara parafraseando el peligro, en pocas palabras", '
            '"direccion": "direccion o referencia de ubicacion mencionada en el '
            'mensaje, o null si no se menciona ninguna"}\n\n'
            'Mensaje: "{{MENSAJE}}"'
        ),
    },
}


def _cargar_config():
    # interpolation=None: el prompt de IA no debe interpretarse (podria traer
    # "%" o llaves sueltas al editarlo, y no queremos que configparser los
    # intente resolver como referencias a otras claves)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict(_CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        cfg.read(CONFIG_FILE, encoding="utf-8")
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(
                "; Configuracion del monitor de emergencias. Edita lo que necesites\n"
                "; y vuelve a arrancar el programa para que tome los cambios.\n\n"
            )
            cfg.write(f)
        print(f"[INFO] se creo {CONFIG_FILE} con los valores por defecto")
    return cfg


def _parsear_servidores_ia(valor):
    """
    Cada entrada separada por coma puede venir en dos formatos:
      - Ollama (nativo):  url|modelo
      - Groq (u otro compatible con OpenAI /chat/completions):
                          tipo|url|modelo|api_key
    Devuelve una lista de dicts: {"tipo", "url", "modelo", "api_key"}.
    """
    servidores = []
    for parte in valor.split(","):
        parte = parte.strip()
        if not parte:
            continue
        campos = [c.strip() for c in parte.split("|")]
        if len(campos) == 2:
            url, modelo = campos
            servidores.append({"tipo": "ollama", "url": url, "modelo": modelo, "api_key": None})
        elif len(campos) == 4:
            tipo, url, modelo, api_key = campos
            servidores.append({"tipo": tipo or "ollama", "url": url, "modelo": modelo, "api_key": api_key or None})
        else:
            print(f"[WARN] entrada de config.ini [ollama] servidores con formato invalido, se ignora: {parte!r}")
    return servidores


_cfg = _cargar_config()

EVOLUTION_API_URL = _cfg.get("evolution", "api_url")
EVOLUTION_API_KEY = _cfg.get("evolution", "api_key")
EVOLUTION_INSTANCE = _cfg.get("evolution", "instance")

SERVIDORES_IA = _parsear_servidores_ia(_cfg.get("ollama", "servidores"))  # [{"tipo","url","modelo","api_key"}, ...]

WEBHOOK_HOST = _cfg.get("webhook", "host")
WEBHOOK_PORT = _cfg.getint("webhook", "port")
WEBHOOK_RUTA = _cfg.get("webhook", "ruta")

CATCHUP_HOURS = _cfg.getint("monitor", "catchup_hours")
POLL_INTERVAL_SECONDS = _cfg.getint("monitor", "poll_interval_seconds")
VENTANA_AGRUPACION_SEGUNDOS = _cfg.getint("monitor", "ventana_agrupacion_segundos")
VENTANA_AGRUPACION_MEDIA_SEGUNDOS = _cfg.getint("monitor", "ventana_agrupacion_media_segundos")
ALERTA_AUTO_CIERRE_SEGUNDOS = _cfg.getint("monitor", "alerta_auto_cierre_segundos")
INTERVALO_VERIFICACION_CONEXION = _cfg.getint("monitor", "intervalo_verificacion_conexion")
CONFIRMACIONES_ANTES_DE_ALERTAR = _cfg.getint("monitor", "confirmaciones_antes_de_alertar")

SEDAPAL_GRUPO_AUTORIZADO = _cfg.get("sedapal", "grupo_autorizado").strip()

PROMPT_CLASIFICACION = _cfg.get("ia", "prompt_clasificacion")

STATE_FILE = ruta("monitor_state.json")   # aqui se guarda el ultimo timestamp visto

CONTACTS_CSV = ruta("contactos.csv")      # opcional: columnas "telefono,nombre" para
                                           # cruzar el numero del remitente con su nombre real

DB_FILE = ruta("emergencias.db")          # historial de todos los mensajes procesados (sqlite)

# ============================================================
# ENERGIA (evitar que Windows apague la pantalla / active el protector)
# ============================================================

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def evitar_suspension_pantalla():
    """
    Le pide a Windows que no apague la pantalla ni active el protector de
    pantalla mientras este proceso siga corriendo. Una alerta de emergencia
    con la pantalla apagada no sirve de nada.
    """
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception as e:
        print(f"[WARN] no se pudo evitar la suspension de pantalla: {e}")


def restaurar_suspension_pantalla():
    """Al salir, devuelve a Windows el comportamiento normal de ahorro de energia."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


# ============================================================
# CONSOLA (agrandar ventana/buffer/letra - por defecto Windows abre
# consolas chiquitas donde apenas entran unas pocas lineas de log)
# ============================================================

LOG_FILE = ruta("webhook.log")


class _TeeStdout:
    """Escribe a la salida original Y a un archivo, para poder ver el log desde otra ventana."""

    def __init__(self, original, archivo):
        self._original = original
        self._archivo = archivo

    def write(self, texto):
        try:
            self._original.write(texto)
        except Exception:
            pass
        try:
            self._archivo.write(texto)
            self._archivo.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._archivo.flush()
        except Exception:
            pass


def activar_log_en_archivo():
    """
    Ademas de imprimir en la consola (que va a quedar oculta), todo se
    escribe a LOG_FILE. Asi el icono de bandeja puede abrir una ventana
    APARTE que solo lee ese archivo en vivo - una ventana totalmente
    independiente del proceso principal, que se puede cerrar sin que eso
    afecte al monitor (a diferencia de tocar la consola propia del proceso,
    que en Windows Terminal puede terminar matando todo el programa).
    """
    try:
        archivo = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = _TeeStdout(sys.stdout, archivo)
        sys.stderr = _TeeStdout(sys.stderr, archivo)
    except Exception as e:
        print(f"[WARN] no se pudo activar el log en archivo: {e}")


def configurar_consola(columnas=120, filas_ventana=40, filas_buffer=3000, tamano_letra=18):
    try:
        STD_OUTPUT_HANDLE = -11
        handle = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if not handle or handle == -1:
            return

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [
                ("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
            ]

        class CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong), ("nFont", ctypes.c_ulong),
                ("dwFontSize", COORD), ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint), ("FaceName", ctypes.c_wchar * 32),
            ]

        # letra mas grande (Consolas), para que se lea bien sin tener que hacer zoom
        fuente = CONSOLE_FONT_INFOEX()
        fuente.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        fuente.dwFontSize = COORD(0, tamano_letra)
        fuente.FontFamily = 54  # TMPF_TRUETYPE
        fuente.FontWeight = 400
        fuente.FaceName = "Consolas"
        ctypes.windll.kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(fuente))

        # la ventana no puede ser mas grande que el buffer ni el buffer mas chico
        # que la ventana: primero se achica la ventana, se agranda el buffer, y
        # recien ahi se agranda la ventana al tamano final
        ctypes.windll.kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(SMALL_RECT(0, 0, 79, 24)))
        ctypes.windll.kernel32.SetConsoleScreenBufferSize(handle, COORD(columnas, filas_buffer))
        ctypes.windll.kernel32.SetConsoleWindowInfo(
            handle, True, ctypes.byref(SMALL_RECT(0, 0, columnas - 1, filas_ventana - 1))
        )
        ctypes.windll.kernel32.SetConsoleTitleW("Monitor de Emergencias - WhatsApp")
    except Exception as e:
        print(f"[WARN] no se pudo agrandar la consola: {e}")


# ============================================================
# INSTANCIA UNICA (evitar dos monitores corriendo a la vez)
# ============================================================

# el mismo nombre de mutex lo usan monitor.py, webhook_server.py y
# webhook_server_backend.py: asi, si ya hay una de esas corriendo, las
# demas se niegan a arrancar (evita alertas/consultas duplicadas por tener
# dos "backends" prendidos juntos). cliente_alertas.py usa un nombre
# DISTINTO a proposito: es un rol distinto (solo muestra pantallas, no
# habla con Evolution) y tiene que poder correr en la MISMA PC que el
# backend sin pisarse con el.
_NOMBRE_MUTEX = "Global\\MonitorEmergenciasWhatsApp_RADIO_SURQUILLO"
_NOMBRE_MUTEX_CLIENTE = "Global\\MonitorEmergenciasWhatsApp_Cliente_RADIO_SURQUILLO"
_ERROR_ALREADY_EXISTS = 183

_mutex_instancia = None  # se guarda la referencia para que el handle no se cierre


def asegurar_instancia_unica(nombre_mutex=_NOMBRE_MUTEX):
    """
    Si ya hay otra instancia con el mismo nombre de mutex corriendo, cierra
    esta copia de inmediato en vez de dejar dos activas a la vez.
    """
    global _mutex_instancia
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, nombre_mutex)
        if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            print("[ERROR] Ya hay una instancia de este programa corriendo. Cerrando esta copia.")
            sys.exit(1)
        _mutex_instancia = handle
    except OSError as e:
        print(f"[WARN] no se pudo verificar instancia unica, se sigue de todos modos: {e}")


# ============================================================
# ESTADO (para no reprocesar mensajes ya vistos)
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # si no existe, arrancamos desde CATCHUP_HOURS atras
    since = datetime.now() - timedelta(hours=CATCHUP_HOURS)
    return {"last_timestamp": int(since.timestamp())}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ============================================================
# BASE DE DATOS (historial de mensajes procesados)
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            fecha TEXT NOT NULL,
            remitente TEXT,
            telefono TEXT,
            nombre TEXT,
            texto TEXT,
            media_tipo TEXT,
            emergencia INTEGER NOT NULL,
            tipo TEXT,
            resumen TEXT,
            direccion TEXT,
            cantidad_agrupados INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    conn.close()


def guardar_mensaje(
    remitente, telefono, nombre, texto, media_tipo, emergencia,
    tipo=None, resumen=None, direccion=None, timestamp=None, cantidad_agrupados=1,
):
    """Cada mensaje/grupo procesado se guarda aca, sea o no emergencia (auditoria/historial)."""
    ts = timestamp or int(time.time())
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            """
            INSERT INTO mensajes
                (timestamp, fecha, remitente, telefono, nombre, texto, media_tipo,
                 emergencia, tipo, resumen, direccion, cantidad_agrupados)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts, datetime.fromtimestamp(ts).isoformat(), remitente, telefono, nombre,
                texto, media_tipo, int(emergencia), tipo, resumen, direccion, cantidad_agrupados,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# CONTACTOS (cruce opcional telefono -> nombre)
# ============================================================

def load_contactos():
    """
    Carga un CSV opcional (columnas: telefono,nombre) para poder decir
    "Sr. Juan Perez" en vez de solo el numero. Si el archivo no existe,
    el monitor sigue funcionando usando el pushName que manda WhatsApp.
    """
    contactos = {}
    if not os.path.exists(CONTACTS_CSV):
        return contactos
    with open(CONTACTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            telefono = (row.get("telefono") or "").strip()
            nombre = (row.get("nombre") or "").strip()
            if telefono:
                contactos[telefono] = nombre
    return contactos


def load_grupos():
    """
    Trae el nombre real (subject) de todos los grupos de WhatsApp de la
    instancia, para poder decir de que grupo vino un reporte. El remoteJid
    de un grupo (ej: 120363409696652327@g.us) no es legible por si solo, y
    "RADIO-SURQUILLO" es el nombre de la INSTANCIA, no el de cada grupo.
    """
    url = f"{EVOLUTION_API_URL}/group/fetchAllGroups/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        resp = requests.get(url, headers=headers, params={"getParticipants": "false"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[WARN] no se pudo cargar la lista de grupos: {e}")
        return {}
    return {g["id"]: g.get("subject") or g["id"] for g in data if g.get("id")}


def cargar_participantes_autorizados():
    """
    Si SEDAPAL_GRUPO_AUTORIZADO esta configurado en config.ini, trae los
    numeros de telefono de los miembros de ESE grupo de WhatsApp: son los
    unicos que pueden usar la consulta de deuda de SEDAPAL (se administra
    agregando/sacando gente del grupo desde WhatsApp). Si no esta
    configurado, devuelve None - el llamador debe usar contactos.csv
    en su lugar (comportamiento anterior).
    """
    if not SEDAPAL_GRUPO_AUTORIZADO:
        return None

    url = f"{EVOLUTION_API_URL}/group/fetchAllGroups/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        resp = requests.get(url, headers=headers, params={"getParticipants": "true"}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[WARN] no se pudo cargar el grupo autorizado para SEDAPAL: {e}")
        return set()

    for g in data:
        if g.get("id") == SEDAPAL_GRUPO_AUTORIZADO:
            return {
                extraer_telefono(p.get("phoneNumber", ""))
                for p in g.get("participants", [])
                if p.get("phoneNumber")
            }

    print(f"[WARN] no se encontro el grupo autorizado {SEDAPAL_GRUPO_AUTORIZADO!r} (revisa config.ini [sedapal])")
    return set()


# ============================================================
# EVOLUTION API - traer mensajes
# ============================================================

def fetch_messages():
    """
    Trae mensajes recientes de todos los chats de la instancia.
    NOTA: el filtro por remoteJid/fecha en /chat/findMessages es
    inconsistente segun la version de Evolution API, asi que traemos
    todo y filtramos nosotros mismos por timestamp.
    """
    url = f"{EVOLUTION_API_URL}/chat/findMessages/{EVOLUTION_INSTANCE}"
    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_API_KEY,
    }
    try:
        resp = requests.post(url, headers=headers, json={"where": {}}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] No se pudo conectar a Evolution API: {e}")
        return []

    # La forma de la respuesta varia segun version (lista simple o {"messages": {"records": [...]}})
    if isinstance(data, dict) and "messages" in data:
        records = data["messages"].get("records", data["messages"])
    elif isinstance(data, list):
        records = data
    else:
        records = []

    return records


def enviar_mensaje(destino, texto):
    """
    Envia un mensaje de texto por WhatsApp via Evolution API. "destino"
    puede ser un numero (ej: 51981359205) o un JID completo, individual o
    de grupo (ej: 120363409696652327@g.us) - Evolution acepta ambos en "number".
    """
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY}
    body = {"number": destino, "text": texto}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        if resp.status_code not in (200, 201):
            print(f"[WARN] no se pudo enviar el mensaje de WhatsApp: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[WARN] no se pudo enviar el mensaje de WhatsApp: {e}")
        return False


def enviar_estado_escribiendo(destino, segundos=8):
    """
    Manda el indicador de "escribiendo..." al chat (individual o grupo).
    Se usa mientras se consulta el saldo en SEDAPAL, que tarda varios
    segundos por el WebSocket, para que se vea como una persona respondiendo
    en vez de silencio total y despues varios mensajes de golpe. Best-effort:
    si falla, no debe romper la consulta de deuda en si.
    """
    url = f"{EVOLUTION_API_URL}/chat/sendPresence/{EVOLUTION_INSTANCE}"
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY}
    body = {"number": destino, "presence": "composing", "delay": segundos * 1000}
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        if resp.status_code not in (200, 201):
            print(f"[WARN] no se pudo enviar el estado 'escribiendo...' a {destino}: {resp.status_code} {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] no se pudo enviar el estado 'escribiendo...' a {destino}: {e}")


def enviar_mensajes_en_partes(destino, partes):
    """
    Manda varios mensajes cortos seguidos (con una pequena pausa aleatoria
    entre cada uno, como si los estuviera tipeando una persona) en vez de
    un solo bloque de texto largo.
    """
    ok = True
    for parte in partes:
        ok = enviar_mensaje(destino, parte) and ok
        time.sleep(random.uniform(0.8, 1.8))
    return ok


# ============================================================
# ESTADO DE CONEXION (detectar que cerraron sesion en el celular)
# ============================================================

def consultar_estado_conexion():
    """
    Le pregunta a Evolution API el estado actual de la sesion de WhatsApp
    ("open" = conectado normal, "connecting" o "close" = sin sesion activa,
    hace falta re-escanear el QR). Devuelve None si no se pudo ni
    consultar (API/red caida) - eso NO se interpreta como sesion cerrada,
    para no disparar una falsa alarma por un problema aparte.
    """
    url = f"{EVOLUTION_API_URL}/instance/connectionState/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[WARN] no se pudo consultar el estado de conexion de WhatsApp: {e}")
        return None

    instancia = data.get("instance") if isinstance(data, dict) else None
    if isinstance(instancia, dict) and instancia.get("state"):
        return instancia["state"]
    if isinstance(data, dict) and data.get("state"):
        return data["state"]
    return None


def obtener_qr_conexion():
    """
    Le pide a Evolution API que genere un QR para volver a vincular la
    sesion (solo tiene sentido llamarlo cuando el estado no es "open").
    Devuelve el base64 de la imagen (sin el prefijo "data:image/...;base64,")
    o None si todavia no hay QR disponible / fallo la consulta.
    """
    url = f"{EVOLUTION_API_URL}/instance/connect/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[WARN] no se pudo obtener el QR de reconexion: {e}")
        return None

    if not isinstance(data, dict):
        return None
    base64_qr = data.get("base64") or (data.get("qrcode") or {}).get("base64")
    if not base64_qr:
        return None
    if "," in base64_qr:
        base64_qr = base64_qr.split(",", 1)[1]  # saca el prefijo "data:image/png;base64,"
    return base64_qr


# ============================================================
# CONSULTA DE DEUDA SEDAPAL ("deuda:1234567" en el chat)
# ============================================================

# El numero de suministro (NIS) de SEDAPAL siempre tiene 7 digitos. Dentro
# del grupo autorizado (que es exclusivamente para esto) no hace falta
# ninguna palabra clave: cualquier numero de 7 digitos se presume que es
# una consulta de NIS/estado de cuenta.
PATRON_NIS = re.compile(r"\b(\d{7})\b")

# extrae de la respuesta cruda de SEDAPAL: cantidad de recibos pendientes,
# el monto total (soles y centavos por separado) y la fecha de vencimiento.
# Ej. de texto real: "Usted tiene 1 recibo(s) pendiente(s) por un monto de
# S/. 240 con 30 centesimo(s). El monto correspondiente a su ultimo recibo
# es de S/. 240 con 30 centesimo(s), cuya fecha de vencimiento es el 24 / 06 / 2026"
PATRON_RESUMEN_DEUDA = re.compile(
    r"tiene\s+(\d+)\s+recibo\(s\)\s+pendiente\(s\)\s+por\s+un\s+monto\s+de\s+S/\.\s*([\d,]+)"
    r"\s+con\s+(\d+)\s+cent[eé]simo\(s\).*?"
    r"fecha de vencimiento es el\s*([\d\s/]+)",
    re.IGNORECASE | re.DOTALL,
)


def _generar_id_aleatorio():
    """
    Sufijo aleatorio (tipo "$id:a14d1ff4w6") para que cada respuesta no sea
    un texto identico a la anterior - ayuda a que WhatsApp no vea patrones
    de mensajes repetidos/plantilla en la cuenta.
    """
    caracteres = string.ascii_lowercase + string.digits
    return "_____$" + "".join(random.choice(caracteres) for _ in range(8)) + "$______"


def _formatear_respuesta_deuda(nis, texto_crudo):
    """
    Ordena la respuesta cruda de SEDAPAL en partes cortas y legibles, listas
    para mandarse como mensajes separados. Si el texto no matchea el patron
    esperado (el bot respondio distinto - sin deuda, NIS invalido, cambio de
    formato, etc.), se manda el texto original tal cual: mejor eso que forzar
    un formato que podria quedar mal armado o perder informacion.
    """
    match = PATRON_RESUMEN_DEUDA.search(texto_crudo)
    if not match:
        return [texto_crudo, _generar_id_aleatorio()]

    cantidad, soles, centavos, fecha = match.groups()
    fecha = " ".join(fecha.split())  # normaliza espacios sueltos: "24 / 06 / 2026" -> igual pero prolijo

    return [
        f"Estado NIS: *{nis}*",
        f"{cantidad} recibo(s) pendiente(s) por S/ {soles}.{centavos}",
        f"Vencimiento {fecha}",
        _generar_id_aleatorio(),
    ]


def manejar_consulta_deuda(texto, autorizado, destino=None):
    """
    Este servicio es solo para el grupo de trabajadores de SEDAPAL
    autorizado (ver SEDAPAL_GRUPO_AUTORIZADO / cargar_participantes_autorizados).
    Si "autorizado" es False, ni se busca el patron: no se llama a
    sedapal_chat (evita gastar consultas del chat de SEDAPAL, que es fragil
    y no es nuestro) y se devuelve None - no se responde absolutamente nada.

    Si "autorizado" es True, cualquier numero de 7 digitos en el texto se
    presume una consulta de NIS/estado de cuenta (no hace falta que diga
    "deuda" ni ninguna palabra clave - el grupo es exclusivamente para
    esto). Consulta el saldo en SEDAPAL y devuelve una LISTA de mensajes
    cortos, listos para mandarse por separado (ver enviar_mensajes_en_partes).

    Mientras se espera la respuesta de SEDAPAL (el WebSocket tarda varios
    segundos), si se paso "destino" se manda ahi el estado "escribiendo..."
    cada pocos segundos, para que se vea mas real (una persona respondiendo)
    en vez de silencio y despues varios mensajes de golpe.
    """
    if not texto or not autorizado or sedapal_chat is None:
        return None

    match = PATRON_NIS.search(texto)
    if not match:
        return None

    nis = match.group(1)
    print(f"[INFO] consultando deuda SEDAPAL para el suministro {nis}...")

    detener_escribiendo = threading.Event()
    if destino:
        def _mantener_escribiendo():
            while not detener_escribiendo.is_set():
                enviar_estado_escribiendo(destino)
                detener_escribiendo.wait(8)

        threading.Thread(target=_mantener_escribiendo, daemon=True).start()

    try:
        resultado = sedapal_chat.consultar_deuda(nis)
    finally:
        detener_escribiendo.set()

    return _formatear_respuesta_deuda(nis, resultado["texto"])


def extract_text(msg):
    """Extrae el texto plano de un mensaje de Evolution API/Baileys."""
    message = msg.get("message") or {}
    if not message:
        return None
    if "conversation" in message:
        return message["conversation"]
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text")
    return None


def extract_ubicacion(msg):
    """Si el mensaje trae una ubicacion GPS (comun/en vivo), devuelve (lat, lng)."""
    message = msg.get("message") or {}
    for campo in ("locationMessage", "liveLocationMessage"):
        loc = message.get(campo)
        if loc and loc.get("degreesLatitude") is not None:
            return loc.get("degreesLatitude"), loc.get("degreesLongitude")
    return None


def extraer_telefono(jid):
    """Extrae solo el numero de un JID de WhatsApp (ej: 51987654321@s.whatsapp.net)."""
    if not jid:
        return ""
    return jid.split("@")[0]


MEDIA_TIPOS = {
    "imageMessage": "foto",
    "audioMessage": "audio",
    "videoMessage": "video",
    "documentMessage": "documento",
}


def extract_media(msg):
    """
    Si el mensaje trae foto/audio/video/documento, devuelve
    {"tipo", "caption", "thumbnail_b64"}. No se descarga el archivo completo
    (requeriria desencriptarlo via la API); para fotos/videos, Baileys ya
    manda una miniatura JPEG chica en base64 dentro del propio mensaje
    (jpegThumbnail), que se usa para mostrar algo en la alerta sin llamadas
    extra. Para audio/documento no hay miniatura: se avisa solo por texto.
    """
    message = msg.get("message") or {}
    for campo, etiqueta in MEDIA_TIPOS.items():
        contenido = message.get(campo)
        if contenido:
            return {
                "tipo": etiqueta,
                "caption": contenido.get("caption") or None,
                "thumbnail_b64": contenido.get("jpegThumbnail") if etiqueta in ("foto", "video") else None,
            }
    return None


def get_new_messages(since_ts):
    raw = fetch_messages()
    nuevos = []
    for msg in raw:
        key = msg.get("key", {})
        if key.get("fromMe"):
            continue  # ignoramos lo que nosotros mismos enviamos

        ts = msg.get("messageTimestamp", 0)
        if isinstance(ts, str):
            ts = int(ts) if ts.isdigit() else 0
        if ts <= since_ts:
            continue

        texto = extract_text(msg)
        media = extract_media(msg)
        ubicacion = extract_ubicacion(msg)
        if not texto and not media and not ubicacion:
            continue  # ni texto, ni media, ni ubicacion: no hay nada que analizar

        # WhatsApp ahora suele usar direccionamiento "lid" (identificador opaco)
        # en vez del numero real, tanto en remoteJid (chats 1 a 1) como en
        # participant (grupos). Cuando eso pasa, Evolution/Baileys manda el
        # numero real aparte en remoteJidAlt / participantAlt. Hay que preferir
        # esos campos "Alt"; si no existen, el chat no usa lid y el numero
        # real ya viene en participant/remoteJid directamente.
        remitente_jid = (
            key.get("participantAlt")
            or key.get("remoteJidAlt")
            or key.get("participant")
            or key.get("remoteJid", "")
        )

        nuevos.append({
            "texto": texto or "",
            "media": media,   # {"tipo","caption"} o None
            "remitente": key.get("remoteJid", "desconocido"),
            "telefono": extraer_telefono(remitente_jid),
            "push_name": msg.get("pushName", ""),
            "ubicacion": ubicacion,   # (lat, lng) o None
            "timestamp": ts,
        })

    nuevos.sort(key=lambda m: m["timestamp"])
    return nuevos


# ============================================================
# IA (Ollama / Groq) - clasificacion
# ============================================================

# se recuerda que servidor respondio bien la ultima vez, para probar ese
# primero en el siguiente mensaje y no perder tiempo reintentando un
# servidor caido antes de caer al que si funciona.
_servidor_ia_activo = [None]


def _orden_intentos_ia():
    servidores = SERVIDORES_IA
    activo = _servidor_ia_activo[0]
    if activo in servidores:
        return [activo] + [s for s in servidores if s != activo]
    return servidores


def _llamar_ollama_nativo(servidor, prompt):
    resp = requests.post(
        servidor["url"],
        json={
            "model": servidor["modelo"],
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=(5, 30),  # 5s para conectar, hasta 30s para la respuesta del modelo
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _llamar_openai_compatible(servidor, prompt):
    """Groq y otros que implementan la misma API que OpenAI (/chat/completions)."""
    headers = {"Content-Type": "application/json"}
    if servidor["api_key"]:
        headers["Authorization"] = f"Bearer {servidor['api_key']}"
    resp = requests.post(
        servidor["url"],
        headers=headers,
        json={
            "model": servidor["modelo"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=(5, 30),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _llamar_ia(prompt):
    """
    Prueba cada servidor de SERVIDORES_IA en orden (el que funciono la
    ultima vez, primero) hasta que alguno responda. Devuelve el texto de
    la respuesta del modelo, o lanza la ultima excepcion si todos fallaron.
    """
    ultimo_error = None
    for servidor in _orden_intentos_ia():
        try:
            if servidor["tipo"] == "ollama":
                texto = _llamar_ollama_nativo(servidor, prompt)
            else:
                texto = _llamar_openai_compatible(servidor, prompt)
            _servidor_ia_activo[0] = servidor
            return texto
        except (requests.RequestException, KeyError, IndexError) as e:
            print(f"[WARN] {servidor['tipo']} en {servidor['url']} (modelo {servidor['modelo']}) no respondio, se prueba el siguiente: {e}")
            ultimo_error = e
    raise ultimo_error


def clasificar_emergencia(texto):
    """
    Le pide a Ollama que clasifique el mensaje Y que extraiga los datos
    utiles para armar la alerta (tipo de peligro, resumen, direccion
    mencionada). Los datos de contacto (nombre/telefono/GPS) NO se le
    piden al modelo: esos los conocemos nosotros y se agregan aparte en
    construir_mensaje_alerta(), para no depender de que la IA los invente.
    """
    # el texto del prompt sale de config.ini [ia] prompt_clasificacion, para
    # poder ajustarlo (agregar mas palabras/casos de peligro, etc.) sin tocar
    # el codigo ni recompilar el .exe
    prompt = PROMPT_CLASIFICACION.replace("{{MENSAJE}}", texto)

    default = {"emergencia": False, "tipo": "otro", "resumen": texto, "direccion": None}

    try:
        raw = _llamar_ia(prompt)

        # el modelo a veces envuelve el json en ```json ... ``` pese a la instruccion
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return {
            "emergencia": bool(parsed.get("emergencia", False)),
            "tipo": parsed.get("tipo") or "otro",
            "resumen": parsed.get("resumen") or texto,
            "direccion": parsed.get("direccion") or None,
        }
    except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError, IndexError) as e:
        print(f"[WARN] No se pudo clasificar el mensaje (se probaron {len(SERVIDORES_IA)} servidor(es)): {e}")
        return default


def construir_mensaje_alerta(nombre, telefono, clasif, ubicacion, contacto_conocido, nombre_grupo=None):
    """
    Arma el texto final que se muestra/lee en la alerta, mezclando lo que
    dijo la IA (tipo/resumen/direccion) con los datos reales del remitente
    (nombre cruzado por CSV, telefono, si mando ubicacion GPS o no).

    Si el numero esta guardado en contactos.csv (contacto_conocido=True) solo
    se dice el nombre; el telefono solo se muestra cuando el contacto NO esta
    identificado, para no ensuciar la alerta con datos redundantes.
    """
    quien = nombre or telefono or "Un contacto"
    partes = [f"{quien} esta informando de un peligro: {clasif['resumen']}."]

    if nombre_grupo:
        partes.append(f"Reportado en el grupo {nombre_grupo}.")

    if clasif.get("direccion"):
        partes.append(f"Direccion: {clasif['direccion']}.")

    if ubicacion:
        lat, lng = ubicacion
        partes.append(f"Se envio ubicacion GPS: https://maps.google.com/?q={lat},{lng}.")

    if not contacto_conocido and telefono:
        partes.append(f"Telefono: {telefono}.")

    partes.append("Favor de atender la urgencia.")
    return " ".join(partes)


# ============================================================
# ALERTA - pantalla completa + voz (Windows)
# ============================================================

# hablar() y mostrar_alerta() (la ventana de emergencia) se re-exportan
# aca para no romper el codigo existente que hace monitor.hablar(...) /
# monitor.mostrar_alerta(...) - ver el import de alertas_ui mas arriba,
# junto al resto de imports, para el detalle de por que viven separadas.

def mostrar_alerta_desconexion():
    """
    Pantalla completa NEGRA con letras VERDES grandes: se muestra cuando se
    detecta que la sesion de WhatsApp se cerro (p.ej. cerraron sesion desde
    el celular) y hace falta volver a escanear el QR. A diferencia de
    mostrar_alerta(), esta ventana NO se cierra sola con un tiempo fijo ni
    con ESC/ENTER: se queda ahi, refrescando el QR cada pocos segundos, y
    solo se cierra cuando vuelve a detectar la sesion "open" - cerrarla
    antes no serviria de nada, el monitor seguiria sin poder leer/mandar
    mensajes. Es una funcion bloqueante (corre su propio mainloop de
    tkinter): quien la llame debe hacerlo desde un hilo dedicado.
    """
    import tkinter as tk

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(bg="black")

    tk.Label(
        root, text="SESION DE WHATSAPP CERRADA", fg="#00ff41", bg="black",
        font=("Consolas", 60, "bold"),
    ).pack(pady=(50, 10))

    tk.Label(
        root, text="Escanea el codigo QR con el celular (WhatsApp > Dispositivos vinculados)\n"
                    "para volver a iniciar sesion",
        fg="#00ff41", bg="black", font=("Consolas", 26, "bold"), justify="center",
    ).pack(pady=(0, 20))

    label_qr = tk.Label(root, bg="black")
    label_qr.pack(pady=20)

    label_estado = tk.Label(
        root, text="Buscando codigo QR...", fg="#00ff41", bg="black", font=("Consolas", 18),
    )
    label_estado.pack(side="bottom", pady=20)

    def actualizar():
        try:
            estado = consultar_estado_conexion()
            if estado == "open":
                root.destroy()
                return

            qr_b64 = obtener_qr_conexion()
            if qr_b64:
                try:
                    import base64
                    import io

                    from PIL import Image, ImageTk

                    datos = base64.b64decode(qr_b64)
                    img = Image.open(io.BytesIO(datos)).resize((480, 480))
                    foto_tk = ImageTk.PhotoImage(img)
                    label_qr.configure(image=foto_tk)
                    label_qr.image = foto_tk  # referencia viva
                    label_estado.configure(text=f"Estado: {estado or 'desconocido'} - el QR se refresca solo")
                except Exception as e:
                    label_estado.configure(text=f"No se pudo dibujar el QR ({e}). Estado: {estado or 'desconocido'}")
            else:
                label_estado.configure(text=f"Esperando codigo QR de Evolution... Estado: {estado or 'desconocido'}")
        except Exception as e:
            label_estado.configure(text=f"[ERROR] {e}")
        finally:
            try:
                root.after(8000, actualizar)
            except tk.TclError:
                pass  # la ventana ya se cerro (root.destroy() de arriba)

    root.after(200, actualizar)
    root.mainloop()


# ============================================================
# AGRUPACION - juntar fragmentos seguidos del mismo numero
# ============================================================

def _texto_con_caption(msg):
    """El texto del mensaje mas el caption de la foto/video, si tiene."""
    texto = msg["texto"]
    if msg["media"] and msg["media"].get("caption"):
        texto = (texto + " " + msg["media"]["caption"]).strip()
    return texto


def agrupar_mensajes(nuevos):
    """
    Junta mensajes CONSECUTIVOS del mismo numero que llegaron cerca en el
    tiempo, para evaluarlos como una sola emergencia en vez de fragmento por
    fragmento. Una foto/video/audio/documento se une al texto (antes o
    despues) del mismo numero: si ese texto combinado resulta emergencia se
    alerta con la foto incluida; si no hay texto cerca, la media queda sola y
    nunca dispara alerta por si misma. Cuando hay media de por medio se usa
    VENTANA_AGRUPACION_MEDIA_SEGUNDOS (mas corta) en vez de la de puro texto.
    """
    grupos = []
    for msg in nuevos:
        anterior = grupos[-1] if grupos else None
        if anterior is not None and anterior["telefono"] == msg["telefono"]:
            hay_media = bool(anterior["media"]) or bool(msg["media"])
            ventana = VENTANA_AGRUPACION_MEDIA_SEGUNDOS if hay_media else VENTANA_AGRUPACION_SEGUNDOS
            seguido_del_mismo_numero = msg["timestamp"] - anterior["timestamp"] < ventana
        else:
            seguido_del_mismo_numero = False

        if seguido_del_mismo_numero:
            anterior["texto"] = (anterior["texto"] + " " + _texto_con_caption(msg)).strip()
            anterior["ubicacion"] = anterior["ubicacion"] or msg["ubicacion"]
            anterior["timestamp"] = msg["timestamp"]
            anterior["cantidad"] += 1
            if msg["media"] and not anterior["media"]:
                anterior["media"] = msg["media"]  # se queda con la primera foto/video del grupo
        else:
            nuevo_grupo = dict(msg)
            nuevo_grupo["texto"] = _texto_con_caption(msg)
            nuevo_grupo["cantidad"] = 1
            grupos.append(nuevo_grupo)
    return grupos


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def main():
    asegurar_instancia_unica()
    configurar_consola()

    print("=== Monitor de emergencias WhatsApp (Evolution API + Ollama) ===")
    print(f"Instancia: {EVOLUTION_INSTANCE}")
    print(f"Revisando cada {POLL_INTERVAL_SECONDS}s. Ctrl+C para salir.\n")

    evitar_suspension_pantalla()
    state = load_state()
    print(f"Poniendose al dia desde: {datetime.fromtimestamp(state['last_timestamp'])}")

    init_db()
    contactos = load_contactos()
    if contactos:
        print(f"[INFO] {len(contactos)} contacto(s) cargados desde {CONTACTS_CSV}")

    grupos_wa = load_grupos()
    if grupos_wa:
        print(f"[INFO] {len(grupos_wa)} grupo(s) de WhatsApp cargados")

    participantes_sedapal = cargar_participantes_autorizados()
    if participantes_sedapal is not None:
        print(f"[INFO] {len(participantes_sedapal)} numero(s) autorizados para consulta SEDAPAL (grupo configurado)")

    try:
        while True:
            try:
                nuevos = get_new_messages(state["last_timestamp"])
                grupos = agrupar_mensajes(nuevos)
            except Exception as e:
                print(f"[ERROR] fallo trayendo/agrupando mensajes, se reintenta en el siguiente ciclo: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            if nuevos:
                print(f"[INFO] {len(nuevos)} mensaje(s) nuevo(s) -> {len(grupos)} grupo(s) a evaluar.")

            for msg in grupos:
                try:
                    if msg["cantidad"] > 1:
                        print(f" -> {msg['remitente']}: [{msg['cantidad']} mensajes seguidos agrupados] {msg['texto'][:80]}")
                    else:
                        print(f" -> {msg['remitente']}: {msg['texto'][:80]}")

                    contacto_conocido = msg["telefono"] in contactos
                    nombre = (
                        contactos.get(msg["telefono"])
                        or msg.get("push_name")
                        or msg["telefono"]
                        or msg["remitente"]
                    )
                    media_tipo = msg["media"]["tipo"] if msg["media"] else None

                    # consulta de deuda SEDAPAL ("deuda:1234567"): no pasa por la
                    # clasificacion de emergencias, se responde directo en el chat de
                    # origen. Solo para miembros del grupo autorizado (config.ini
                    # [sedapal]); si no hay grupo configurado, nadie queda autorizado
                    # (sin respaldo de contactos.csv - el servicio es exclusivo de ese grupo)
                    autorizado_sedapal = participantes_sedapal is not None and msg["telefono"] in participantes_sedapal
                    partes_deuda = manejar_consulta_deuda(msg["texto"], autorizado_sedapal, destino=msg["remitente"])
                    if partes_deuda is not None:
                        # se responde al mismo chat de donde vino la pregunta (remitente):
                        # si fue en un grupo, la respuesta queda visible ahi para todo el grupo
                        enviado = enviar_mensajes_en_partes(msg["remitente"], partes_deuda)
                        print(f"   [i] respuesta de SEDAPAL {'enviada' if enviado else 'NO enviada'} a {msg['remitente']}")
                        guardar_mensaje(
                            msg["remitente"], msg["telefono"], nombre, msg["texto"], "consulta_sedapal",
                            emergencia=False, resumen=" | ".join(partes_deuda), timestamp=msg["timestamp"],
                            cantidad_agrupados=msg["cantidad"],
                        )
                        continue

                    if not msg["texto"]:
                        # media sin texto/caption ni mensajes cercanos: no hay nada que
                        # analizar, no se alerta, solo se registra para el historial
                        if msg["media"]:
                            guardar_mensaje(
                                msg["remitente"], msg["telefono"], nombre, "", media_tipo,
                                emergencia=False, timestamp=msg["timestamp"], cantidad_agrupados=msg["cantidad"],
                            )
                    else:
                        clasif = clasificar_emergencia(msg["texto"])

                        if clasif["emergencia"]:
                            nombre_grupo = grupos_wa.get(msg["remitente"]) if msg["remitente"].endswith("@g.us") else None
                            mensaje = construir_mensaje_alerta(
                                nombre, msg["telefono"], clasif, msg["ubicacion"], contacto_conocido, nombre_grupo
                            )
                            thumbnail = msg["media"].get("thumbnail_b64") if msg["media"] else None
                            de_mostrar = f"{nombre} - Grupo: {nombre_grupo}" if nombre_grupo else nombre
                            print("   [!] EMERGENCIA DETECTADA -> disparando alerta")
                            mostrar_alerta(de_mostrar, mensaje, thumbnail, segundos_auto_cierre=ALERTA_AUTO_CIERRE_SEGUNDOS)

                        guardar_mensaje(
                            msg["remitente"], msg["telefono"], nombre, msg["texto"], media_tipo,
                            emergencia=clasif["emergencia"], tipo=clasif["tipo"], resumen=clasif["resumen"],
                            direccion=clasif["direccion"], timestamp=msg["timestamp"], cantidad_agrupados=msg["cantidad"],
                        )
                except Exception as e:
                    # un mensaje raro no debe tumbar el monitor: se registra el error y se sigue
                    print(f"[ERROR] fallo procesando un mensaje, se omite y se continua: {e}")
                finally:
                    # el timestamp se avanza SIEMPRE, incluso si fallo el procesamiento,
                    # para no quedar reintentando por siempre el mismo mensaje roto
                    state["last_timestamp"] = max(state["last_timestamp"], msg["timestamp"])
                    save_state(state)

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitor detenido por el usuario.")
        sys.exit(0)
    finally:
        restaurar_suspension_pantalla()


if __name__ == "__main__":
    main()