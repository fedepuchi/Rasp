"""Envio de los datos al backend en formato JSON.

Diseno:

  * El bucle principal solo encola. Nunca hace red, asi que una WiFi lenta no
    baja los FPS del monitor.
  * Un hilo aparte arma lotes y hace POST. Si el backend no contesta, los lotes
    quedan en un buffer acotado (se descartan los mas viejos) y se reintenta
    con backoff exponencial.
  * Todo mensaje lleva device_id, session_id, seq y timestamp, asi el backend
    puede detectar huecos y reordenar.

El contrato completo esta documentado en JSON.md.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from config import BackendConfig, Config

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

import urllib.error
import urllib.request


SCHEMA = "monitor.v1"


def iso_utc(epoch: float) -> str:
    """Timestamp ISO-8601 en UTC con milisegundos, que es lo que espera casi
    cualquier backend (JS lo parsea directo con new Date(...))."""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))
    return f"{base}.{int((epoch % 1) * 1000):03d}Z"


@dataclass
class PublisherStatus:
    enabled: bool = False
    connected: bool = False
    pending_batches: int = 0
    sent_ok: int = 0
    failed: int = 0
    dropped: int = 0
    last_error: str | None = None
    last_success_t: float = 0.0
    last_latency_ms: float | None = None


@dataclass
class _Channel:
    """Acumulador de una onda hasta que toca mandarla."""

    name: str
    unit: str
    fs_hz: float
    scale: float
    decimation: int = 1
    note: str = ""
    samples: list[int] = field(default_factory=list)
    t0: float | None = None
    _phase: int = 0

    def add(self, values: list[float], t0: float) -> None:
        if not values:
            return
        if self.t0 is None:
            self.t0 = t0
        inv = 1.0 / self.scale
        if self.decimation <= 1:
            self.samples.extend(int(round(v * inv)) for v in values)
            return
        for value in values:
            if self._phase == 0:
                self.samples.append(int(round(value * inv)))
            self._phase = (self._phase + 1) % self.decimation

    def flush(self) -> dict[str, Any] | None:
        if not self.samples or self.t0 is None:
            return None
        message = {
            "name": self.name,
            "unit": self.unit,
            "fs_hz": round(self.fs_hz / self.decimation, 3),
            "scale": self.scale,
            "t0": iso_utc(self.t0),
            "t0_ms": int(self.t0 * 1000),
            "n": len(self.samples),
            "samples": self.samples,
        }
        self.samples = []
        self.t0 = None
        return message


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg: BackendConfig = cfg.backend
        self.device = cfg.device
        self.full_cfg = cfg
        self.session_id = uuid.uuid4().hex
        self.status = PublisherStatus(enabled=self.cfg.enabled)

        self._seq = 0
        self._seq_lock = threading.Lock()
        self._outbox: deque[list[dict]] = deque(maxlen=self.cfg.offline_buffer_batches)
        self._outbox_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.time()

        self._pending_messages: list[dict] = []
        self._channels: dict[str, _Channel] = {}
        self._last_vitals_at = 0.0
        self._last_wave_at = 0.0

        self._session = None
        if requests is not None:
            self._session = requests.Session()
            self._session.headers.update(self._headers())

    # -- configuracion -----------------------------------------------------

    @property
    def endpoint(self) -> str:
        return self.cfg.url.rstrip("/") + self.cfg.ingest_path

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"monitor-vital/1.0 ({self.device.device_id})",
        }
        if self.cfg.api_key:
            headers["X-API-Key"] = self.cfg.api_key
        return headers

    def register_channel(self, name: str, unit: str, fs_hz: float, scale: float,
                         decimation: int = 1, note: str = "") -> None:
        self._channels[name] = _Channel(name, unit, fs_hz, scale,
                                        max(1, decimation), note)

    # -- entrada de datos --------------------------------------------------

    def add_samples(self, channel: str, values: list[float], t0: float) -> None:
        ch = self._channels.get(channel)
        if ch is not None and self.cfg.enabled:
            ch.add(values, t0)

    def tick(self, snapshot, alarm_manager) -> None:
        """Llamar una vez por frame: decide que mensajes toca emitir."""
        if not self.cfg.enabled:
            return
        now = time.monotonic()

        if now - self._last_vitals_at >= self.cfg.vitals_interval_s:
            self._last_vitals_at = now
            self._emit(self._vitals_message(snapshot, alarm_manager))

        if now - self._last_wave_at >= self.cfg.waveform_interval_s:
            self._last_wave_at = now
            waveforms = [w for w in (ch.flush() for ch in self._channels.values()) if w]
            if waveforms:
                self._emit(self._envelope("waveform", {"waveforms": waveforms}))

    # -- construccion de mensajes -----------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _envelope(self, msg_type: str, payload: dict) -> dict:
        now = time.time()
        return {
            "schema": SCHEMA,
            "type": msg_type,
            "device_id": self.device.device_id,
            "session_id": self.session_id,
            "seq": self._next_seq(),
            "ts": iso_utc(now),
            "ts_ms": int(now * 1000),
            "uptime_s": round(now - self._started_at, 3),
            **payload,
        }

    def _vitals_message(self, snapshot, alarm_manager) -> dict:
        limits = self.full_cfg.alarms
        return self._envelope("vitals", {
            "patient": {
                "id": self.device.patient_id,
                "name": self.device.patient_name,
                "bed": self.device.bed,
            },
            "vitals": snapshot.vitals_json(),
            "quality": snapshot.quality_json(),
            # Salud del equipo, aparte de los signos vitales a proposito
            "diagnostics": snapshot.diagnostics_json(),
            "alarms": alarm_manager.to_json(),
            "alarms_muted": alarm_manager.muted,
            "limits": {
                "hr_bpm": [limits.hr_low, limits.hr_high],
                "spo2_pct": [limits.spo2_low, 100],
                "resp_rpm": [limits.resp_low, limits.resp_high],
            },
        })

    def session_start(self, extra: dict | None = None) -> None:
        if not self.cfg.enabled:
            return
        channels = [
            {"name": ch.name, "unit": ch.unit,
             "fs_hz": round(ch.fs_hz / ch.decimation, 3), "scale": ch.scale,
             "note": ch.note}
            for ch in self._channels.values()
        ]
        self._emit(self._envelope("session_start", {
            "patient": {
                "id": self.device.patient_id,
                "name": self.device.patient_name,
                "bed": self.device.bed,
            },
            "sensors": {
                "ecg": {"frontend": "AD8232", "adc": "ADS1115",
                        "lead": self.full_cfg.ecg.lead_label,
                        "fs_hz": self.full_cfg.ecg.sample_rate_hz,
                        "gain_nominal": self.full_cfg.ecg.frontend_gain,
                        "calibrated": False},
                "spo2": {"sensor": "MAX30102",
                         "fs_hz": self.full_cfg.ppg.sample_rate_hz / self.full_cfg.ppg.averaging,
                         "calibrated": False},
            },
            "channels": channels,
            # De donde sale cada numero, declarado por el propio equipo. Sirve
            # para que nadie le atribuya al monitor algo que no puede medir.
            "measures": {
                "hr_bpm": "AD8232 -> ADS1115 (intervalos R-R)",
                "rr_last_ms": "AD8232 -> ADS1115",
                "hrv_rmssd_ms": "AD8232 -> ADS1115 (corto plazo, ~30 latidos)",
                "spo2_pct": "MAX30102 (curva empirica generica, sin calibrar)",
                "pr_bpm": "MAX30102",
                "perfusion_index": "MAX30102",
                "resp_rpm_estimated": "estimado del pleth del MAX30102, NO medido",
            },
            "not_measured": [
                "temperatura corporal",
                "presion arterial",
                "capnografia",
                "respiracion por impedancia o flujo",
            ],
            "demo": self.full_cfg.demo,
            **(extra or {}),
        }), urgent=True)

    def session_end(self, reason: str = "shutdown") -> None:
        if not self.cfg.enabled:
            return
        self._emit(self._envelope("session_end", {"reason": reason}), urgent=True)
        self._flush_pending()

    def measurement(self, summary, index: int) -> None:
        """Resumen de una medicion a demanda. Se manda apenas termina.

        Es el mensaje mas util del contrato para guardar historial: una fila
        por medicion, en vez de un caudal continuo de vitals.
        """
        if not self.cfg.enabled:
            return
        self._emit(self._envelope("measurement", {
            "patient": {
                "id": self.device.patient_id,
                "name": self.device.patient_name,
                "bed": self.device.bed,
            },
            "index": index,
            "summary": summary.to_json(),
        }), urgent=True)

    def event(self, name: str, detail: dict | None = None) -> None:
        """Evento suelto (por ejemplo: se cambiaron los limites de alarma)."""
        if not self.cfg.enabled:
            return
        self._emit(self._envelope("event", {"event": name, "detail": detail or {}}))

    # -- cola y envio ------------------------------------------------------

    def _emit(self, message: dict, urgent: bool = False) -> None:
        self._pending_messages.append(message)
        if urgent or len(self._pending_messages) >= self.cfg.batch_max_messages:
            self._flush_pending()

    def _flush_pending(self) -> None:
        if not self._pending_messages:
            return
        batch = self._pending_messages
        self._pending_messages = []
        with self._outbox_lock:
            if len(self._outbox) == self._outbox.maxlen:
                self.status.dropped += 1
            self._outbox.append(batch)
            self.status.pending_batches = len(self._outbox)
        self._wake.set()

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        self._thread = threading.Thread(target=self._run, name="publisher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._flush_pending()
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = self.cfg.retry_base_s
        while not self._stop.is_set():
            with self._outbox_lock:
                batch = self._outbox[0] if self._outbox else None
            if batch is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue

            started = time.monotonic()
            ok, error = self._post(batch)
            latency_ms = (time.monotonic() - started) * 1000.0

            if ok:
                with self._outbox_lock:
                    if self._outbox and self._outbox[0] is batch:
                        self._outbox.popleft()
                    self.status.pending_batches = len(self._outbox)
                self.status.sent_ok += 1
                self.status.connected = True
                self.status.last_error = None
                self.status.last_success_t = time.time()
                self.status.last_latency_ms = round(latency_ms, 1)
                backoff = self.cfg.retry_base_s
            else:
                self.status.failed += 1
                self.status.connected = False
                self.status.last_error = error
                # No se descarta el lote: se reintenta hasta que el buffer se llene
                self._stop.wait(timeout=backoff)
                backoff = min(self.cfg.retry_max_s, backoff * 2)

    def _post(self, batch: list[dict]) -> tuple[bool, str | None]:
        envelope = {
            "schema": SCHEMA,
            "device_id": self.device.device_id,
            "session_id": self.session_id,
            "sent_at": iso_utc(time.time()),
            "messages": batch,
        }
        try:
            body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return False, f"JSON invalido: {exc}"

        headers = self._headers()
        if 0 < self.cfg.gzip_over_bytes <= len(body):
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        if self._session is not None:
            return self._post_requests(body, headers)
        return self._post_urllib(body, headers)

    def _post_requests(self, body: bytes, headers: dict) -> tuple[bool, str | None]:
        try:
            response = self._session.post(
                self.endpoint, data=body, headers=headers,
                timeout=self.cfg.timeout_s, verify=self.cfg.verify_tls,
            )
        except requests.RequestException as exc:
            return False, _short(exc)
        if 200 <= response.status_code < 300:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:120]}"

    def _post_urllib(self, body: bytes, headers: dict) -> tuple[bool, str | None]:
        request = urllib.request.Request(
            self.endpoint, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout_s) as resp:
                if 200 <= resp.status < 300:
                    return True, None
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, _short(exc)


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:110]
