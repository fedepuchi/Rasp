"""Seniales sinteticas para probar sin hardware (--demo).

Genera ECG y PPG en las MISMAS unidades que devuelven los sensores reales
(cuentas del ADS1115 y cuentas de 18 bits del MAX30102), asi el resto del
pipeline no se entera de la diferencia.
"""

from __future__ import annotations

import math
import random


def _gauss(t: float, center: float, width: float, amplitude: float) -> float:
    x = (t - center) / width
    return amplitude * math.exp(-0.5 * x * x)


class EcgSimulator:
    """Latido PQRST armado con gaussianas, mas deriva de linea de base y ruido."""

    def __init__(self, fs: float, hr_bpm: float = 72.0, amplitude_counts: float = 3000.0):
        self.fs = fs
        self.hr = hr_bpm
        self.amplitude = amplitude_counts
        self.phase = 0.0  # posicion dentro del latido, en segundos
        self.rr = 60.0 / hr_bpm
        self.t = 0.0
        self._noise = 0.0

    def _new_rr(self) -> float:
        # Un poco de variabilidad: arritmia sinusal respiratoria + jitter
        resp = 0.04 * math.sin(2 * math.pi * 0.25 * self.t)
        return max(0.3, 60.0 / self.hr * (1.0 + resp + random.gauss(0, 0.012)))

    def step(self) -> float:
        dt = 1.0 / self.fs
        self.t += dt
        self.phase += dt
        if self.phase >= self.rr:
            self.phase -= self.rr
            self.rr = self._new_rr()

        p = self.phase
        # Tiempos relativos al inicio del latido (segundos)
        value = 0.0
        value += _gauss(p, 0.14, 0.025, 0.12)   # P
        value += _gauss(p, 0.23, 0.008, -0.07)  # Q
        value += _gauss(p, 0.25, 0.008, 1.00)   # R
        value += _gauss(p, 0.27, 0.010, -0.22)  # S
        value += _gauss(p, 0.42, 0.045, 0.28)   # T

        # Deriva de linea de base (movimiento del paciente / respiracion)
        drift = 0.06 * math.sin(2 * math.pi * 0.22 * self.t)
        # Ruido de red + ruido blanco
        mains = 0.012 * math.sin(2 * math.pi * 50.0 * self.t)
        self._noise = 0.85 * self._noise + 0.15 * random.gauss(0, 0.012)

        signal = (value + drift + mains + self._noise) * self.amplitude
        # El AD8232 saca centrado en VCC/2; en cuentas del ADS eso es ~13100
        return signal + 13100.0

    def block(self, n: int) -> list[float]:
        return [self.step() for _ in range(n)]


class PpgSimulator:
    """Pulso con onda sistolica + muesca dicrota, modulado por la respiracion."""

    def __init__(self, fs: float, hr_bpm: float = 72.0, spo2: float = 98.0,
                 resp_rpm: float = 16.0):
        self.fs = fs
        self.hr = hr_bpm
        self.spo2 = spo2
        self.resp_rpm = resp_rpm
        self.phase = 0.0
        self.rr = 60.0 / hr_bpm
        self.t = 0.0
        self.finger = True

    def _pulse(self, p: float) -> float:
        frac = p / self.rr
        v = _gauss(frac, 0.18, 0.075, 1.00)   # pico sistolico
        v += _gauss(frac, 0.42, 0.090, 0.32)  # rebote dicroto
        v += _gauss(frac, 0.70, 0.180, 0.08)
        return v

    def step(self) -> tuple[int, int]:
        dt = 1.0 / self.fs
        self.t += dt
        self.phase += dt
        if self.phase >= self.rr:
            self.phase -= self.rr
            self.rr = max(0.3, 60.0 / self.hr * (1.0 + random.gauss(0, 0.015)))

        if not self.finger:
            base = 1500
            return (int(base + random.gauss(0, 60)), int(base + random.gauss(0, 60)))

        pulse = self._pulse(self.phase)
        # Modulacion respiratoria de la amplitud y de la linea de base
        resp = math.sin(2 * math.pi * (self.resp_rpm / 60.0) * self.t)
        amp_mod = 1.0 + 0.18 * resp

        # La linea de base se mueve ~1% con la respiracion (de ahi sale la
        # curva de RESP), bastante menos que el 2% que aporta el pulso.
        dc_ir = 95_000.0 + 950.0 * resp
        dc_red = 78_000.0 + 780.0 * resp

        # Indice de perfusion ~2% en el IR
        ac_ir = dc_ir * 0.020 * amp_mod
        # Despejamos la relacion R que hace falta para el SpO2 pedido:
        #   SpO2 = -45.06*R^2 + 30.354*R + 94.845
        ratio = _ratio_for_spo2(self.spo2)
        ac_red = ratio * (ac_ir / dc_ir) * dc_red

        ir = dc_ir - ac_ir * pulse + random.gauss(0, 45)
        red = dc_red - ac_red * pulse + random.gauss(0, 45)
        return int(max(0, red)), int(max(0, ir))

    def block(self, n: int) -> tuple[list[int], list[int]]:
        reds: list[int] = []
        irs: list[int] = []
        for _ in range(n):
            r, i = self.step()
            reds.append(r)
            irs.append(i)
        return reds, irs


def _ratio_for_spo2(spo2: float) -> float:
    """Invierte la curva empirica de Maxim para generar un SpO2 objetivo."""
    a, b, c = -45.06, 30.354, 94.845 - spo2
    disc = b * b - 4 * a * c
    if disc < 0:
        return 0.5
    r1 = (-b + math.sqrt(disc)) / (2 * a)
    r2 = (-b - math.sqrt(disc)) / (2 * a)
    candidates = [r for r in (r1, r2) if 0.2 <= r <= 3.0]
    return min(candidates) if candidates else 0.5
