"""
configurar_webhook.py
----------------------
Configura, consulta o desactiva el webhook de Evolution API (evento
MESSAGES_UPSERT) para la instancia definida en monitor.py, de forma que
Evolution avise al instante en vez de que monitor.py tenga que preguntar
cada POLL_INTERVAL_SECONDS.

Uso:
    python configurar_webhook.py https://tu-url-publica.com/webhook
    python configurar_webhook.py --status
    python configurar_webhook.py --disable
"""

import argparse
import json
import sys

import requests

import monitor  # reusa EVOLUTION_API_URL / EVOLUTION_API_KEY / EVOLUTION_INSTANCE

EVENTS = ["MESSAGES_UPSERT"]


def _headers():
    return {"apikey": monitor.EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _url_set():
    return f"{monitor.EVOLUTION_API_URL}/webhook/set/{monitor.EVOLUTION_INSTANCE}"


def _url_find():
    return f"{monitor.EVOLUTION_API_URL}/webhook/find/{monitor.EVOLUTION_INSTANCE}"


def mostrar_estado():
    r = requests.get(_url_find(), headers=_headers(), timeout=15)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


def configurar(url_webhook, habilitar=True):
    # se fuerzan explicitamente para no heredar un valor previo que haya
    # quedado guardado en Evolution (ej: si alguien lo cambio desde el
    # manager web). webhookByEvents=true hace que Evolution le agregue el
    # nombre del evento a la URL, y webhook_server.py solo acepta la ruta
    # exacta configurada.
    body = {
        "webhook": {
            "enabled": habilitar,
            "url": url_webhook,
            "events": EVENTS,
            "webhookByEvents": False,
            "webhookBase64": False,
        }
    }
    r = requests.post(_url_set(), headers=_headers(), json=body, timeout=15)
    if r.status_code not in (200, 201):
        print(f"[ERROR] Evolution respondio {r.status_code}: {r.text}")
        sys.exit(1)
    print("[OK] Webhook configurado:")
    print(json.dumps(r.json(), indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="URL publica que apunta a webhook_server.py (ej: https://xxxx.trycloudflare.com/webhook)")
    ap.add_argument("--status", action="store_true", help="muestra la configuracion actual del webhook")
    ap.add_argument("--disable", action="store_true", help="desactiva el webhook (vuelve a usar solo polling)")
    args = ap.parse_args()

    if args.status:
        mostrar_estado()
    elif args.disable:
        mostrar_estado_previo = None
        try:
            r = requests.get(_url_find(), headers=_headers(), timeout=15)
            r.raise_for_status()
            mostrar_estado_previo = r.json()
        except requests.RequestException:
            pass
        url_actual = (mostrar_estado_previo or {}).get("url") or "https://example.invalid/webhook"
        configurar(url_actual, habilitar=False)
        print("[OK] Webhook desactivado (vuelve a depender del polling de monitor.py).")
    elif args.url:
        configurar(args.url, habilitar=True)
    else:
        ap.error("debes indicar una URL, o usar --status / --disable")


if __name__ == "__main__":
    main()
