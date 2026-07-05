"""
cliente_alertas.py
--------------------
Corre en cada PC de la red local (ej: 1.2.1.*) que NO tiene VPN ni acceso
directo a Evolution API. No habla con Evolution, Ollama ni SEDAPAL para
nada: solo consulta al backend (webhook_server_backend.py, en la PC que
SI tiene VPN, ej: 1.2.1.42:8500) por HTTP long-polling y muestra lo que
le llega:
    - la alerta de emergencia a pantalla completa (con foto y voz)
    - la pantalla negra con el QR cuando se cae la sesion de WhatsApp

Uso:
    python cliente_alertas.py

Config (se crea sola la primera vez, con la IP del backend a editar):
config_cliente.ini, junto a este archivo / al lado del .exe si esta
compilado.

Corre con un icono en la bandeja del sistema (junto al reloj), igual que
el backend. Requiere: pip install requests pystray Pillow pyttsx3
"""

import configparser
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

import pystray
import requests
from PIL import Image, ImageDraw

import alertas_ui
import monitor  # solo se reusan sus utilidades de sistema (instancia unica,
                 # consola, log a archivo, evitar suspension de pantalla) -
                 # NO se usa nada de Evolution/Ollama/SEDAPAL de este modulo aca.

monitor.asegurar_instancia_unica(monitor._NOMBRE_MUTEX_CLIENTE)

# ============================================================
# CONFIG propia del cliente (separada de config.ini del backend: este
# programa no necesita ni debe tener credenciales de Evolution/Groq)
# ============================================================

CONFIG_FILE = monitor.ruta("config_cliente.ini")

_CONFIG_DEFAULTS = {
    "backend": {
        # IP:puerto de la PC que corre webhook_server_backend.py (la unica
        # con VPN/acceso a Evolution). Edita esto con la IP real de tu red.
        "url": "http://1.2.1.42:8500",
    },
    "cliente": {
        # la ventana de alerta se cierra sola cuando la barra llega al final
        "alerta_auto_cierre_segundos": "30",
    },
}


def _cargar_config():
    cfg = configparser.ConfigParser()
    cfg.read_dict(_CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        cfg.read(CONFIG_FILE, encoding="utf-8")
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(
                "; Configuracion del cliente de alertas. Cambia la IP del\n"
                "; backend si hace falta y vuelve a arrancar el programa.\n\n"
            )
            cfg.write(f)
        print(f"[INFO] se creo {CONFIG_FILE} con los valores por defecto")
    return cfg


_cfg = _cargar_config()
BACKEND_URL = _cfg.get("backend", "url").rstrip("/")
ALERTA_AUTO_CIERRE_SEGUNDOS = _cfg.getint("cliente", "alerta_auto_cierre_segundos")

MI_ID = uuid.uuid4().hex  # identifica a esta PC ante el backend mientras dure el proceso

cola_alertas = queue.Queue()
pantalla_desconexion = None  # se crea en main(), antes de arrancar el polling


def hilo_alertas():
    """
    tkinter no es seguro de usar desde varios hilos a la vez: si llegan dos
    emergencias casi juntas, cada una se procesa en su propio hilo de
    polling, pero la ventana de alerta se muestra siempre desde este
    UNICO hilo consumidor.
    """
    while True:
        evento = cola_alertas.get()
        try:
            alertas_ui.mostrar_alerta(
                evento.get("nombre"), evento.get("mensaje"), evento.get("imagen_b64"),
                segundos_auto_cierre=ALERTA_AUTO_CIERRE_SEGUNDOS,
            )
        except Exception as e:
            # una alerta que falle en mostrarse no debe matar el hilo: si no,
            # ninguna alerta futura se volveria a mostrar por el resto de la sesion
            print(f"[ERROR] fallo mostrando una alerta: {e}")


def hilo_polling():
    """
    Long-polling contra el backend: cada peticion queda esperando hasta
    ~35s a que haya un evento nuevo (o vacio si no paso nada, y se vuelve
    a pedir de inmediato). Ante cualquier error de red (backend caido, PC
    sin conexion a la LAN, etc.) espera un poco y reintenta - este hilo
    nunca debe terminar, es la unica forma en que esta PC se entera de algo.
    """
    url = f"{BACKEND_URL}/eventos/siguiente"
    while True:
        try:
            resp = requests.get(url, params={"id": MI_ID}, timeout=35)
            if resp.status_code != 200:
                print(f"[WARN] el backend respondio {resp.status_code}, se reintenta")
                time.sleep(3)
                continue

            evento = resp.json()
            tipo = evento.get("tipo")

            if tipo == "emergencia":
                cola_alertas.put(evento)
            elif tipo == "sesion_cerrada":
                pantalla_desconexion.actualizar(evento.get("qr_base64"), evento.get("estado"))
            elif tipo == "sesion_reconectada":
                pantalla_desconexion.ocultar()
            # tipo == "ninguno": se acabo el tiempo de espera sin novedades, se vuelve a pedir

        except requests.RequestException as e:
            print(f"[WARN] no se pudo conectar al backend ({BACKEND_URL}): {e}")
            time.sleep(5)
        except Exception as e:
            print(f"[ERROR] fallo en el polling de eventos, se reintenta: {e}")
            time.sleep(5)


# ============================================================
# ICONO DE BANDEJA (junto al reloj) - igual que el backend
# ============================================================

SW_HIDE = 0


def ocultar_consola():
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)


