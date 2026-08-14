"""Publicacion de los signos vitales por MQTT hacia el backend de SIAPPC.

Antes esto armaba un sobre `monitor.v1` y lo mandaba por HTTP a
`POST /api/v1/ingest`. Ese endpoint no existe en SIAPPC: la telemetria que llega
a la base entra por MQTT, en `siappc/<dispositivo>/telemetry`, y la consume
`backend/src/services/mqttIngest.ts`. El contrato esta en JSON.md.

Diseno (el mismo de antes, con otro transporte):

  * El bucle principal solo encola: escribe la lectura en SQLite y sigue. Nunca
    toca la red, asi que una WiFi lenta no baja los FPS del monitor.
  * Un hilo aparte vacia la cola contra el broker, y una lectura solo se borra
    cuando el broker confirma la entrega (PUBACK de QoS 1).
  * Si el broker no esta, las lecturas se acumulan en el buffer y salen al
    reconectar. El buffer tira las mas viejas antes que las recientes.

Lo que **no** se publica:

  * **Las ondas** (ECG, pleth, resp). El backend guarda una fila por lectura en
    la tabla `lectura`, y el ECG son 250 muestras por segundo: no entra en ese
    modelo. Graficar la onda necesita su propia tabla y su propio tema MQTT.
    Mientras tanto las muestras se dibujan en pantalla y ahi se quedan.
  * **Las alarmas, la calidad de senial y el diagnostico del equipo.** El
    payload es una lectura suelta y no tiene donde meterlos. Las alarmas
    clinicas las vuelve a evaluar el backend sobre `hr`, `spo2`, `pr` y `resp`
    (`evaluateAlert` en mqttIngest.ts); las de pantalla siguen sonando aca.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - en la PC de desarrollo puede no estar
    mqtt = None

import iot_env
from config import BackendConfig, Config

from .buffer import Buffer

# Que se publica de cada foto del estado: (variable, campo de vitals_json, unidad).
#
# `variable` es lo que el backend guarda en `sensor.variable_medida`. No es un
# enum cerrado, pero estos nombres tienen que ser exactamente estos: son los que
# ya usa `iot/src/main.py` y sobre los que mqttIngest.ts evalua las alertas.
#
# Se lee de `vitals_json()` y no de los atributos crudos para publicar los
# mismos numeros que muestra la pantalla, con el mismo redondeo.
VARIABLES = (
    ("hr", "hr_bpm", "bpm"),
    ("spo2", "spo2_pct", "%"),
    ("pr", "pr_bpm", "bpm"),
    ("perfusion", "perfusion_index", "%"),
    # OJO: estimada de como la respiracion mueve la linea de base del pleth, no
    # medida con un sensor de flujo ni de impedancia. En pantalla va rotulada
    # como ESTIMADA; en el payload no hay donde decirlo, asi que queda escrito
    # en JSON.md y el backend le pone severidad "alta" como techo, nunca
    # "critica".
    ("resp", "resp_rpm_estimated", "rpm"),
)


def reading_hash(device: str, variable: str, timestamp: float, value: float) -> str:
    """Identidad de una lectura.

    Misma construccion que `iot/src/publisher.py:reading_hash`, a proposito: el
    backend tiene un indice unico sobre esta columna, asi que un reenvio tras
    una caida (o un duplicado de QoS 1) choca contra el indice y se descarta en
    vez de contarse dos veces. Si cambia alla, cambia aca.
    """
    raw = f"{device}|{variable}|{timestamp:.3f}|{value:.4f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_payload(device: str, timestamp: float, variable: str,
                  value: float, unit: str) -> dict:
    """La forma exacta que valida `telemetrySchema` en mqttIngest.ts."""
    return {
        "device": device,
        "variable": variable,
        "value": value,
        "unit": unit,
        "ts": timestamp,
        "hash": reading_hash(device, variable, timestamp, value),
    }


@dataclass
class PublisherStatus:
    enabled: bool = False
    connected: bool = False
    # Lecturas en la cola local esperando que el broker las confirme.
    pending_readings: int = 0
    sent_ok: int = 0
    failed: int = 0
    dropped: int = 0
    last_error: str | None = None
    last_success_t: float = 0.0


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg: BackendConfig = cfg.backend
        self.full_cfg = cfg
        self.device = cfg.device.device_id
        self.telemetry_topic = iot_env.telemetry_topic(self.device)
        self.status_topic = iot_env.status_topic(self.device)
        self.status = PublisherStatus(enabled=self.cfg.enabled)

        self._buffer: Buffer | None = None
        self._client = None
        # Protege el buffer: el bucle principal escribe y el hilo de red borra.
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_vitals_at = 0.0

    @property
    def destination(self) -> str:
        """Para imprimirlo al arrancar, como antes se imprimia la URL."""
        scheme = "mqtts" if iot_env.MQTT_TLS else "mqtt"
        return f"{scheme}://{self.cfg.host}:{self.cfg.port}/{self.telemetry_topic}"

    # -- conexion ----------------------------------------------------------

    def _build_client(self):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            # Sufijo `-monitor`: dentro de SIAPPC, `iot/src/` se conecta con el
            # DEVICE_CODE pelado, y dos clientes con el mismo id se echan uno al
            # otro del broker.
            client_id=f"{self.device}-monitor",
            # clean_session=False: el broker guarda los QoS 1 en vuelo si el Pi
            # se desconecta un momento.
            clean_session=False,
        )

        if iot_env.MQTT_USER:
            client.username_pw_set(iot_env.MQTT_USER, iot_env.MQTT_PASSWORD)

        if iot_env.MQTT_TLS:
            self._enable_tls(client)

        # Last Will: si el Pi desaparece sin avisar, el broker publica esto por
        # el y el tablero puede marcar el equipo como caido.
        client.will_set(self.status_topic, self._status_message("offline"),
                        qos=1, retain=True)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    def _enable_tls(self, client) -> None:
        """Cifra la conexion y valida quien esta del otro lado.

        La CA se comprueba aca y no en `tls_set` para dar un mensaje claro: el
        error de paho cuando el archivo no existe no dice cual falta.
        """
        if not Path(iot_env.MQTT_CA_FILE).is_file():
            raise SystemExit(
                f"[mqtt] no se encuentra la CA del broker en {iot_env.MQTT_CA_FILE}.\n"
                "        Copiala desde infra/mosquitto/certs/ca.crt (la genera\n"
                "        infra/mosquitto/gen-certs.sh) o apunta MQTT_CA_FILE al\n"
                "        archivo correcto. Si el broker no usa TLS, MQTT_TLS=false."
            )

        client.tls_set(
            ca_certs=iot_env.MQTT_CA_FILE,
            certfile=iot_env.MQTT_CLIENT_CERT_FILE,
            keyfile=iot_env.MQTT_CLIENT_KEY_FILE,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        # Nada de tls_insecure_set(True): el nombre del certificado tiene que
        # coincidir con el host. Si el broker se alcanza por IP, esa IP va como
        # SAN al emitir el certificado, no se desactiva la validacion.

    def _status_message(self, state: str) -> str:
        return json.dumps({"device": self.device, "status": state})

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.status.connected = False
            self.status.last_error = f"conexion rechazada: {reason_code}"
            print(f"[mqtt] conexion rechazada: {reason_code}")
            return

        self.status.connected = True
        self.status.last_error = None
        print(f"[mqtt] conectado a {self.destination}")
        client.publish(self.status_topic, self._status_message("online"),
                       qos=1, retain=True)
        # El vaciado lo hace el hilo de red, no este callback: `wait_for_publish`
        # bloquearia el bucle interno de paho.
        self._wake.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        # paho reintenta solo mientras loop_start siga vivo; aca solo se deja
        # constancia para que la pantalla lo muestre.
        self.status.connected = False
        print(f"[mqtt] desconectado ({reason_code}), reintentando...")

    # -- ciclo de vida -----------------------------------------------------

    def start(self) -> None:
        if not self.cfg.enabled:
            return

        if mqtt is None:
            self.cfg.enabled = False
            self.status.enabled = False
            self.status.last_error = "paho-mqtt no esta instalado"
            print("[mqtt] paho-mqtt no esta instalado, no se publica nada.\n"
                  "       pip install paho-mqtt")
            return

        if iot_env.env_file_missing():
            print(
                f"[mqtt] aviso: no hay {iot_env.env_file_path()}, asi que el broker,\n"
                "       el usuario y la CA son los valores por defecto. Copia el\n"
                "       .env.example (y el ca.crt), o arranca con --no-backend si\n"
                "       solo queres la pantalla."
            )

        self._buffer = Buffer(self.cfg.buffer_path, self.cfg.buffer_max_rows)
        self.status.pending_readings = self._buffer.count()

        self._client = self._build_client()
        # connect_async + loop_start no bloquean: el monitor arranca aunque el
        # broker este caido, y las lecturas se van al buffer.
        self._client.connect_async(self.cfg.host, self.cfg.port, keepalive=30)
        self._client.loop_start()

        self._thread = threading.Thread(target=self._run, name="publisher-mqtt",
                                        daemon=True)
        self._thread.start()

    # Mas que los 5 s que puede tardar un `wait_for_publish`: si no, el join
    # vence justo mientras el hilo de red esta esperando el PUBACK del ultimo
    # mensaje.
    def stop(self, timeout: float = 6.0) -> None:
        if not self.cfg.enabled or self._client is None:
            return

        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

        # El tema de estado no toca el buffer, asi que se publica siempre: es
        # como el tablero se entera de que este equipo se apago a proposito.
        if self._client.is_connected():
            info = self._client.publish(self.status_topic,
                                        self._status_message("offline"),
                                        qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=2)
            except (ValueError, RuntimeError):
                pass

        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

        # El buffer solo se cierra si el hilo de red ya termino. Si sigue vivo
        # lo esta usando, y cerrarlo desde aca le dejaria la base en la mano.
        worker_alive = self._thread is not None and self._thread.is_alive()
        if not worker_alive and self._buffer is not None:
            self._buffer.close()

    # -- entrada de datos --------------------------------------------------

    def tick(self, snapshot, alarm_manager=None) -> None:
        """Llamar una vez por frame: encola una tanda si toca.

        `alarm_manager` se acepta y se ignora: el payload de SIAPPC es una
        lectura suelta y no tiene donde llevar alarmas. Las clinicas las vuelve
        a evaluar el backend.
        """
        if not self.cfg.enabled or self._buffer is None:
            return

        now = time.monotonic()
        if now - self._last_vitals_at < self.cfg.vitals_interval_s:
            return
        self._last_vitals_at = now
        self.publish_vitals(snapshot)

    def publish_vitals(self, snapshot) -> None:
        """Una fila en `lectura` por cada signo vital que tenga valor."""
        if not self.cfg.enabled or self._buffer is None:
            return

        vitals = snapshot.vitals_json()
        # El mismo instante para toda la tanda: asi las cinco lecturas quedan
        # alineadas en la base y se pueden graficar juntas.
        timestamp = time.time()

        encoladas = 0
        for variable, campo, unit in VARIABLES:
            value = vitals.get(campo)
            if value is None:
                # Un `null` no es un cero: significa que no se pudo medir. No se
                # publica, y en la base simplemente no hay fila para ese
                # instante.
                continue
            payload = build_payload(self.device, timestamp, variable,
                                    float(value), unit)
            with self._lock:
                self._buffer.add(json.dumps(payload), payload["hash"])
            encoladas += 1

        if encoladas:
            with self._lock:
                self.status.pending_readings = self._buffer.count()
            self._wake.set()

    # -- hilo de red -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._client is None or not self._client.is_connected():
                continue
            self._flush()

    def _flush(self) -> None:
        if self._buffer is None:
            return
        with self._lock:
            pendientes = list(self._buffer.pending())

        for row_id, payload in pendientes:
            if self._stop.is_set():
                return
            info = self._client.publish(self.telemetry_topic, payload,
                                        qos=self.cfg.qos)
            # wait_for_publish confirma el PUBACK del broker. Sin esto se
            # borraria del buffer algo que quiza nunca llego.
            try:
                info.wait_for_publish(timeout=5)
            except (ValueError, RuntimeError) as exc:
                self.status.failed += 1
                self.status.last_error = str(exc)[:110]
                return
            if not info.is_published():
                self.status.failed += 1
                return

            with self._lock:
                self._buffer.drop(row_id)
            self.status.sent_ok += 1
            self.status.last_success_t = time.time()

        with self._lock:
            self.status.pending_readings = self._buffer.count()

    # -- compatibilidad con el contrato HTTP viejo -------------------------
    #
    # main.py llamaba a esto cuando el transporte era HTTP. Se dejan como no-op
    # para que el bucle principal no tenga que preguntar por el transporte, y
    # documentadas para que quede claro por que no hacen nada.

    def register_channel(self, *args, **kwargs) -> None:
        """Las ondas no se publican: no entran en la tabla `lectura`."""

    def add_samples(self, *args, **kwargs) -> None:
        """Idem: las muestras de onda se dibujan y ahi se quedan."""

    def session_start(self, extra: dict | None = None) -> None:
        """El equivalente en MQTT es el mensaje `status` retenido, y lo publica
        `_on_connect` cuando el broker acepta la conexion."""

    def session_end(self, reason: str = "shutdown") -> None:
        """Idem: lo publica `stop()` como `status: offline`."""

    def measurement(self, summary, index: int) -> None:
        """El resumen de una medicion no tiene forma en este contrato.

        `lectura` guarda un valor por fila y por instante, no promedios con
        minimo y maximo. Los valores de la ventana ya se publicaron uno por uno
        mientras se media, asi que el resumen se puede reconstruir en la base.
        """
