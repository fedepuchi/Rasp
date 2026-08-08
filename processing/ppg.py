"""Procesamiento del PPG del MAX30102: SpO2, pulso, perfusion y respiracion.

SpO2 sale de la "relacion de relaciones" clasica:

    R = (AC_rojo / DC_rojo) / (AC_ir / DC_ir)
    SpO2 = -45.06*R^2 + 30.354*R + 94.845     (curva empirica de Maxim)

La componente AC se mide latido por latido como pico a pico, NO como RMS sobre
una ventana larga. La diferencia importa: la linea de base se mueve con la
respiracion, y ese movimiento se cuela en cualquier ventana de varios segundos
y ensucia la relacion R. Midiendo dentro de un solo ciclo cardiaco el efecto es
chico, y la mediana de varios latidos termina de estabilizarlo. Es tambien la
definicion correcta del indice de perfusion (AC pico a pico sobre DC).

AVISO: la curva de arriba es una aproximacion generica. Un oximetro comercial
se calibra contra co-oximetria en personas reales. Esto sirve para ver
tendencias, no para tomar decisiones clinicas.
"""

from __future__ import annotations

import time
from collections import deque

from .filters import (FilterChain, MovingAverage, band_pass, high_pass,
                      low_pass, median)


class PpgProcessor:
    def __init__(
        self,
        fs: float,
        highpass_hz: float = 0.5,
        lowpass_hz: float = 5.0,
        spo2_window_s: float = 4.0,
        finger_threshold: int = 50_000,
        resp_fs: int = 25,
        resp_low_hz: float = 0.1,
        resp_high_hz: float = 0.7,
    ):
        self.fs = fs
        self.finger_threshold = finger_threshold

        # Cadena de dibujo: suave, queda linda en pantalla
        self.ir_display = band_pass(highpass_hz, lowpass_hz, fs)

        # Cadena de medicion: pasa-altos de 4 polos para que la respiracion no
        # se filtre dentro de la banda del pulso
        self.ir_measure = _measure_chain(highpass_hz, lowpass_hz, fs)
        self.red_measure = _measure_chain(highpass_hz, lowpass_hz, fs)

        # La continua se estima con una media movil de 1 s
        self.ir_dc = MovingAverage(max(4, int(fs)))
        self.red_dc = MovingAverage(max(4, int(fs)))
        self.ir_dc_value = 0.0
        self.red_dc_value = 0.0

        # Ventana del latido en curso (se vacia en cada pulso detectado)
        limit = max(8, int(3.0 * fs))
        self._win_ir: deque[float] = deque(maxlen=limit)
        self._win_red: deque[float] = deque(maxlen=limit)
        self._win_ir_dc: deque[float] = deque(maxlen=limit)
        self._win_red_dc: deque[float] = deque(maxlen=limit)

        # Deteccion de pulso por envolvente adaptativa
        self._env_hi = 0.0
        self._env_lo = 0.0
        self._prev_value = 0.0
        self._refractory = int(0.30 * fs)  # 200 lpm como techo
        self._since_pulse = self._refractory
        # Tiempo que tardan en asentarse los pasa-altos de 0.5 Hz
        self._settle = int(3.0 * fs)

        self._sample_index = 0
        self._last_pulse_index: int | None = None
        self.pulse_intervals: deque[float] = deque(maxlen=8)
        self._last_pulse_monotonic = 0.0

        self._ratios: deque[float] = deque(maxlen=max(3, int(spo2_window_s)))
        self._perfusions: deque[float] = deque(maxlen=5)

        # Respiracion: se decima el IR y se filtra en la banda respiratoria
        self.resp_fs = max(1, int(resp_fs))
        self._resp_decim = max(1, int(round(fs / self.resp_fs)))
        self.resp_fs = fs / self._resp_decim
        self._resp_counter = 0
        self._resp_pre = low_pass(resp_high_hz * 2.0, fs)
        self.resp_filter = band_pass(resp_low_hz, resp_high_hz, self.resp_fs)
        self._resp_index = 0
        self._resp_last = 0.0
        self._resp_rising = False
        self._resp_last_peak_index: int | None = None
        self._resp_periods: deque[float] = deque(maxlen=5)
        # Un pasa-altos de 0.1 Hz tarda bastante en asentarse. Mostrar el
        # transitorio da un numero de RESP disparatado y dispara alarmas falsas.
        self._resp_settle = int(self.resp_fs * 12)

        # Resultados
        self.spo2: float | None = None
        self.pr_bpm: float | None = None
        self.perfusion_index: float | None = None
        self.resp_rpm: float | None = None
        self.finger_detected = False

    # -- API principal -----------------------------------------------------

    def process(self, red: list[int], ir: list[int]) -> tuple[list[float], list[float], int]:
        """Devuelve (pleth para dibujar, muestras de respiracion, pulsos nuevos)."""
        pleth: list[float] = []
        resp_out: list[float] = []
        pulses = 0

        for index in range(len(ir)):
            ir_raw = float(ir[index])
            red_raw = float(red[index])
            self._sample_index += 1

            self.ir_dc_value = self.ir_dc.process(ir_raw)
            self.red_dc_value = self.red_dc.process(red_raw)
            self.finger_detected = self.ir_dc_value > self.finger_threshold

            # Se invierte: mas absorcion es menos luz, y el pico va para arriba
            pleth.append(-self.ir_display.process(ir_raw))

            ir_meas = -self.ir_measure.process(ir_raw)
            red_meas = -self.red_measure.process(red_raw)
            self._win_ir.append(ir_meas)
            self._win_red.append(red_meas)
            self._win_ir_dc.append(self.ir_dc_value)
            self._win_red_dc.append(self.red_dc_value)

            if self._detect_pulse(ir_meas):
                self._register_pulse()
                self._measure_beat()
                pulses += 1

            # Respiracion (decimada)
            smoothed = self._resp_pre.process(ir_raw)
            self._resp_counter += 1
            if self._resp_counter >= self._resp_decim:
                self._resp_counter = 0
                value = self.resp_filter.process(smoothed)
                self._track_respiration(value)
                # Mientras se asienta el filtro dibujamos linea recta
                resp_out.append(value if self._resp_ready else 0.0)

        if not self.finger_detected:
            self._invalidate()

        return pleth, resp_out, pulses

    # -- deteccion de pulso ------------------------------------------------

    def _detect_pulse(self, value: float) -> bool:
        self._since_pulse += 1
        if self._sample_index <= self._settle or not self.finger_detected:
            self._prev_value = value
            return False

        # Seguidor de envolvente: sube de golpe, baja despacio
        span = max(self._env_hi - self._env_lo, 1e-9)
        decay = span / (3.0 * self.fs)
        self._env_hi = value if value > self._env_hi else self._env_hi - decay
        self._env_lo = value if value < self._env_lo else self._env_lo + decay

        threshold = self._env_lo + 0.55 * (self._env_hi - self._env_lo)
        crossed = self._prev_value <= threshold < value
        self._prev_value = value

        if crossed and self._since_pulse >= self._refractory:
            self._since_pulse = 0
            return True
        return False

    def _register_pulse(self) -> None:
        if self._last_pulse_index is not None:
            interval = (self._sample_index - self._last_pulse_index) / self.fs
            if 0.3 <= interval <= 2.5:
                self.pulse_intervals.append(interval)
        self._last_pulse_index = self._sample_index
        self._last_pulse_monotonic = time.monotonic()
        if len(self.pulse_intervals) >= 3:
            self.pr_bpm = round(60.0 / median(list(self.pulse_intervals)))

    # -- SpO2 --------------------------------------------------------------

    def _measure_beat(self) -> None:
        """Mide AC pico a pico sobre el latido que acaba de cerrar."""
        n = len(self._win_ir)
        # Un latido tiene que durar al menos 250 ms para ser creible
        if n >= int(0.25 * self.fs):
            ir_ac = max(self._win_ir) - min(self._win_ir)
            red_ac = max(self._win_red) - min(self._win_red)
            ir_dc = sum(self._win_ir_dc) / n
            red_dc = sum(self._win_red_dc) / n

            if ir_dc > 0 and red_dc > 0 and ir_ac > 0:
                perfusion = ir_ac / ir_dc * 100.0
                # Un dedo real da entre 0.02% y ~20%. Fuera de ese rango es el
                # transitorio de los filtros o el paciente moviendo la mano.
                if 0.02 <= perfusion <= 20.0:
                    self._perfusions.append(perfusion)
                    if len(self._perfusions) >= 3:
                        self.perfusion_index = round(median(list(self._perfusions)), 2)

                # Con perfusion muy baja la relacion es puro ruido
                if 0.05 <= perfusion <= 20.0:
                    ratio = (red_ac / red_dc) / (ir_ac / ir_dc)
                    if 0.2 <= ratio <= 3.0:
                        self._ratios.append(ratio)
                        # Mediana de varios latidos: con menos de tres, un solo
                        # artefacto ya mueve el numero varios puntos
                        if len(self._ratios) >= 3:
                            r = median(list(self._ratios))
                            value = -45.06 * r * r + 30.354 * r + 94.845
                            self.spo2 = round(max(70.0, min(100.0, value)), 1)

        self._win_ir.clear()
        self._win_red.clear()
        self._win_ir_dc.clear()
        self._win_red_dc.clear()

    def _invalidate(self) -> None:
        self.spo2 = None
        self.pr_bpm = None
        self.perfusion_index = None
        self.resp_rpm = None
        self._ratios.clear()
        self._perfusions.clear()
        self.pulse_intervals.clear()
        self._last_pulse_index = None
        self._resp_periods.clear()
        self._resp_last_peak_index = None

    # -- respiracion -------------------------------------------------------

    @property
    def _resp_ready(self) -> bool:
        return self._resp_index > self._resp_settle

    def _track_respiration(self, value: float) -> None:
        self._resp_index += 1
        if not self._resp_ready or not self.finger_detected:
            self._resp_last = value
            return

        if value > 0 and self._resp_last <= 0:
            self._resp_rising = True
        if self._resp_rising and value < self._resp_last:
            self._resp_rising = False
            if self._resp_last_peak_index is not None:
                period = (self._resp_index - self._resp_last_peak_index) / self.resp_fs
                if 1.2 <= period <= 12.0:  # 5..50 rpm
                    self._resp_periods.append(period)
                    # Tres periodos antes de publicar: con dos, un artefacto
                    # suelto ya mueve el numero
                    if len(self._resp_periods) >= 3:
                        self.resp_rpm = round(60.0 / median(list(self._resp_periods)))
            self._resp_last_peak_index = self._resp_index
        self._resp_last = value

    # -- estado ------------------------------------------------------------

    def tick(self) -> None:
        if self.pr_bpm is not None and time.monotonic() - self._last_pulse_monotonic > 5.0:
            self.pr_bpm = None
            self.pulse_intervals.clear()
            self._last_pulse_index = None

    def seconds_since_pulse(self) -> float:
        if self._last_pulse_monotonic == 0.0:
            return 999.0
        return time.monotonic() - self._last_pulse_monotonic


def _measure_chain(highpass_hz: float, lowpass_hz: float, fs: float) -> FilterChain:
    """Pasa-altos de 4 polos + pasa-bajos de 2, para aislar solo el pulso."""
    chain = FilterChain()
    chain.add(high_pass(highpass_hz, fs))
    chain.add(high_pass(highpass_hz, fs))
    chain.add(low_pass(lowpass_hz, fs))
    return chain