def _crear_icono():
    """Icono generado a mano (circulo verde + signo de exclamacion), para distinguirlo del backend (rojo)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(31, 90, 31, 255))
    draw.rectangle((29, 14, 35, 38), fill="white")
    draw.rectangle((29, 44, 35, 50), fill="white")
    return img


def _ver_log(icon, item):
    try:
        comando = (
            f"$Host.UI.RawUI.WindowTitle = 'Cliente de Alertas - Log'; "
            f"Get-Content -Path '{monitor.LOG_FILE}' -Wait -Tail 200"
        )
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", comando],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as e:
        print(f"[WARN] no se pudo abrir la ventana de log: {e}")


def _reiniciar(icon, item):
    try:
        print("[INFO] reiniciando por pedido del icono de bandeja...")
        icon.stop()
        subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        os._exit(0)
    except Exception as e:
        print(f"[ERROR] fallo reiniciando desde el icono de bandeja: {e}")


def _cerrar(icon, item):
    try:
        print("[INFO] cerrando por pedido del icono de bandeja...")
        icon.stop()
        os._exit(0)
    except Exception as e:
        print(f"[ERROR] fallo cerrando desde el icono de bandeja: {e}")


def iniciar_icono_bandeja():
    menu = pystray.Menu(
        pystray.MenuItem("Ver log en tiempo real", _ver_log, default=True),
        pystray.MenuItem("Reiniciar", _reiniciar),
        pystray.MenuItem("Cerrar", _cerrar),
    )
    icon = pystray.Icon("cliente_alertas", _crear_icono(), "Cliente de Alertas WhatsApp", menu)
    icon.run_detached()
    return icon


def main():
    global pantalla_desconexion

    monitor.activar_log_en_archivo()
    monitor.configurar_consola()

    print("=== Cliente de alertas (red local) ===")
    print(f"Backend: {BACKEND_URL}")
    print(f"Id de este cliente: {MI_ID}")
    print("Ctrl+C para salir.\n")

    monitor.evitar_suspension_pantalla()

    pantalla_desconexion = alertas_ui.PantallaDesconexion()

    threading.Thread(target=hilo_alertas, daemon=True).start()
    threading.Thread(target=hilo_polling, daemon=True).start()

    iniciar_icono_bandeja()
    print("Icono de bandeja listo (junto al reloj). Ocultando la consola...\n")
    ocultar_consola()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nCliente detenido por el usuario.")
    finally:
        monitor.restaurar_suspension_pantalla()


if __name__ == "__main__":
    main()
