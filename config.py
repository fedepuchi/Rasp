"""Configuracion central del monitor de signos vitales.

Todo lo que se toca sin abrir el resto del codigo vive aca. Se puede pisar con
un archivo JSON (--config mi_config.json) o con variables de entorno MONITOR_*.

Ejemplo de override por entorno:
    MONITOR_BACKEND_URL=http://192.168.0.50:8000 python main.py
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any


# --------------------------------------------------------------------------
# Hardware
# --------------------------------------------------------------------------


@dataclass
class EcgConfig:
    """AD8232 (salida analogica) leido por un ADS1115."""

    i2c_bus: int = 1
    ads_address: int = 0x48  # ADDR a GND. 0x49=VDD, 0x4A=SDA, 0x4B=SCL
    ads_channel: int = 0  # AIN0 <- OUTPUT del AD8232
    # SPS soportados por el ADS1115: 8,16,32,64,128,250,475,860.
    # 250 alcanza y sobra para ver morfologia; 475 si el Pi da el cuero.
    sample_rate_hz: int = 250
    # Ganancia del PGA en volts full-scale: 6.144, 4.096, 2.048, 1.024, 0.512, 0.256
    # El AD8232 saca ~0..3.3V centrado en VCC/2, asi que 4.096 es lo correcto.
    pga_volts: float = 4.096
    # Ganancia del AD8232 (fija en el modulo tipico ~ x1100 con el filtro de a bordo).
    # Solo se usa para convertir a mV. OJO: es la ganancia NOMINAL del diseno de
    # referencia, no una calibracion. Los mV que salen son aproximados.
    frontend_gain: float = 1100.0
    # Alimentacion del AD8232. Se usa para detectar cuando su salida pega
    # contra un riel (electrodo moviendose, mal contacto).
    supply_volts: float = 3.3
    # Como se rotula el trazo. Con 3 electrodos la derivacion depende de donde
    # los pegues, y el modulo no tiene forma de saberlo: por eso "ECG" a secas.
    # Si los ubicas en RA / pierna izquierda / RL, poné "ECG II".
    lead_label: str = "ECG"
    # Deteccion de electrodo suelto (LO+ / LO-). None = deshabilitado.
    lo_plus_pin: int | None = 17
    lo_minus_pin: int | None = 27
    # Filtrado
    highpass_hz: float = 0.5
    lowpass_hz: float = 40.0
    notch_hz: float | None = 50.0  # 50 en Argentina/Europa, 60 en Norteamerica
    notch_q: float = 30.0


@dataclass
class PpgConfig:
    """MAX30102: pulsioximetro (LED rojo + infrarrojo)."""

    i2c_bus: int = 1
    address: int = 0x57
    # Config del chip. sample_rate/averaging dan la frecuencia efectiva de salida:
    #   fs_efectiva = sample_rate_hz / averaging
    sample_rate_hz: int = 400  # 50,100,200,400,800,1000,1600,3200
    averaging: int = 4  # 1,2,4,8,16,32  -> 400/4 = 100 Hz de salida
    pulse_width_us: int = 411  # 69,118,215,411 (411 = ADC de 18 bits)
    adc_range_na: int = 4096  # 2048,4096,8192,16384
    led_red_current: int = 0x24  # 0x00..0xFF (~0.2 mA por paso)
    led_ir_current: int = 0x24
    # Umbral de "hay dedo": DC del infrarrojo por debajo de esto = sensor al aire
    finger_threshold: int = 50_000
    # Filtrado del pulso
    highpass_hz: float = 0.5
    lowpass_hz: float = 5.0
    # Ventana para calcular SpO2 (segundos)
    spo2_window_s: float = 4.0
    read_interval_s: float = 0.02


@dataclass
class RespConfig:
    """Respiracion derivada de la modulacion del PPG (no es un sensor aparte)."""

    enabled: bool = True
    fs_hz: int = 25  # se decima desde el PPG
    highpass_hz: float = 0.1  # 6 rpm
    lowpass_hz: float = 0.7  # 42 rpm


# --------------------------------------------------------------------------
# Red / backend
# --------------------------------------------------------------------------


@dataclass
class BackendConfig:
    """Cliente HTTP que manda el JSON al backend en la misma WiFi."""

    enabled: bool = True
    # Sin barra final. Puede ser IP directa o hostname .local
    url: str = "http://192.168.0.100:8000"
    ingest_path: str = "/api/v1/ingest"
    api_key: str | None = None  # se manda como header X-API-Key si no es None
    timeout_s: float = 3.0
    # Cada cuanto se manda cada tipo de mensaje
    vitals_interval_s: float = 1.0
    waveform_interval_s: float = 0.25
    # Cuantos mensajes se agrupan por POST
    batch_max_messages: int = 8
    # Cuantos lotes se guardan en RAM si se cae la WiFi (se descartan los mas viejos)
    offline_buffer_batches: int = 600
    # Reintento con backoff exponencial
    retry_base_s: float = 0.5
    retry_max_s: float = 15.0
    # Diezmado de la onda de ECG antes de mandarla (1 = manda todo)
    ecg_send_decimation: int = 1
    verify_tls: bool = True
    # Comprime el cuerpo con gzip si supera estos bytes (0 = nunca).
    # Solo activalo si el backend entiende Content-Encoding: gzip.
    gzip_over_bytes: int = 0


@dataclass
class DeviceConfig:
    device_id: str = field(default_factory=lambda: f"rpi-{socket.gethostname()}")
    patient_id: str = "ANON-001"
    patient_name: str = "PACIENTE DE PRUEBA"
    bed: str = "CAMA 1"


# --------------------------------------------------------------------------
# Alarmas
# --------------------------------------------------------------------------


@dataclass
class AlarmLimits:
    hr_low: float = 50
    hr_high: float = 120
    spo2_low: float = 90
    resp_low: float = 8
    resp_high: float = 30
    # Segundos que una condicion tiene que sostenerse para disparar la alarma
    debounce_s: float = 3.0
    # Cuanto dura el silencio cuando se aprieta M
    mute_duration_s: float = 120.0


# --------------------------------------------------------------------------
# Interfaz
# --------------------------------------------------------------------------


@dataclass
class UiConfig:
    fullscreen: bool = True
    # (0, 0) = usar la resolucion nativa del monitor HDMI
    window_size: tuple[int, int] = (0, 0)
    fps: int = 60
    hide_cursor: bool = True
    sweep_seconds: float = 5.0  # cuanto tiempo entra a lo ancho de la pantalla
    sound_enabled: bool = True
    beat_beep: bool = True  # el "bip" clasico en cada latido
    show_debug: bool = False


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    ecg: EcgConfig = field(default_factory=EcgConfig)
    ppg: PpgConfig = field(default_factory=PpgConfig)
    resp: RespConfig = field(default_factory=RespConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    alarms: AlarmLimits = field(default_factory=AlarmLimits)
    ui: UiConfig = field(default_factory=UiConfig)
    # Modo demo: sin hardware, seniales sinteticas. Sirve para probar en la PC.
    demo: bool = False

    # ---- utilidades -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                _merge(cfg, json.load(fh))
        _apply_env(cfg)
        return cfg

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)


def _merge(target: Any, data: dict[str, Any]) -> None:
    """Pisa recursivamente los campos de un dataclass con un dict."""
    valid = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in valid:
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif isinstance(current, tuple) and isinstance(value, list):
            setattr(target, key, tuple(value))
        else:
            setattr(target, key, value)


# Solo se exponen por entorno las cosas que uno cambia al desplegar.
_ENV_MAP = {
    "MONITOR_BACKEND_URL": ("backend", "url", str),
    "MONITOR_BACKEND_ENABLED": ("backend", "enabled", lambda v: v.lower() in ("1", "true", "si", "yes")),
    "MONITOR_API_KEY": ("backend", "api_key", str),
    "MONITOR_DEVICE_ID": ("device", "device_id", str),
    "MONITOR_PATIENT": ("device", "patient_name", str),
    "MONITOR_FULLSCREEN": ("ui", "fullscreen", lambda v: v.lower() in ("1", "true", "si", "yes")),
    "MONITOR_NOTCH_HZ": ("ecg", "notch_hz", float),
}


def _apply_env(cfg: Config) -> None:
    for env_name, (section, attr, caster) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            setattr(getattr(cfg, section), attr, caster(raw))
        except (ValueError, TypeError):
            print(f"[config] valor invalido en {env_name}={raw!r}, se ignora")
