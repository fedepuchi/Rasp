"""Filtros IIR de segundo orden (biquads) y buffers circulares.

Escritos a mano para no arrastrar scipy: a 250 Hz el costo en Python puro es
despreciable y el Pi arranca mas rapido. Coeficientes segun el "Audio EQ
Cookbook" de Robert Bristow-Johnson, que es el estandar para biquads.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Iterable, Sequence


class Biquad:
    """Seccion de segundo orden en forma directa II transpuesta."""

    __slots__ = ("b0", "b1", "b2", "a1", "a2", "z1", "z2")

    def __init__(self, b0: float, b1: float, b2: float, a0: float, a1: float, a2: float):
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
        self.z1 = 0.0
        self.z2 = 0.0

    def reset(self) -> None:
        self.z1 = 0.0
        self.z2 = 0.0

    def process(self, x: float) -> float:
        y = self.b0 * x + self.z1
        self.z1 = self.b1 * x - self.a1 * y + self.z2
        self.z2 = self.b2 * x - self.a2 * y
        return y


def _omega(freq: float, fs: float) -> float:
    # Se limita a un poco menos de Nyquist para que no explote el diseno
    freq = min(max(freq, 0.001), fs * 0.49)
    return 2.0 * math.pi * freq / fs


def low_pass(freq: float, fs: float, q: float = 0.7071) -> Biquad:
    w0 = _omega(freq, fs)
    cos_w0, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
    b0 = (1.0 - cos_w0) / 2.0
    return Biquad(b0, 1.0 - cos_w0, b0, 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha)


def high_pass(freq: float, fs: float, q: float = 0.7071) -> Biquad:
    w0 = _omega(freq, fs)
    cos_w0, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
    b0 = (1.0 + cos_w0) / 2.0
    return Biquad(b0, -(1.0 + cos_w0), b0, 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha)


def notch(freq: float, fs: float, q: float = 30.0) -> Biquad:
    w0 = _omega(freq, fs)
    cos_w0, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
    return Biquad(1.0, -2.0 * cos_w0, 1.0, 1.0 + alpha, -2.0 * cos_w0, 1.0 - alpha)


class FilterChain:
    """Varios biquads en cascada."""

    def __init__(self, stages: Sequence[Biquad] | None = None):
        self.stages: list[Biquad] = list(stages or [])

    def add(self, stage: Biquad) -> "FilterChain":
        self.stages.append(stage)
        return self

    def reset(self) -> None:
        for stage in self.stages:
            stage.reset()

    def process(self, x: float) -> float:
        for stage in self.stages:
            x = stage.process(x)
        return x

    def process_block(self, xs: Iterable[float]) -> list[float]:
        stages = self.stages
        out = []
        for x in xs:
            for stage in stages:
                x = stage.process(x)
            out.append(x)
        return out


def band_pass(low_hz: float, high_hz: float, fs: float,
              notch_hz: float | None = None, notch_q: float = 30.0) -> FilterChain:
    """Pasabanda de 2 polos por lado, con notch de red opcional."""
    chain = FilterChain()
    if notch_hz is not None and notch_hz < fs / 2:
        chain.add(notch(notch_hz, fs, notch_q))
        # Segundo armonico: el ruido de red rara vez es una senoide limpia
        if notch_hz * 2 < fs / 2:
            chain.add(notch(notch_hz * 2, fs, notch_q))
    chain.add(high_pass(low_hz, fs))
    chain.add(low_pass(high_hz, fs))
    return chain


class RingBuffer:
    """Ventana deslizante de tamano fijo con media y min/max al vuelo."""

    __slots__ = ("_data", "capacity")

    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self._data: deque[float] = deque(maxlen=self.capacity)

    def push(self, value: float) -> None:
        self._data.append(value)

    def extend(self, values: Iterable[float]) -> None:
        self._data.extend(values)

    def __len__(self) -> int:
        return len(self._data)

    @property
    def full(self) -> bool:
        return len(self._data) >= self.capacity

    def values(self) -> list[float]:
        return list(self._data)

    def mean(self) -> float:
        if not self._data:
            return 0.0
        return sum(self._data) / len(self._data)

    def min_max(self) -> tuple[float, float]:
        if not self._data:
            return 0.0, 0.0
        return min(self._data), max(self._data)

    def rms_ac(self) -> float:
        """RMS despues de sacarle la continua."""
        n = len(self._data)
        if n < 2:
            return 0.0
        mean = sum(self._data) / n
        acc = 0.0
        for v in self._data:
            d = v - mean
            acc += d * d
        return math.sqrt(acc / n)


class MovingAverage:
    """Media movil con suma incremental (O(1) por muestra)."""

    __slots__ = ("_data", "_sum", "capacity")

    def __init__(self, capacity: int):
        self.capacity = max(1, capacity)
        self._data: deque[float] = deque(maxlen=self.capacity)
        self._sum = 0.0

    def process(self, value: float) -> float:
        if len(self._data) == self.capacity:
            self._sum -= self._data[0]
        self._data.append(value)
        self._sum += value
        return self._sum / len(self._data)

    def reset(self) -> None:
        self._data.clear()
        self._sum = 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
