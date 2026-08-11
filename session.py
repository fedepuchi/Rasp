"""Medicion a demanda: los sensores arrancan apagados y miden N segundos.

Ciclo:

    ESPERA  --tecla-->  ESTABILIZANDO  -->  MIDIENDO  -->  RESULTADO
       ^                                                       |
       +------------------------ tecla ------------------------+

Por que hay una fase de estabilizacion antes de contar los segundos: los
pasa-altos de 0.5 Hz del ECG y del pletismografo arrancan con un transitorio
que tapa la senial. Si la ventana de medicion empezara en el instante cero,
los primeros segundos serian basura y la cuenta de "10 segundos de lectura"
seria mentira. Con la espera previa, los 10 segundos son 10 segundos utiles.

Que NO entra en una ventana corta: la frecuencia respiratoria. El filtro de
0.1 Hz necesita unos 12 s solo para asentarse, y despues hacen falta 3 ciclos
respiratorios. Con la ventana en 10 s el numero de RESP no aparece, y eso esta
bien: es preferible a inventarlo. Subiendo `session.duration_s` a 35-40 s si
aparece.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from config import Config

IDLE = "espera"
WARMUP = "estabilizando"
MEASURING = "midiendo"
RESULT = "resultado"

# Cada cuanto se toma una muestra de los numeros para el resumen
_SAMPLE_INTERVAL_S = 0.25


@dataclass
class Stat:
    """Resumen de una magnitud a lo largo de la medicion."""

    name: str
    unit: str
    values: list[float] = field(default_factory=list)

    def add(self, value: float | None) -> None:
        if value is not None:
            self.values.append(value)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float | None:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def minimum(self) -> float | None:
        return min(self.values) if self.values else None

    @property
    def maximum(self) -> float | None:
        return max(self.values) if self.values else None

    def to_json(self, decimals: int = 1) -> dict:
        def r(value):
            if value is None:
                return None
            return int(round(value)) if decimals == 0 else round(value, decimals)

        return {
            "unit": self.unit,
            "n": self.n,
            "mean": r(self.mean),
            "min": r(self.minimum),
            "max": r(self.maximum),
        }


@dataclass
class Summary:
    """Lo que quedo de una medicion terminada."""

    started_at: float
    duration_s: float
    hr: Stat
    spo2: Stat
    pr: Stat
    perfusion: Stat
    resp: Stat
    beats: int = 0
    samples: int = 0
    leads_off_samples: int = 0
    no_finger_samples: int = 0
    saturated_samples: int = 0
    aborted: bool = False

    # -- calidad -----------------------------------------------------------

    def _fraction(self, count: int) -> float:
        return count / self.samples if self.samples else 0.0

    @property
    def ecg_ok_fraction(self) -> float:
        return 1.0 - self._fraction(self.leads_off_samples + self.saturated_samples)

    @property
    def ppg_ok_fraction(self) -> float:
        return 1.0 - self._fraction(self.no_finger_samples)

    @property
    def usable(self) -> bool:
        """Si al menos una de las dos vias dio algo confiable."""
        return (self.hr.n > 0 and self.ecg_ok_fraction > 0.7) or \
               (self.spo2.n > 0 and self.ppg_ok_fraction > 0.7)

    @property
    def problems(self) -> list[str]:
        issues = []
        if self.aborted:
            issues.append("medicion cancelada")
        if self._fraction(self.leads_off_samples) > 0.1:
            issues.append("electrodo suelto durante la medicion")
        if self._fraction(self.saturated_samples) > 0.1:
            issues.append("senial de ECG saturada")
        if self._fraction(self.no_finger_samples) > 0.1:
            issues.append("dedo fuera del sensor")
        if self.hr.n == 0:
            issues.append("no se detectaron latidos en el ECG")
        if self.spo2.n == 0 and self._fraction(self.no_finger_samples) <= 0.1:
            issues.append("no se pudo calcular SpO2")
        return issues

    def to_json(self) -> dict:
        return {
            "started_at_ms": int(self.started_at * 1000),
            "duration_s": round(self.duration_s, 2),
            "aborted": self.aborted,
            "usable": self.usable,
            "beats_detected": self.beats,
            "hr_bpm": self.hr.to_json(0),
            "pr_bpm": self.pr.to_json(0),
            "spo2_pct": self.spo2.to_json(1),
            "perfusion_index": self.perfusion.to_json(2),
            "resp_rpm_estimated": self.resp.to_json(0),
            "quality": {
                "ecg_ok_fraction": round(self.ecg_ok_fraction, 3),
                "ppg_ok_fraction": round(self.ppg_ok_fraction, 3),
                "problems": self.problems,
            },
        }


def _new_summary(duration_s: float) -> Summary:
    return Summary(
        started_at=time.time(),
        duration_s=duration_s,
        hr=Stat("FC", "lpm"),
        spo2=Stat("SpO2", "%"),
        pr=Stat("PR", "lpm"),
        perfusion=Stat("PI", "%"),
        resp=Stat("RESP", "rpm"),
    )


class MeasurementSession:
    """Coordina el encendido de los modulos con la ventana de medicion."""

    def __init__(self, cfg: Config, acquisition):
        self.cfg = cfg.session
        self.acquisition = acquisition
        self.manual = cfg.session.manual

        # Callbacks que completa main.py
        self.on_start: Callable[[], None] | None = None
        self.on_window_start: Callable[[], None] | None = None
        self.on_finish: Callable[[Summary], None] | None = None

        self.state = IDLE if self.manual else MEASURING
        self.summary: Summary | None = None
        self.last_summary: Summary | None = None
        self.measurements = 0

        self._phase_started = time.monotonic()
        self._last_sample = 0.0

        if not self.manual:
            # Modo continuo: nunca se sale de MIDIENDO y no hay resumen
            self.summary = _new_summary(0.0)

    # -- consulta ----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._phase_started

    @property
    def remaining_s(self) -> float:
        if self.state == MEASURING:
            return max(0.0, self.cfg.duration_s - self.elapsed)
        if self.state == WARMUP:
            return max(0.0, self.cfg.warmup_s - self.elapsed)
        return 0.0

    @property
    def progress(self) -> float:
        """0..1 dentro de la fase actual."""
        total = {WARMUP: self.cfg.warmup_s, MEASURING: self.cfg.duration_s}.get(self.state)
        if not total:
            return 0.0
        return min(1.0, self.elapsed / total)

    @property
    def acquiring(self) -> bool:
        """True si los modulos estan encendidos y llegando muestras."""
        return self.state in (WARMUP, MEASURING)

    @property
    def recording(self) -> bool:
        """True solo dentro de la ventana que cuenta.

        Durante la estabilizacion las muestras alimentan los filtros pero no
        se dibujan ni se mandan: son el transitorio, no la medicion.
        """
        return self.state == MEASURING

    # -- transiciones ------------------------------------------------------

    def _enter(self, state: str) -> None:
        self.state = state
        self._phase_started = time.monotonic()

    def trigger(self) -> None:
        """La tecla de medicion. Arranca, o cancela si ya esta midiendo."""
        if not self.manual:
            return
        if self.state in (WARMUP, MEASURING):
            self.abort()
        else:
            self.start()

    def start(self) -> None:
        if not self.manual:
            return
        self.summary = _new_summary(self.cfg.duration_s)
        self._last_sample = 0.0
        if self.on_start is not None:
            self.on_start()
        self.acquisition.start_reading()
        self._enter(WARMUP)

    def abort(self) -> None:
        if self.summary is not None:
            self.summary.aborted = True
            self.summary.duration_s = self.elapsed
        self._finish()

    def _finish(self) -> None:
        self.acquisition.stop_reading()
        self.last_summary = self.summary
        self.measurements += 1
        if self.summary is not None and self.on_finish is not None:
            self.on_finish(self.summary)
        self.summary = None
        self._enter(RESULT)

    # -- avance ------------------------------------------------------------

    def update(self, snapshot) -> None:
        """Llamar una vez por frame, despues de actualizar el snapshot."""
        if not self.manual:
            return

        if self.state == WARMUP:
            if self.elapsed >= self.cfg.warmup_s:
                # El resumen recien empieza a acumular ahora
                self.summary = _new_summary(self.cfg.duration_s)
                self._enter(MEASURING)
                if self.on_window_start is not None:
                    self.on_window_start()
            return

        if self.state == MEASURING:
            self._accumulate(snapshot)
            if self.elapsed >= self.cfg.duration_s:
                if self.summary is not None:
                    self.summary.duration_s = self.elapsed
                self._finish()
            return

        if self.state == RESULT and self.cfg.result_hold_s > 0:
            if self.elapsed >= self.cfg.result_hold_s:
                self._enter(IDLE)

    def note_beat(self) -> None:
        if self.state == MEASURING and self.summary is not None:
            self.summary.beats += 1

    def _accumulate(self, snapshot) -> None:
        now = time.monotonic()
        if now - self._last_sample < _SAMPLE_INTERVAL_S:
            return
        self._last_sample = now

        s = self.summary
        if s is None:
            return
        s.samples += 1
        s.hr.add(snapshot.hr_bpm)
        s.spo2.add(snapshot.spo2_pct)
        s.pr.add(snapshot.pr_bpm)
        s.perfusion.add(snapshot.perfusion_index)
        s.resp.add(snapshot.resp_rpm)
        if snapshot.ecg_leads_off:
            s.leads_off_samples += 1
        if snapshot.ecg_saturated:
            s.saturated_samples += 1
        if not snapshot.finger_detected:
            s.no_finger_samples += 1
