"""Hilos productores: leen los sensores a ritmo constante y entregan bloques.

Cada sensor corre en su propio hilo para que la temporizacion no dependa de
cuanto tarde el dibujado. El bucle principal drena las colas una vez por frame
y es el unico que toca el procesamiento, asi que no hace falta lock en los
filtros ni en los detectores.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from config import Config
from .ecg_ads1115 import ADS1115, AdsError, LeadsOffDetector
from .max30102 import MAX30102, Max30102Error
from .simulator import EcgSimulator, PpgSimulator


@dataclass
class Chunk:
    """Bloque de muestras crudas con el instante de la PRIMERA muestra."""

    t0: float  # time.time() de la primera muestra
    values: list[float] = field(default_factory=list)


@dataclass
class EcgChunk(Chunk):
    lo_plus: bool = False
    lo_minus: bool = False


@dataclass
class PpgChunk:
    t0: float
    red: list[int] = field(default_factory=list)
    ir: list[int] = field(default_factory=list)


class _PacedThread(threading.Thread):
    """Hilo con temporizacion por reloj monotonico (sin deriva acumulada)."""

    def __init__(self, name: str, period_s: float):
        super().__init__(name=name, daemon=True)
        self.period = period_s
        self._stop = threading.Event()
        self.error: str | None = None
        self.overruns = 0

    def stop(self) -> None:
        self._stop.set()

    def _sleep_until(self, target: float) -> None:
        remaining = target - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        elif remaining < -self.period * 5:
            self.overruns += 1


class EcgThread(_PacedThread):
    """Muestrea el ADS1115 a fs fija y agrupa en bloques de ~20 ms."""

    def __init__(self, cfg: Config, out: queue.Queue):
        fs = cfg.ecg.sample_rate_hz
        super().__init__("ecg", 1.0 / fs)
        self.cfg = cfg
        self.out = out
        self.fs = fs
        self.block_size = max(1, int(round(fs * 0.02)))
        self._adc: ADS1115 | None = None
        self._leads: LeadsOffDetector | None = None
        self._sim: EcgSimulator | None = None
        self.leads_off_supported = False
        self.force_leads_off = False  # solo para el modo demo

    def setup(self) -> None:
        if self.cfg.demo:
            self._sim = EcgSimulator(self.fs)
            return
        self._adc = ADS1115(
            bus=self.cfg.ecg.i2c_bus,
            address=self.cfg.ecg.ads_address,
            channel=self.cfg.ecg.ads_channel,
            data_rate=self.cfg.ecg.sample_rate_hz,
            pga_volts=self.cfg.ecg.pga_volts,
        )
        self._leads = LeadsOffDetector(
            self.cfg.ecg.lo_plus_pin, self.cfg.ecg.lo_minus_pin
        )
        self.leads_off_supported = self._leads.available

    def run(self) -> None:
        next_t = time.monotonic()
        buf: list[float] = []
        t0 = time.time()
        lo_plus = lo_minus = False
        leads_check_at = 0.0

        while not self._stop.is_set():
            next_t += self.period
            try:
                if self._sim is not None:
                    value = self._sim.step()
                else:
                    value = float(self._adc.read_counts())
            except OSError as exc:
                self.error = f"I2C ECG: {exc}"
                time.sleep(0.25)
                next_t = time.monotonic()
                continue

            if not buf:
                t0 = time.time()
            buf.append(value)

            now = time.monotonic()
            if self._leads is not None and now >= leads_check_at:
                lo_plus, lo_minus = self._leads.read()
                leads_check_at = now + 0.1
            if self.force_leads_off:
                lo_plus = lo_minus = True

            if len(buf) >= self.block_size:
                try:
                    self.out.put_nowait(
                        EcgChunk(t0=t0, values=buf, lo_plus=lo_plus, lo_minus=lo_minus)
                    )
                except queue.Full:
                    pass  # la UI se colgo; preferimos perder muestras a crecer sin techo
                buf = []

            self._sleep_until(next_t)

    def close(self) -> None:
        if self._adc is not None:
            self._adc.close()
        if self._leads is not None:
            self._leads.close()


class PpgThread(_PacedThread):
    """Vacia la FIFO del MAX30102 cada read_interval_s."""

    def __init__(self, cfg: Config, out: queue.Queue):
        super().__init__("ppg", cfg.ppg.read_interval_s)
        self.cfg = cfg
        self.out = out
        self.fs = cfg.ppg.sample_rate_hz / cfg.ppg.averaging
        self._sensor: MAX30102 | None = None
        self._sim: PpgSimulator | None = None
        self.die_temp_c: float | None = None

    def setup(self) -> None:
        if self.cfg.demo:
            self._sim = PpgSimulator(self.fs)
            self.die_temp_c = 30.2  # el MAX30102 tibio, como en la realidad
            return
        self._sensor = MAX30102(
            bus=self.cfg.ppg.i2c_bus,
            address=self.cfg.ppg.address,
            sample_rate_hz=self.cfg.ppg.sample_rate_hz,
            averaging=self.cfg.ppg.averaging,
            pulse_width_us=self.cfg.ppg.pulse_width_us,
            adc_range_na=self.cfg.ppg.adc_range_na,
            led_red_current=self.cfg.ppg.led_red_current,
            led_ir_current=self.cfg.ppg.led_ir_current,
        )
        self.fs = self._sensor.output_rate_hz

    def run(self) -> None:
        next_t = time.monotonic()
        temp_at = 0.0
        pending = 0.0  # muestras fraccionarias acumuladas en modo demo

        while not self._stop.is_set():
            next_t += self.period
            try:
                if self._sim is not None:
                    pending += self.fs * self.period
                    n = int(pending)
                    pending -= n
                    red, ir = self._sim.block(n)
                else:
                    red, ir = self._sensor.read_fifo()
                    now = time.monotonic()
                    if now >= temp_at:
                        self.die_temp_c = self._sensor.read_temperature()
                        temp_at = now + 5.0
            except OSError as exc:
                self.error = f"I2C PPG: {exc}"
                time.sleep(0.25)
                next_t = time.monotonic()
                continue

            if red:
                # t0 = cuando se tomo la primera muestra del bloque
                t0 = time.time() - len(red) / self.fs
                try:
                    self.out.put_nowait(PpgChunk(t0=t0, red=red, ir=ir))
                except queue.Full:
                    pass

            self._sleep_until(next_t)

    def close(self) -> None:
        if self._sensor is not None:
            self._sensor.close()


class AcquisitionManager:
    """Arranca, para y drena los hilos de sensores."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ecg_queue: queue.Queue = queue.Queue(maxsize=256)
        self.ppg_queue: queue.Queue = queue.Queue(maxsize=256)
        self.ecg = EcgThread(cfg, self.ecg_queue)
        self.ppg = PpgThread(cfg, self.ppg_queue)
        self.ecg_ready = False
        self.ppg_ready = False
        self.errors: list[str] = []

    def start(self) -> None:
        try:
            self.ecg.setup()
            self.ecg.start()
            self.ecg_ready = True
        except (AdsError, OSError) as exc:
            self.errors.append(f"ECG no disponible: {exc}")
        try:
            self.ppg.setup()
            self.ppg.start()
            self.ppg_ready = True
        except (Max30102Error, OSError) as exc:
            self.errors.append(f"SpO2 no disponible: {exc}")

    def drain_ecg(self) -> list[EcgChunk]:
        out: list[EcgChunk] = []
        while True:
            try:
                out.append(self.ecg_queue.get_nowait())
            except queue.Empty:
                return out

    def drain_ppg(self) -> list[PpgChunk]:
        out: list[PpgChunk] = []
        while True:
            try:
                out.append(self.ppg_queue.get_nowait())
            except queue.Empty:
                return out

    def stop(self) -> None:
        self.ecg.stop()
        self.ppg.stop()
        for thread in (self.ecg, self.ppg):
            if thread.is_alive():
                thread.join(timeout=1.0)
        self.ecg.close()
        self.ppg.close()

    # -- controles del modo demo ------------------------------------------
    # Sirven para mostrar las alarmas en una presentacion sin tener que
    # provocarlas de verdad sobre una persona.

    @property
    def demo_available(self) -> bool:
        return self.cfg.demo and self.ecg._sim is not None and self.ppg._sim is not None

    def demo_shift_hr(self, delta: float) -> float | None:
        if not self.demo_available:
            return None
        value = max(25.0, min(200.0, self.ecg._sim.hr + delta))
        self.ecg._sim.hr = value
        self.ppg._sim.hr = value
        return value

    def demo_shift_spo2(self, delta: float) -> float | None:
        if not self.demo_available:
            return None
        value = max(70.0, min(100.0, self.ppg._sim.spo2 + delta))
        self.ppg._sim.spo2 = value
        return value

    def demo_toggle_finger(self) -> bool | None:
        if not self.demo_available:
            return None
        self.ppg._sim.finger = not self.ppg._sim.finger
        return self.ppg._sim.finger

    def demo_toggle_leads(self) -> bool | None:
        if not self.cfg.demo:
            return None
        self.ecg.force_leads_off = not self.ecg.force_leads_off
        return self.ecg.force_leads_off
