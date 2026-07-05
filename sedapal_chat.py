"""
sedapal_chat.py
----------------
Cliente para consultar el saldo/deuda de un numero de suministro (NIS) de
SEDAPAL a traves de su chat de soporte (Genesys Cloud Messenger).

El protocolo NO es una API publica documentada por SEDAPAL/Genesys: se
obtuvo capturando (HAR) una conversacion real con el bot en
https://sedapal-webchat-773948435929.southamerica-west1.run.app/
Puede romperse si SEDAPAL cambia el bot, el flujo de preguntas, o la
version del widget (viene en la URL: application=messenger-X.Y.Z).

Flujo del bot (tal cual se capturo):
    1. configureSession
    2. Event Presence Join       -> bot saluda y pide el NOMBRE
    3. Text: <cualquier nombre>  -> bot pide el NUMERO DE SUMINISTRO (7 digitos)
    4. Text: <NIS>               -> bot saluda por nombre y muestra un menu
    5. Structured "2. Consulta de Saldo" -> bot responde con el saldo/deuda

Uso:
    from sedapal_chat import consultar_deuda
    resultado = consultar_deuda("2525123")
    print(resultado)   # {"ok": True, "texto": "Usted tiene 1 recibo(s)..."}
"""

import json
import time
import uuid

from websockets.sync.client import connect

DEPLOYMENT_ID = "7007fd59-a883-4350-b1e5-eee795fc22c6"
WS_URL = (
    "wss://webmessaging.sae1.pure.cloud/v1"
    f"?deploymentId={DEPLOYMENT_ID}&application=messenger-2.16.2"
)

TIMEOUT_SEGUNDOS = 20  # cuanto esperar como maximo por cada respuesta del bot


def _uuid():
    return str(uuid.uuid4())


def _recv_raw(ws, timeout):
    try:
        raw = ws.recv(timeout=timeout)
    except TimeoutError:
        return None
    return json.loads(raw)


def _recibir_texto(ws, timeout=TIMEOUT_SEGUNDOS):
    """
    Espera el proximo mensaje de TEXTO que manda el bot (direction=Outbound,
    type Text o Structured) e ignora todo lo demas (eventos de Presence/
    Typing, ecos de nuestros propios mensajes Inbound, respuestas de sesion).
    """
    limite = time.time() + timeout
    while True:
        restante = limite - time.time()
        if restante <= 0:
            return None
        msg = _recv_raw(ws, restante)
        if msg is None:
            return None
        body = msg.get("body", {})
        if body.get("direction") == "Outbound" and body.get("type") in ("Text", "Structured"):
            texto = body.get("text")
            if texto:
                return texto


def consultar_deuda(nis, nombre="Consulta"):
    """
    Consulta el saldo/deuda de un numero de suministro (NIS) de 7 digitos
    a traves del chat de SEDAPAL. Devuelve {"ok": bool, "texto": str}.
    """
    nis = str(nis).strip()
    if not nis.isdigit() or len(nis) != 7:
        return {"ok": False, "texto": "El numero de suministro debe tener 7 digitos."}

    token = _uuid()

    def _mandar_texto(ws, texto):
        ws.send(json.dumps({
            "action": "onMessage",
            "token": token,
            "tracingId": _uuid(),
            "message": {"metadata": {"id": _uuid()}, "type": "Text", "text": texto},
        }))

    try:
        with connect(WS_URL, open_timeout=10) as ws:
            ws.send(json.dumps({
                "deploymentId": DEPLOYMENT_ID,
                "token": token,
                "mode": 0,
                "journeyContext": {
                    "customer": {"id": token, "idType": "cookie"},
                    "customerSession": {"id": _uuid(), "type": "web"},
                },
                "startNew": True,
                "action": "configureSession",
            }))
            _recv_raw(ws, 10)  # SessionResponse, no nos interesa el contenido

            ws.send(json.dumps({
                "action": "onMessage",
                "token": token,
                "tracingId": _uuid(),
                "message": {
                    "metadata": {"id": _uuid()},
                    "type": "Event",
                    "events": [{"eventType": "Presence", "presence": {"type": "Join"}}],
                },
            }))
            _recibir_texto(ws)  # "Bienvenido a SEDAPAL"
            pide_nombre = _recibir_texto(ws)  # "Por favor, ingrese su nombre"
            if not pide_nombre:
                return {"ok": False, "texto": "El bot de SEDAPAL no respondio (posible cambio de flujo)."}

            _mandar_texto(ws, nombre)
            pide_nis = _recibir_texto(ws)
            if not pide_nis or "suministro" not in pide_nis.lower():
                return {"ok": False, "texto": pide_nis or "El bot no pidio el numero de suministro como se esperaba."}

            _mandar_texto(ws, nis)
            _recibir_texto(ws)  # "Hola X, soy Clarita..."
            menu = _recibir_texto(ws)
            if not menu or ("consulta de saldo" not in menu.lower() and "menú" not in menu.lower()):
                # el bot no reconocio el NIS o cambio el flujo: devolvemos lo que dijo
                return {"ok": False, "texto": menu or "El bot no reconocio el numero de suministro."}

            ws.send(json.dumps({
                "action": "onMessage",
                "token": token,
                "tracingId": _uuid(),
                "message": {
                    "metadata": {"id": _uuid()},
                    "type": "Structured",
                    "text": "2. Consulta de Saldo",
                    "content": [{
                        "contentType": "ButtonResponse",
                        "buttonResponse": {
                            "type": "QuickReply",
                            "text": "2. Consulta de Saldo",
                            "payload": "2. Consulta de Saldo",
                        },
                    }],
                },
            }))
            respuesta = _recibir_texto(ws)
            if not respuesta:
                return {"ok": False, "texto": "SEDAPAL no respondio a tiempo con el saldo."}
            return {"ok": True, "texto": respuesta}

    except Exception as e:
        return {"ok": False, "texto": f"No se pudo consultar SEDAPAL: {e}"}


if __name__ == "__main__":
    import sys
    nis_prueba = sys.argv[1] if len(sys.argv) > 1 else input("Numero de suministro (7 digitos): ")
    print(consultar_deuda(nis_prueba))
