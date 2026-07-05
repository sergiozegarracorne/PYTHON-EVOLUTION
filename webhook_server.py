"""
webhook_server.py
------------------
Servidor HTTP que recibe los webhooks de Evolution API (evento
MESSAGES_UPSERT) y dispara la alerta de emergencia AL INSTANTE, sin
esperar el POLL_INTERVAL_SECONDS de monitor.py.

Reusa toda la logica de monitor.py (extraccion de texto/ubicacion,
clasificacion con Ollama, cruce con contactos.csv, alerta pantalla+voz).

IMPORTANTE - conectividad:
    Evolution API corre en un servidor remoto (jsjperu.net) y debe poder
    alcanzar este servidor por HTTP. Si este script corre en tu PC local,
    necesitas exponerlo con un tunel, por ejemplo:

        cloudflared tunnel --url http://localhost:8500
        ngrok http 8500

    Copia la URL publica que te den (https://algo.trycloudflare.com o
    https://algo.ngrok-free.app) y luego registra el webhook con:

        python configurar_webhook.py https://algo.trycloudflare.com/webhook

Uso:
    python webhook_server.py

Corre con un icono en la bandeja del sistema (junto al reloj). La consola se
oculta al arrancar; el menu del icono deja verla de nuevo (log en vivo),
reiniciar el proceso o cerrarlo. Requiere ademas: pip install pystray Pillow
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pystray
from PIL import Image, ImageDraw

import monitor  # reusa toda la logica de clasificacion/alerta/contactos

# se verifica ANTES de cargar contactos.csv / traer los grupos de Evolution
# (mas abajo), para que una segunda instancia se cierre de inmediato en vez
# de hacer todo ese trabajo de arranque y recien ahi darse cuenta que sobra.
monitor.asegurar_instancia_unica()

# host/puerto/ruta y las ventanas de agrupacion salen de config.ini (via
# monitor.py), para no tener dos copias de la misma configuracion desincronizadas
HOST = monitor.WEBHOOK_HOST
PORT = monitor.WEBHOOK_PORT
RUTA = monitor.WEBHOOK_RUTA
VENTANA_AGRUPACION_SEGUNDOS = monitor.VENTANA_AGRUPACION_SEGUNDOS
VENTANA_AGRUPACION_MEDIA_SEGUNDOS = monitor.VENTANA_AGRUPACION_MEDIA_SEGUNDOS

contactos = monitor.load_contactos()
grupos_wa = monitor.load_grupos()
participantes_sedapal = monitor.cargar_participantes_autorizados()  # None si no hay grupo configurado

# tkinter no es seguro de usar desde varios hilos a la vez: si llegan dos
# emergencias casi juntas, cada una se procesa en su propio hilo, pero la
# ventana de alerta se muestra siempre desde este UNICO hilo consumidor.
cola_alertas = queue.Queue()

# telefono -> {"textos": [...], "media": None o dict, "ubicacion": ..., "nombre": ...,
#              "telefono": ..., "remitente": ..., "timer": Timer}
pendientes = {}
pendientes_lock = threading.Lock()


def hilo_alertas():
    while True:
        nombre, mensaje, imagen_b64 = cola_alertas.get()
        try:
            monitor.mostrar_alerta(nombre, mensaje, imagen_b64)
        except Exception as e:
            # una alerta que falle en mostrarse no debe matar el hilo: si no,
            # ninguna alerta futura se volveria a mostrar por el resto de la sesion
            print(f"[ERROR] fallo mostrando una alerta: {e}")


def hilo_vigilancia_conexion():
    """
    Cada monitor.INTERVALO_VERIFICACION_CONEXION segundos revisa (via la
    API de Evolution) si la sesion de WhatsApp sigue "open". Si deja de
    estarlo durante monitor.CONFIRMACIONES_ANTES_DE_ALERTAR chequeos
    seguidos (para no saltar por un corte de red/VPN de un instante),
    levanta la pantalla completa negra con el QR y se queda ahi (bloqueado
    en esa llamada) hasta que vuelve a detectar la sesion conectada.
    """
    fallos_seguidos = 0
    while True:
        try:
            time.sleep(monitor.INTERVALO_VERIFICACION_CONEXION)
            estado = monitor.consultar_estado_conexion()

            if estado == "open":
                fallos_seguidos = 0
                continue
            if estado is None:
                # no se pudo ni consultar el estado (API/red caida): no se
                # cuenta como sesion cerrada, podria ser otro problema
                continue

            fallos_seguidos += 1
            print(
                f"[WARN] sesion de WhatsApp no esta 'open' (estado: {estado}), "
                f"chequeo {fallos_seguidos}/{monitor.CONFIRMACIONES_ANTES_DE_ALERTAR}"
            )
            if fallos_seguidos >= monitor.CONFIRMACIONES_ANTES_DE_ALERTAR:
                print("[ALERTA] sesion de WhatsApp cerrada -> mostrando pantalla de reconexion (QR)")
                monitor.mostrar_alerta_desconexion()
                print("[INFO] sesion de WhatsApp reconectada, se sigue vigilando normalmente")
                fallos_seguidos = 0
        except Exception as e:
            # un fallo aca no debe tumbar la vigilancia para siempre: se
            # registra y se sigue en el proximo ciclo
            print(f"[ERROR] fallo en la vigilancia de conexion, se reintenta en el siguiente ciclo: {e}")


def _flush_pendiente(telefono):
    """Se dispara cuando pasa la ventana de agrupacion sin nuevos mensajes de este numero."""
    try:
        _flush_pendiente_interno(telefono)
    except Exception as e:
        print(f"[ERROR] fallo evaluando el grupo de mensajes de {telefono}, se descarta: {e}")


def _flush_pendiente_interno(telefono):
    with pendientes_lock:
        grupo = pendientes.pop(telefono, None)
    if not grupo:
        return

    texto = " ".join(t for t in grupo["textos"] if t).strip()
    cantidad = len(grupo["textos"])
    media = grupo.get("media")
    media_tipo = media["tipo"] if media else None

    if cantidad > 1 or media:
        detalle = f"{cantidad} mensaje(s)" + (f" + {media_tipo}" if media else "")
        print(f"   [i] {detalle} de {grupo['nombre']} agrupados -> evaluando juntos")

    if not texto:
        # solo llego media sin texto/caption cerca: no hay nada que analizar,
        # no se alerta, solo se registra para el historial
        if media:
            monitor.guardar_mensaje(grupo["remitente"], telefono, grupo["nombre"], "", media_tipo, emergencia=False)
        return

    contacto_conocido = telefono in contactos
    clasif = monitor.clasificar_emergencia(texto)

    monitor.guardar_mensaje(
        grupo["remitente"], telefono, grupo["nombre"], texto, media_tipo,
        emergencia=clasif["emergencia"], tipo=clasif["tipo"], resumen=clasif["resumen"],
        direccion=clasif["direccion"], cantidad_agrupados=cantidad,
    )

    if not clasif["emergencia"]:
        return

    nombre_grupo = grupos_wa.get(grupo["remitente"]) if grupo["remitente"].endswith("@g.us") else None
    mensaje = monitor.construir_mensaje_alerta(
        grupo["nombre"], telefono, clasif, grupo["ubicacion"], contacto_conocido, nombre_grupo
    )
    thumbnail = media.get("thumbnail_b64") if media else None
    de_mostrar = f"{grupo['nombre']} - Grupo: {nombre_grupo}" if nombre_grupo else grupo["nombre"]
    print("   [!] EMERGENCIA DETECTADA (webhook) -> encolando alerta")
    cola_alertas.put((de_mostrar, mensaje, thumbnail))


def procesar_mensaje(msg):
    key = msg.get("key", {})
    if key.get("fromMe"):
        return

    texto = monitor.extract_text(msg)
    media = monitor.extract_media(msg)
    ubicacion = monitor.extract_ubicacion(msg)
    if not texto and not media:
        return

    remitente = key.get("remoteJid", "")
    remitente_jid = (
        key.get("participantAlt")
        or key.get("remoteJidAlt")
        or key.get("participant")
        or key.get("remoteJid", "")
    )
    telefono = monitor.extraer_telefono(remitente_jid)
    push_name = msg.get("pushName", "")
    nombre = contactos.get(telefono) or push_name or telefono or remitente

    etiqueta_media = f" [{media['tipo']}]" if media else ""
    print(f" -> [webhook] {remitente}: {(texto or '')[:80]}{etiqueta_media}")

    # consulta de deuda SEDAPAL ("deuda:1234567"): no pasa por la agrupacion
    # ni la clasificacion de emergencias, se responde directo en el chat de
    # origen. Solo para miembros del grupo autorizado (config.ini [sedapal]);
    # si no hay grupo configurado, nadie queda autorizado (sin respaldo de
    # contactos.csv - el servicio es exclusivo de ese grupo)
    autorizado_sedapal = participantes_sedapal is not None and telefono in participantes_sedapal
    partes_deuda = monitor.manejar_consulta_deuda(texto, autorizado_sedapal, destino=remitente)
    if partes_deuda is not None:
        # se responde al mismo chat de donde vino la pregunta (remitente): si
        # fue en un grupo, la respuesta queda visible ahi para todo el grupo
        enviado = monitor.enviar_mensajes_en_partes(remitente, partes_deuda)
        print(f"   [i] respuesta de SEDAPAL {'enviada' if enviado else 'NO enviada'} a {remitente}")
        monitor.guardar_mensaje(
            remitente, telefono, nombre, texto, "consulta_sedapal",
            emergencia=False, resumen=" | ".join(partes_deuda),
        )
        return

    texto_completo = texto or ""
    if media and media.get("caption"):
        texto_completo = (texto_completo + " " + media["caption"]).strip()

    # texto y media se agrupan igual (misma cola por numero); la media nunca
    # dispara alerta por si sola, solo se une al texto cercano para evaluarlo junto
    with pendientes_lock:
        grupo = pendientes.get(telefono)
        if grupo:
            grupo["timer"].cancel()
            if texto_completo:
                grupo["textos"].append(texto_completo)
            grupo["ubicacion"] = grupo["ubicacion"] or ubicacion
            if media and not grupo.get("media"):
                grupo["media"] = media
        else:
            grupo = {
                "textos": [texto_completo] if texto_completo else [],
                "media": media,
                "ubicacion": ubicacion,
                "nombre": nombre,
                "telefono": telefono,
                "remitente": remitente,
            }
            pendientes[telefono] = grupo

        hay_media = bool(media) or bool(grupo.get("media"))
        ventana = VENTANA_AGRUPACION_MEDIA_SEGUNDOS if hay_media else VENTANA_AGRUPACION_SEGUNDOS
        timer = threading.Timer(ventana, _flush_pendiente, args=(telefono,))
        timer.daemon = True
        grupo["timer"] = timer
        timer.start()


def procesar_evento(payload, evento_de_ruta=None):
    """Se corre en un hilo aparte para responder rapido el HTTP al webhook."""
    evento = (payload.get("event") or evento_de_ruta or "").lower().replace("_", ".").replace("-", ".")
    if evento != "messages.upsert":
        print(f"[DEBUG] evento ignorado (no es messages.upsert): {evento!r}")
        return

    data = payload.get("data")
    if not data:
        print("[DEBUG] evento messages.upsert sin 'data' en el payload")
        return

    mensajes = data if isinstance(data, list) else [data]
    for msg in mensajes:
        try:
            procesar_mensaje(msg)
        except Exception as e:
            print(f"[WARN] no se pudo procesar un mensaje del webhook: {e}")


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silenciamos el log default de http.server; usamos nuestros propios prints

    def do_POST(self):
        try:
            self._manejar_post()
        except Exception as e:
            # cualquier fallo aca no debe tumbar el servidor entero, solo esta peticion
            print(f"[ERROR] fallo manejando una peticion del webhook: {e}")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def _manejar_post(self):
        ruta = self.path.split("?")[0]
        print(f"[DEBUG] POST recibido desde {self.client_address[0]} -> {self.path}")

        # con "webhookByEvents" activado (config global del servidor Evolution,
        # no controlable por instancia), Evolution le agrega el nombre del
        # evento a la URL: /webhook/messages-upsert, /webhook/send-message, etc.
        if ruta == RUTA:
            evento_de_ruta = None
        elif ruta.startswith(RUTA + "/"):
            evento_de_ruta = ruta[len(RUTA) + 1:]
        else:
            print(f"[DEBUG] ruta no reconocida, respondiendo 404: {ruta!r}")
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        # respondemos rapido: Evolution espera un 200 casi inmediato
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            print("[WARN] webhook recibio un body que no es JSON valido")
            return

        threading.Thread(target=procesar_evento, args=(payload, evento_de_ruta), daemon=True).start()

    def do_GET(self):
        # util para confirmar desde el navegador que el tunel/puerto esta vivo
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"webhook activo")


# ============================================================
# ICONO DE BANDEJA (junto al reloj)
# ============================================================

SW_HIDE = 0


def ocultar_consola():
    """
    Oculta (una sola vez, al arrancar) la consola desde la que se lanzo el
    programa. No se vuelve a mostrar nunca: "Ver log en tiempo real" abre
    una ventana APARTE (proceso independiente) que solo lee LOG_FILE, para
    no depender de la consola propia del proceso (en Windows Terminal,
    tocarla puede terminar matando el programa entero).
    """
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)


def _crear_icono():
    """Icono generado a mano (circulo rojo + signo de exclamacion), sin depender de un .ico externo."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(122, 31, 31, 255))
    draw.rectangle((29, 14, 35, 38), fill="white")
    draw.rectangle((29, 44, 35, 50), fill="white")
    return img


