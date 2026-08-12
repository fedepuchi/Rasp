"""Buzzer PASIVO por PWM.

Un buzzer pasivo no tiene oscilador adentro: hay que darle una onda cuadrada a
la frecuencia que uno quiere escuchar. Por eso se maneja con PWM y no con un
simple encendido/apagado, y por eso se le puede cambiar el tono (que es lo que
hace el bip de latido para avisar que baja la saturacion).

Si tenes un buzzer ACTIVO, esto no sirve: ese suena solo con darle tension y a
una unica frecuencia. En ese caso dejá `ui.buzzer_pin` en null y usá el audio.

Los tonos se encolan y los reproduce un hilo aparte: si sonaran desde el bucle
principal, cada bip congelaria el dibujado los milisegundos que dura.
"""

from __future__ import annotations

import queue
import threading
import time

try:
    from gpiozero import PWMOutputDevice
except ImportError:  # pragma: no cover - en la PC de desarrollo no esta
    PWMOutputDevice = None


class Buzzer:
    def __init__(self, pin: int, default_hz: int = 2400):
        self.available = False
        self.default_hz = default_hz
        self._device = None
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        if PWMOutputDevice is None:
            print("[buzzer] gpiozero no esta instalado, se ignora el buzzer")
            return
        try:
            self._device = PWMOutputDevice(pin, frequency=default_hz,
                                           initial_value=0.0)
        except Exception as exc:  # pin ocupado, sin permisos, etc.
            print(f"[buzzer] no se pudo abrir el pin {pin}: {exc}")
            return

        self.available = True
        self._thread = threading.Thread(target=self._run, name="buzzer",
                                        daemon=True)
        self._thread.start()

    def beep(self, freq_hz: float | None = None, duration_s: float = 0.06) -> None:
        """Encola un tono. Si la cola esta llena, se descarta: mas vale perder
        un bip que acumular retraso y que suenen todos juntos despues."""
        if not self.available:
            return
        try:
            self._queue.put_nowait((float(freq_hz or self.default_hz),
                                    max(0.01, duration_s)))
        except queue.Full:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                freq, duration = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._device.frequency = freq
                self._device.value = 0.5  # 50% de ciclo: lo mas fuerte
                time.sleep(duration)
                self._device.value = 0.0
                # Silencio corto entre tonos, si no se pegan entre si
                time.sleep(0.02)
            except Exception:
                # Un fallo del GPIO no puede tumbar el monitor
                self.available = False
                return

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        if self._device is not None:
            try:
                self._device.value = 0.0
                self._device.close()
            except Exception:
                pass
