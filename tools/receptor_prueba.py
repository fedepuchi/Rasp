"""Receptor de prueba: NO es el backend, es para ver que sale la telemetria.

Se suscribe a los mismos temas que `backend/src/services/mqttIngest.ts` y va
imprimiendo lo que publica el monitor. Sirve para separar "el Pi no publica" de
"el backend no guarda" sin tener que levantar todo el Compose.

    python tools/receptor_prueba.py
    python tools/receptor_prueba.py --host 192.168.0.50 --guardar datos.jsonl
    python tools/receptor_prueba.py --sin-tls --puerto 1883   # broker de juguete

Toma el broker, el usuario y la CA del mismo sitio que el monitor (iot_env), asi
que si el monitor llega, esto tambien.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iot_env  # noqa: E402

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise SystemExit("Falta paho-mqtt.  pip install paho-mqtt")

ARCHIVO = None
CONTADOR = {"telemetry": 0, "status": 0, "invalidos": 0}


def al_conectar(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print(f"[mqtt] conexion rechazada: {reason_code}")
        return
    temas = [(userdata["telemetry"], 1), (userdata["status"], 1)]
    client.subscribe(temas)
    print(f"[mqtt] conectado. Escuchando:")
    for tema, _ in temas:
        print(f"         {tema}")
    print()


def al_recibir(client, userdata, msg):
    stamp = time.strftime("%H:%M:%S")
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        CONTADOR["invalidos"] += 1
        print(f"[{stamp}] {msg.topic}  !! no es JSON valido", flush=True)
        return

    if msg.topic.endswith("/status"):
        CONTADOR["status"] += 1
        estado = payload.get("status")
        marca = "RETENIDO" if msg.retain else ""
        print(f"[{stamp}] ESTADO  {payload.get('device')}  {estado}  {marca}",
              flush=True)
    else:
        CONTADOR["telemetry"] += 1
        print(f"[{stamp}] {payload.get('variable'):<10} "
              f"{payload.get('value'):>8} {payload.get('unit'):<4} "
              f"ts={payload.get('ts'):.3f}  {payload.get('hash', '')[:12]}...",
              flush=True)
        _validar(payload)

    if ARCHIVO is not None:
        ARCHIVO.write(json.dumps(payload, ensure_ascii=False) + "\n")
        ARCHIVO.flush()


def _validar(payload: dict) -> None:
    """Las mismas comprobaciones que hace `telemetrySchema` con zod.

    Vale la pena repetirlas aca: si el payload no valida, el backend lo tira con
    un warning en su log y desde el Pi no se nota nada.
    """
    problemas = []
    for campo in ("device", "variable", "value", "unit", "ts", "hash"):
        if campo not in payload:
            problemas.append(f"falta '{campo}'")
    if isinstance(payload.get("hash"), str) and len(payload["hash"]) != 64:
        problemas.append(f"hash de {len(payload['hash'])} caracteres, tienen que ser 64")
    if not isinstance(payload.get("value"), (int, float)):
        problemas.append("value no es numero")
    if isinstance(payload.get("device"), str) and len(payload["device"]) > 50:
        problemas.append("device de mas de 50 caracteres")
    if problemas:
        CONTADOR["invalidos"] += 1
        print(f"           !! el backend lo va a rechazar: {', '.join(problemas)}",
              flush=True)


def main() -> None:
    global ARCHIVO
    parser = argparse.ArgumentParser(description="Receptor MQTT de prueba")
    parser.add_argument("--host", default=iot_env.MQTT_HOST)
    parser.add_argument("--puerto", type=int, default=iot_env.MQTT_PORT)
    parser.add_argument("--device", default="+",
                        help="codigo del dispositivo, o + para escuchar todos")
    parser.add_argument("--sin-tls", action="store_true",
                        help="broker sin TLS (SIAPPC no acepta esto)")
    parser.add_argument("--guardar", metavar="ARCHIVO.jsonl")
    args = parser.parse_args()

    if args.guardar:
        ARCHIVO = open(args.guardar, "a", encoding="utf-8")

    userdata = {
        "telemetry": iot_env.telemetry_topic(args.device),
        "status": iot_env.status_topic(args.device),
    }
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="receptor-prueba", userdata=userdata)
    if iot_env.MQTT_USER:
        client.username_pw_set(iot_env.MQTT_USER, iot_env.MQTT_PASSWORD)

    if not args.sin_tls and iot_env.MQTT_TLS:
        if not Path(iot_env.MQTT_CA_FILE).is_file():
            raise SystemExit(
                f"No se encuentra la CA en {iot_env.MQTT_CA_FILE}.\n"
                "Copiala desde infra/mosquitto/certs/ca.crt, o usa --sin-tls "
                "contra un broker de juguete.")
        client.tls_set(ca_certs=iot_env.MQTT_CA_FILE,
                       certfile=iot_env.MQTT_CLIENT_CERT_FILE,
                       keyfile=iot_env.MQTT_CLIENT_KEY_FILE,
                       cert_reqs=ssl.CERT_REQUIRED,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)

    client.on_connect = al_conectar
    client.on_message = al_recibir

    print(f"Conectando a {args.host}:{args.puerto}...")
    client.connect(args.host, args.puerto, keepalive=30)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print(f"\nRecibidos: {CONTADOR['telemetry']} lecturas, "
              f"{CONTADOR['status']} de estado, {CONTADOR['invalidos']} invalidos")
    finally:
        if ARCHIVO is not None:
            ARCHIVO.close()


if __name__ == "__main__":
    main()