def _ver_log(icon, item):
    """
    Abre una ventana de PowerShell APARTE (proceso independiente, sin
    relacion con el proceso principal) que solo va leyendo LOG_FILE en
    vivo. Cerrar esa ventana no afecta al monitor para nada.
    """
    try:
        comando = (
            f"$Host.UI.RawUI.WindowTitle = 'Monitor de Emergencias - Log'; "
            f"Get-Content -Path '{monitor.LOG_FILE}' -Wait -Tail 200"
        )
        subprocess.Popen(
            ["powershell", "-NoExit", "-Command", comando],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as e:
        print(f"[WARN] no se pudo abrir la ventana de log: {e}")


_server = None  # referencia al HTTPServer activo, para poder cerrarlo desde el icono de bandeja


def _reiniciar(icon, item):
    try:
        print("[INFO] reiniciando por pedido del icono de bandeja...")
        icon.stop()
        if _server is not None:
            # hay que soltar el puerto 8500 ANTES de lanzar el proceso nuevo, si no
            # el nuevo puede fallar al arrancar porque el viejo todavia lo tiene
            _server.shutdown()
            _server.server_close()
        subprocess.Popen([sys.executable, os.path.abspath(__file__)])
        # server.shutdown() ya hizo que serve_forever() retorne en el hilo principal,
        # asi que main() sigue su curso normal (finally + fin del script) sin forzar salida
    except Exception as e:
        print(f"[ERROR] fallo reiniciando desde el icono de bandeja: {e}")


def _cerrar(icon, item):
    try:
        print("[INFO] cerrando por pedido del icono de bandeja...")
        icon.stop()
        if _server is not None:
            _server.shutdown()
            _server.server_close()
        # igual que en _reiniciar: shutdown() libera el hilo principal en serve_forever(),
        # que termina main() de forma normal (corre el finally y el proceso sale solo)
    except Exception as e:
        print(f"[ERROR] fallo cerrando desde el icono de bandeja: {e}")


def iniciar_icono_bandeja():
    menu = pystray.Menu(
        pystray.MenuItem("Ver log en tiempo real", _ver_log, default=True),
        pystray.MenuItem("Reiniciar", _reiniciar),
        pystray.MenuItem("Cerrar", _cerrar),
    )
    icon = pystray.Icon("monitor_emergencias", _crear_icono(), "Monitor de Emergencias", menu)
    icon.run_detached()
    return icon


def main():
    global _server

    monitor.activar_log_en_archivo()
    monitor.configurar_consola()

    print("=== Webhook de emergencias (Evolution API) ===")
    print(f"Escuchando en http://{HOST}:{PORT}{RUTA}")
    print(f"Contactos cargados: {len(contactos)}")
    print(f"Grupos de WhatsApp cargados: {len(grupos_wa)}")
    if participantes_sedapal is not None:
        print(f"Autorizados para consulta SEDAPAL (grupo configurado): {len(participantes_sedapal)}")
    print("Recuerda exponer este puerto con un tunel y registrar la URL con")
    print("configurar_webhook.py. Ctrl+C para salir.\n")

    monitor.init_db()
    monitor.evitar_suspension_pantalla()
    threading.Thread(target=hilo_alertas, daemon=True).start()
    threading.Thread(target=hilo_vigilancia_conexion, daemon=True).start()

    try:
        server = ThreadingHTTPServer((HOST, PORT), WebhookHandler)
        _server = server
    except OSError as e:
        print(f"[ERROR] no se pudo abrir el puerto {PORT}, seguramente ya esta en uso: {e}")
        monitor.restaurar_suspension_pantalla()
        sys.exit(1)

    iniciar_icono_bandeja()
    print("Icono de bandeja listo (junto al reloj). Ocultando la consola...\n")
    ocultar_consola()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido por el usuario.")
    except Exception as e:
        print(f"[ERROR] el servidor se detuvo por un error inesperado: {e}")
    finally:
        monitor.restaurar_suspension_pantalla()


if __name__ == "__main__":
    main()
