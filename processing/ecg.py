"""Procesamiento del ECG: filtrado, deteccion de R y frecuencia cardiaca.

La deteccion de R es un Pan-Tompkins simplificado, que es el algoritmo clasico
para esto:

    pasabanda 5-15 Hz -> derivada -> cuadrado -> integracion en ventana movil
    -> umbral adaptativo con periodo refractario

No pretende ser de grado diagnostico, pero con electrodos bien puestos clava el
QRS de forma estable.

Los intervalos RR se miden contando muestras, no con el reloj de pared: asi la
frecuencia no depende de cuando llegan los bloques desde el hilo del sensor.
"""

from __future__ import annotations

import time
from collections import deque

from .filters import MovingAverage, band_pass, median


class EcgProcessor:
    def __init__(
        self,
        fs: float,
        highpass_hz: float = 0.5,
        lowpass_hz: float = 40.0,
        notch_hz: float | None = 50.0,
        notch_q: float = 30.0,
        counts_to_mv: float = 1.0,
        volts_per_count: float = 1.0,
        supply_volts: float = 3.3,
    ):
        self.fs = fs
        self.counts_to_mv = counts_to_mv
        self.volts_per_count = volts_per_count

        # El AD8232 saca entre 0 V y su alimentacion, centrado en VCC/2. Cuando
        # el electrodo se mueve o hace mal contacto, la salida pega contra un
        # riel y ahi lo que se ve en pantalla ya no es el corazon.
        self._sat_high = supply_volts / volts_per_count * 0.97
        self._sat_low = supply_volts / volts_per_count * 0.03
        self._raw_mean = MovingAverage(max(4, int(fs)))
        self._raw_mean_value = 0.0
        self._sat_window: deque[bool] = deque(maxlen=max(8, int(fs)))

        # Cadena de visualizacion: lo que se dibuja y se manda al backend
        self.display_filter = band_pass(highpass_hz, lowpass_hz, fs, notch_hz, notch_q)
        # Cadena de deteccion: mas angosta, centrada en la energia del QRS
        self.detect_filter = band_pass(5.0, 15.0, fs, notch_hz, notch_q)

        self._prev_sample = 0.0
        self._integrator = MovingAverage(max(1, int(0.150 * fs)))  # ventana de 150 ms

        # Umbrales adaptativos de Pan-Tompkins
        self._spki = 0.0  # nivel estimado de pico de senial
        self._npki = 0.0  # nivel estimado de pico de ruido
        self._threshold = 0.0
        # Los filtros arrancan con un transitorio enorme (escalon de continua).
        # Si lo dejamos entrar al aprendizaje, el umbral queda por las nubes y
        # no se detecta un solo latido en toda la sesion.
        self._settle = int(1.5 * fs)
        self._learning = int(2.0 * fs)
        self._samples_seen = 0
        # Decaimiento lento del nivel de senial: si cambia el paciente o la
        # posicion de los electrodos, el umbral acompania solo.
        self._spki_decay = 0.9999

        self._refractory = int(0.200 * fs)  # 200 ms: no puede haber 2 R tan juntas
        self._since_peak = self._refractory
        self._peak_candidate = 0.0
        self._rising = False

        self._sample_index = 0
        self._last_beat_index: int | None = None
        self.rr_intervals: deque[float] = deque(maxlen=8)  # en segundos, para la FC
        # Ventana mas larga para la variabilidad: con 8 latidos el RMSSD es puro
        # ruido. Ni con 30 llega a ser un RMSSD "de manual" (eso pide 5 minutos),
        # pero al menos es un numero de corto plazo defendible.
        self.rr_history: deque[float] = deque(maxlen=30)
        self.beat_count = 0
        self.hr_bpm: float | None = None
        self._last_beat_monotonic = 0.0

        self._noise_window: deque[float] = deque(maxlen=int(fs))
        self.leads_off = False

    # -- API principal -----------------------------------------------------

    def process(self, raw_counts: list[float]) -> tuple[list[float], int]:
        """Recibe cuentas crudas del ADS. Devuelve (mV filtrados, latidos nuevos)."""
        display: list[float] = []
        beats = 0

        for raw in raw_counts:
            filtered = self.display_filter.process(raw) * self.counts_to_mv
            display.append(filtered)
            self._noise_window.append(filtered)
            self._sample_index += 1

            # Diagnostico del frente analogico, sobre la senial SIN filtrar
            self._raw_mean_value = self._raw_mean.process(raw)
            self._sat_window.append(raw >= self._sat_high or raw <= self._sat_low)

            if self.leads_off:
                # Con un electrodo suelto lo que entra es basura: no detectamos,
                # pero seguimos alimentando los filtros para que no se desfasen.
                self.detect_filter.process(raw)
                self._since_peak += 1
                continue

            if self._detect(raw):
                self._register_beat()
                beats += 1

        return display, beats

    def _detect(self, raw: float) -> bool:
        band = self.detect_filter.process(raw)
        derivative = (band - self._prev_sample) * self.fs / 100.0
        self._prev_sample = band
        integrated = self._integrator.process(derivative * derivative)

        self._samples_seen += 1
        self._since_peak += 1

        if self._samples_seen <= self._settle:
            return False

        if self._samples_seen <= self._settle + self._learning:
            if integrated > self._spki:
                self._spki = integrated
            self._npki = 0.9 * self._npki + 0.1 * integrated
            self._refresh_threshold()
            return False

        self._spki *= self._spki_decay

        if integrated > self._threshold:
            self._rising = True
            if integrated > self._peak_candidate:
                self._peak_candidate = integrated
            return False

        if self._rising:
            # Bajamos del umbral: se cerro el pico
            self._rising = False
            peak = self._peak_candidate
            self._peak_candidate = 0.0
            if self._since_peak >= self._refractory:
                self._spki = 0.125 * peak + 0.875 * self._spki
                self._refresh_threshold()
                self._since_peak = 0
                return True
            return False

        self._npki = 0.125 * integrated + 0.875 * self._npki
        self._refresh_threshold()

        # Rescate: si hace demasiado que no aparece nada, el umbral quedo alto.
        # Sin esto, un umbral mal aprendido no se recupera nunca.
        if self.rr_intervals:
            starve = median(list(self.rr_intervals)) * 1.66 * self.fs
        else:
            starve = 2.0 * self.fs
        if self._since_peak > starve:
            self._spki *= 0.6
            self._refresh_threshold()
            self._since_peak = int(starve * 0.5)
        return False

    def _refresh_threshold(self) -> None:
        self._threshold = self._npki + 0.25 * (self._spki - self._npki)

    def _register_beat(self) -> None:
        if self._last_beat_index is not None:
            rr = (self._sample_index - self._last_beat_index) / self.fs
            if 0.24 <= rr <= 3.0:  # 20..250 lpm
                self.rr_intervals.append(rr)
                self.rr_history.append(rr)
        self._last_beat_index = self._sample_index
        self._last_beat_monotonic = time.monotonic()
        self.beat_count += 1

        if len(self.rr_intervals) >= 3:
            self.hr_bpm = 60.0 / median(list(self.rr_intervals))

    # -- estado ------------------------------------------------------------

    def tick(self) -> None:
        """Llamar una vez por frame: invalida la FC si hace rato que no hay latidos."""
        if self.hr_bpm is None:
            return
        if time.monotonic() - self._last_beat_monotonic > 5.0:
            self.hr_bpm = None
            self.rr_intervals.clear()
            self.rr_history.clear()
            self._last_beat_index = None

    def set_leads_off(self, off: bool) -> None:
        if off and not self.leads_off:
            self.hr_bpm = None
            self.rr_intervals.clear()
            self.rr_history.clear()
            self._last_beat_index = None
        self.leads_off = off

    @property
    def last_rr_ms(self) -> float | None:
        if not self.rr_intervals:
            return None
        return round(self.rr_intervals[-1] * 1000.0, 1)

    @property
    def rmssd_ms(self) -> float | None:
        """Variabilidad de corto plazo (RMSSD) sobre los ultimos ~30 latidos.

        El RMSSD "de verdad" se calcula sobre 5 minutos de registro limpio.
        Este es un valor de corto plazo: sirve para ver que se mueve, no para
        compararlo contra tablas de referencia.
        """
        rr = list(self.rr_history)
        if len(rr) < 12:
            return None
        diffs = [(rr[i + 1] - rr[i]) * 1000.0 for i in range(len(rr) - 1)]
        acc = sum(d * d for d in diffs) / len(diffs)
        return round(acc ** 0.5, 1)

    @property
    def baseline_v(self) -> float | None:
        """Continua de la salida del AD8232. Tiene que dar cerca de VCC/2.

        Si da casi 0 o casi VCC, el frente analogico esta pegado a un riel:
        electrodo suelto, mala alimentacion o el modulo directamente colgado.
        """
        if self._sample_index < self.fs:
            return None
        return self._raw_mean_value * self.volts_per_count

    @property
    def saturated(self) -> bool:
        """True si una parte apreciable del ultimo segundo pego contra un riel."""
        if len(self._sat_window) < self._sat_window.maxlen:
            return False
        return sum(self._sat_window) / len(self._sat_window) > 0.01

    @property
    def noise_level(self) -> float:
        """0 = limpio, 1 = muy ruidoso. Heuristico, para el indicador de calidad."""
        if len(self._noise_window) < 10:
            return 0.0
        values = list(self._noise_window)
        span = max(values) - min(values)
        if span <= 0:
            return 1.0
        # Energia de alta frecuencia contra amplitud total
        hf = sum(abs(values[i + 1] - values[i]) for i in range(len(values) - 1))
        hf /= len(values) - 1
        return min(1.0, hf / (span * 0.25 + 1e-9))

    def seconds_since_beat(self) -> float:
        if self._last_beat_monotonic == 0.0:
            return 999.0
        return time.monotonic() - self._last_beat_monotonic
