"""Motor de alarmas: limites, antirrebote, prioridades y silencio temporal."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from config import AlarmLimits

# Prioridades tal como las usa un monitor real
HIGH = "high"      # rojo, requiere accion inmediata
MEDIUM = "medium"  # amarillo
LOW = "low"        # celeste, informativo
TECHNICAL = "technical"  # problema del equipo, no del paciente

_LEVEL_ORDER = {HIGH: 3, MEDIUM: 2, LOW: 1, TECHNICAL: 0}

# El detector de QRS necesita unos segundos para asentar los filtros y aprender
# el umbral. Sin esta espera, el monitor arranca gritando que no hay latidos.
ECG_WARMUP_S = 15.0


@dataclass
class Alarm:
    code: str
    level: str
    message: str
    value: float | None = None
    limit: float | None = None
    since: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "value": self.value,
            "limit": self.limit,
            "since": _iso(self.since),
            "duration_s": round(time.time() - self.since, 1),
        }


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + \
        f".{int((epoch % 1) * 1000):03d}Z"


class AlarmManager:
    def __init__(self, limits: AlarmLimits):
        self.limits = limits
        self.active: dict[str, Alarm] = {}
        self._pending: dict[str, float] = {}  # code -> instante en que empezo
        self.muted_until: float = 0.0

    # -- silencio ----------------------------------------------------------

    @property
    def muted(self) -> bool:
        return time.monotonic() < self.muted_until

    @property
    def mute_remaining_s(self) -> float:
        return max(0.0, self.muted_until - time.monotonic())

    def toggle_mute(self) -> None:
        if self.muted:
            self.muted_until = 0.0
        else:
            self.muted_until = time.monotonic() + self.limits.mute_duration_s

    # -- evaluacion --------------------------------------------------------

    def evaluate(self, snapshot) -> None:
        """Recalcula las alarmas a partir del estado actual."""
        lim = self.limits
        candidates: list[Alarm] = []

        # -- tecnicas (no dependen del paciente) --
        if snapshot.ecg_leads_off:
            which = []
            if snapshot.ecg_lo_plus:
                which.append("LO+")
            if snapshot.ecg_lo_minus:
                which.append("LO-")
            detail = "/".join(which) if which else "ECG"
            candidates.append(Alarm("LEADS_OFF", TECHNICAL,
                                    f"ELECTRODO SUELTO ({detail})"))
        elif snapshot.ecg_saturated:
            # Sin esto, un AD8232 pegado al riel se ve como una linea rara y
            # uno se pasa media hora buscando el problema en el software.
            candidates.append(Alarm("ECG_SATURADO", TECHNICAL,
                                    "SENIAL DE ECG SATURADA"))
        if not snapshot.finger_detected:
            candidates.append(Alarm("NO_PROBE", TECHNICAL, "SIN DEDO EN EL SENSOR"))
        elif snapshot.spo2_out_of_range >= 3:
            # Sin este aviso, el SpO2 queda en guiones y no hay forma de saber
            # por que. La causa mas comun son los canales rojo/IR invertidos.
            candidates.append(Alarm("SPO2_INVALIDO", TECHNICAL,
                                    "SpO2 FUERA DE RANGO - VER CANALES LED"))
        if not snapshot.backend_ok and snapshot.backend_enabled:
            candidates.append(Alarm("NET_DOWN", TECHNICAL, "SIN CONEXION AL SERVIDOR"))

        # -- fisiologicas --
        hr = snapshot.hr_bpm
        if snapshot.ecg_leads_off or snapshot.ecg_saturated:
            # Con la senial rota no se opina sobre el corazon: lo que hay que
            # arreglar son los electrodos, y esa alarma ya se emitio arriba.
            pass
        elif hr is not None:
            if hr < lim.hr_low:
                candidates.append(Alarm("HR_LOW", HIGH, "BRADICARDIA", hr, lim.hr_low))
            elif hr > lim.hr_high:
                candidates.append(Alarm("HR_HIGH", HIGH, "TAQUICARDIA", hr, lim.hr_high))
        elif (snapshot.ecg_active
              and snapshot.uptime_s > ECG_WARMUP_S
              and snapshot.seconds_since_beat > 10.0):
            # A proposito NO dice "asistolia". Un solo canal de ECG con
            # electrodos de superficie no puede distinguir un paro cardiaco de
            # un electrodo flojo, y lo segundo es muchisimo mas frecuente.
            candidates.append(Alarm("NO_BEATS", MEDIUM,
                                    "SIN LATIDOS - REVISAR ELECTRODOS"))

        spo2 = snapshot.spo2_pct
        if spo2 is not None and snapshot.finger_detected:
            if spo2 < lim.spo2_low:
                level = HIGH if spo2 < lim.spo2_low - 5 else MEDIUM
                candidates.append(Alarm("SPO2_LOW", level, "SATURACION BAJA",
                                        spo2, lim.spo2_low))

        resp = snapshot.resp_rpm
        if resp is not None:
            if resp < lim.resp_low:
                candidates.append(Alarm("RESP_LOW", MEDIUM, "BRADIPNEA", resp, lim.resp_low))
            elif resp > lim.resp_high:
                candidates.append(Alarm("RESP_HIGH", MEDIUM, "TAQUIPNEA", resp, lim.resp_high))

        self._apply(candidates)

    def _apply(self, candidates: list[Alarm]) -> None:
        """Antirrebote: una condicion tiene que sostenerse antes de sonar."""
        now = time.monotonic()
        codes = {alarm.code for alarm in candidates}

        for alarm in candidates:
            if alarm.code in self.active:
                # Ya activa: actualizamos el valor pero conservamos el 'since'
                existing = self.active[alarm.code]
                existing.value = alarm.value
                existing.level = alarm.level
                existing.message = alarm.message
                continue
            started = self._pending.setdefault(alarm.code, now)
            # Las tecnicas entran enseguida; las fisiologicas esperan el debounce
            delay = 0.5 if alarm.level == TECHNICAL else self.limits.debounce_s
            if now - started >= delay:
                self.active[alarm.code] = alarm
                self._pending.pop(alarm.code, None)

        for code in list(self._pending):
            if code not in codes:
                self._pending.pop(code, None)
        for code in list(self.active):
            if code not in codes:
                self.active.pop(code, None)

    def clear(self) -> None:
        """Apaga todo. Se usa entre mediciones: sin sensores leyendo no hay
        nada que alarmar, y dejar la ultima alarma prendida seria enganioso."""
        self.active.clear()
        self._pending.clear()

    # -- consulta ----------------------------------------------------------

    def sorted_alarms(self) -> list[Alarm]:
        return sorted(self.active.values(),
                      key=lambda a: (-_LEVEL_ORDER[a.level], a.since))

    @property
    def highest_level(self) -> str | None:
        if not self.active:
            return None
        return max(self.active.values(), key=lambda a: _LEVEL_ORDER[a.level]).level

    def should_sound(self) -> bool:
        return bool(self.active) and not self.muted and \
            self.highest_level in (HIGH, MEDIUM)

    def to_json(self) -> list[dict]:
        return [alarm.to_json() for alarm in self.sorted_alarms()]
