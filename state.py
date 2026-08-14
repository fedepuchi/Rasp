"""Foto del estado del monitor en un instante.

Es la unica estructura que comparten la UI, las alarmas y el publicador, asi
que si se agrega un signo vital nuevo se toca aca y ya lo ven los tres.

Regla de oro de este archivo: en `vitals` solo van magnitudes que **estos tres
modulos pueden medir de verdad**. Lo que es del equipo y no del paciente (el
offset de continua del AD8232, el nivel de continua de los LED) va en
`diagnostics`, que es otra cosa y se muestra aparte.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class VitalsSnapshot:
    # -- signos vitales, todos medidos --
    hr_bpm: float | None = None          # AD8232 -> ADS1115: intervalos R-R
    pr_bpm: float | None = None          # MAX30102: picos del pletismograma
    spo2_pct: float | None = None        # MAX30102: relacion rojo/infrarrojo
    perfusion_index: float | None = None  # MAX30102: AC pico a pico sobre DC
    rr_last_ms: float | None = None      # AD8232: ultimo intervalo R-R
    hrv_rmssd_ms: float | None = None    # AD8232: RMSSD sobre los ultimos ~30 R-R

    # -- estimado, no medido: sale de como la respiracion mueve la linea de
    #    base del pletismograma. Se muestra siempre marcado como estimacion.
    resp_rpm: float | None = None

    # -- calidad de senial --
    ecg_leads_off: bool = False
    ecg_lo_plus: bool = False
    ecg_lo_minus: bool = False
    ecg_noise: float = 0.0
    ecg_saturated: bool = False          # el AD8232 pega contra el riel del ADS1115
    finger_detected: bool = False
    spo2_out_of_range: int = 0           # veces seguidas que la curva dio fuera de 70-100%
    ecg_active: bool = False             # el hardware de ECG arranco bien
    ppg_active: bool = False
    seconds_since_beat: float = 999.0

    # -- diagnostico del equipo, NO del paciente --
    ecg_baseline_v: float | None = None     # continua del AD8232, deberia dar ~VCC/2
    ir_dc: float | None = None              # nivel de continua del LED infrarrojo
    red_dc: float | None = None             # idem del LED rojo

    # -- estado del equipo --
    backend_enabled: bool = False
    backend_ok: bool = False
    backend_pending: int = 0
    uptime_s: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def pulse_deficit(self) -> float | None:
        """FC (electrica) menos frecuencia de pulso (mecanica).

        Si el corazon se contrae pero no expulsa sangre suficiente, aparecen
        latidos en el ECG que no llegan al dedo y los dos numeros se separan.
        """
        if self.hr_bpm is None or self.pr_bpm is None:
            return None
        return round(self.hr_bpm - self.pr_bpm)

    def vitals_json(self) -> dict:
        """Solo lo que se mide, redondeado como lo mostraria un monitor."""
        return {
            "hr_bpm": _round(self.hr_bpm, 0),
            "hr_source": "ecg" if self.hr_bpm is not None else None,
            "pr_bpm": _round(self.pr_bpm, 0),
            "pulse_deficit_bpm": self.pulse_deficit,
            "spo2_pct": _round(self.spo2_pct, 1),
            "perfusion_index": _round(self.perfusion_index, 2),
            "rr_last_ms": _round(self.rr_last_ms, 1),
            "hrv_rmssd_ms": _round(self.hrv_rmssd_ms, 1),
            # Estimado a partir del pleth, no medido con un sensor de flujo ni
            # de impedancia toracica. El backend deberia tratarlo distinto.
            "resp_rpm_estimated": _round(self.resp_rpm, 0),
        }

    def quality_json(self) -> dict:
        return {
            "ecg_leads_off": self.ecg_leads_off,
            "ecg_lo_plus": self.ecg_lo_plus,
            "ecg_lo_minus": self.ecg_lo_minus,
            "ecg_noise": round(self.ecg_noise, 3),
            "ecg_saturated": self.ecg_saturated,
            "finger_detected": self.finger_detected,
            "spo2_out_of_range": self.spo2_out_of_range,
            "ecg_active": self.ecg_active,
            "ppg_active": self.ppg_active,
        }

    def diagnostics_json(self) -> dict:
        """Salud del equipo. Nada de esto es un signo vital."""
        return {
            "ecg_baseline_v": _round(self.ecg_baseline_v, 3),
            "ir_dc": _round(self.ir_dc, 0),
            "red_dc": _round(self.red_dc, 0),
        }


def _round(value: float | None, digits: int) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(value, digits)
