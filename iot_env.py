"""De donde salen el broker y sus credenciales.

Este monitor tiene que funcionar en dos sitios:

1. **Dentro de SIAPPC**, en `iot/monitor/`. Ahi comparte broker, credenciales y
   CA con `iot/src/`, y esos valores viven en `iot/.env`. Se leen a traves de
   `iot/src/config.py`, que es el modulo que ya sabe hacerlo: si manana cambia
   el host del broker o donde vive la CA, se cambia en un solo lugar.

2. **Suelto**, fuera del repo de SIAPPC. Ahi no existe `iot/src/config.py`, asi
   que se leen las mismas variables del entorno o de un `.env` al lado.

Los nombres de las variables son los mismos en los dos casos, asi que un `.env`
sirve tal cual en cualquiera de las dos ubicaciones.

Lo que sale de aca es solo *donde* y *con que credenciales* se publica. Lo que
se publica y cada cuanto vive en `config.py`, en `BackendConfig`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
# Si esto vive en iot/monitor/, el .env compartido esta un nivel mas arriba.
_IOT_DIR = _HERE.parent
_SHARED_CONFIG = _IOT_DIR / "src" / "config.py"


def _load_env_file(path: Path) -> None:
    """Carga un `.env` sencillo. Lo que ya venga del entorno gana."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _load_shared() -> Any | None:
    """El config de `iot/src/`, si estamos dentro de SIAPPC.

    Se carga por ruta y con otro nombre de modulo a proposito: este directorio
    ya tiene su propio `config.py` al frente de `sys.path`, asi que un
    `import config` desde aca resolveria a ese y no al de `iot/src/`.
    """
    if not _SHARED_CONFIG.is_file():
        return None
    spec = importlib.util.spec_from_file_location("siappc_iot_config", _SHARED_CONFIG)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registrarlo antes de ejecutarlo evita que se cargue dos veces (y que se
    # relea el .env) si algo mas lo pide.
    sys.modules["siappc_iot_config"] = module
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()
INSIDE_SIAPPC = _shared is not None

if _shared is not None:
    DEVICE_CODE: str = _shared.DEVICE_CODE
    MQTT_HOST: str = _shared.MQTT_HOST
    MQTT_PORT: int = _shared.MQTT_PORT
    MQTT_USER: str | None = _shared.MQTT_USER
    MQTT_PASSWORD: str | None = _shared.MQTT_PASSWORD
    MQTT_TLS: bool = _shared.MQTT_TLS
    MQTT_CA_FILE: str = _shared.MQTT_CA_FILE
    MQTT_CLIENT_CERT_FILE: str | None = _shared.MQTT_CLIENT_CERT_FILE
    MQTT_CLIENT_KEY_FILE: str | None = _shared.MQTT_CLIENT_KEY_FILE
    PUBLISH_INTERVAL: float = _shared.PUBLISH_INTERVAL
    BUFFER_MAX_ROWS: int = _shared.BUFFER_MAX_ROWS
    _ENV_PATH = _IOT_DIR / ".env"
else:
    # Modo suelto: las mismas variables, leidas del entorno o de un .env local.
    _ENV_PATH = _HERE / ".env"
    _load_env_file(_ENV_PATH)

    # Codigo del dispositivo tal como esta dado de alta en la tabla
    # `dispositivo`. Si el backend no lo encuentra ahi, descarta las lecturas
    # por no saber de quien son (ver `ingestReading` en mqttIngest.ts).
    DEVICE_CODE = os.environ.get("DEVICE_CODE", "RPI-01")
    MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
    # El broker de SIAPPC solo escucha TLS: 8883, no 1883.
    MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
    MQTT_USER = os.environ.get("MQTT_USER") or None
    MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
    MQTT_TLS = os.environ.get("MQTT_TLS", "true").lower() not in ("false", "0", "no")
    MQTT_CA_FILE = os.environ.get("MQTT_CA_FILE") or str(_HERE / "certs" / "ca.crt")
    MQTT_CLIENT_CERT_FILE = os.environ.get("MQTT_CLIENT_CERT_FILE") or None
    MQTT_CLIENT_KEY_FILE = os.environ.get("MQTT_CLIENT_KEY_FILE") or None
    PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "1"))
    BUFFER_MAX_ROWS = int(os.environ.get("BUFFER_MAX_ROWS", "86400"))


def env_file_missing() -> bool:
    """Si no hay `.env`, todo lo de arriba son valores por defecto.

    Se avisa en vez de intentarlo en silencio contra `localhost`: sin ese
    archivo no hay broker real al que llegar, y las lecturas se irian
    acumulando en la cola sin que nadie entienda por que.
    """
    return not _ENV_PATH.is_file()


def env_file_path() -> str:
    return str(_ENV_PATH)


def telemetry_topic(device: str) -> str:
    return f"siappc/{device}/telemetry"


def status_topic(device: str) -> str:
    return f"siappc/{device}/status"


def monitor_buffer_path() -> str:
    """Cola local del monitor, aparte de la de `iot/src/`.

    Dentro de SIAPPC son dos procesos distintos: si compartieran archivo, uno
    borraria del buffer lecturas que el otro todavia no confirmo.
    """
    default = _IOT_DIR if INSIDE_SIAPPC else _HERE
    return os.environ.get("MONITOR_BUFFER_PATH", str(default / "monitor-buffer.db"))
