"""Driver minimo para el MAX30102 (pulsioximetro rojo + IR) por I2C.

Se habla directo con los registros con smbus2 en vez de usar el stack de
Adafruit: es mas liviano y permite vaciar la FIFO de a bloques, que es lo que
necesitamos para no perder muestras.

Hoja de datos: MAX30102, Maxim Integrated.
"""

from __future__ import annotations

import time

try:  # solo existe en el Pi
    from smbus2 import SMBus, i2c_msg
except ImportError:  # pragma: no cover - en la PC de desarrollo no esta
    SMBus = None
    i2c_msg = None

try:
    from gpiozero import DigitalInputDevice
except ImportError:  # pragma: no cover
    DigitalInputDevice = None


# --- registros -------------------------------------------------------------
REG_INTR_STATUS_1 = 0x00
REG_INTR_STATUS_2 = 0x01
REG_INTR_ENABLE_1 = 0x02
REG_INTR_ENABLE_2 = 0x03
REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C  # rojo
REG_LED2_PA = 0x0D  # infrarrojo
REG_TEMP_INT = 0x1F
REG_TEMP_FRAC = 0x20
REG_TEMP_CONFIG = 0x21
REG_REV_ID = 0xFE
REG_PART_ID = 0xFF

PART_ID_MAX30102 = 0x15

MODE_SPO2 = 0x03  # rojo + IR

# Tablas de codificacion de los campos de configuracion
_AVG_BITS = {1: 0b000, 2: 0b001, 4: 0b010, 8: 0b011, 16: 0b100, 32: 0b101}
_RATE_BITS = {50: 0b000, 100: 0b001, 200: 0b010, 400: 0b011,
              800: 0b100, 1000: 0b101, 1600: 0b110, 3200: 0b111}
_PW_BITS = {69: 0b00, 118: 0b01, 215: 0b10, 411: 0b11}
_RANGE_BITS = {2048: 0b00, 4096: 0b01, 8192: 0b10, 16384: 0b11}

FIFO_DEPTH = 32
BYTES_PER_SAMPLE = 6  # 3 bytes por LED, 2 LEDs en modo SpO2


class Max30102Error(RuntimeError):
    pass


class DataReadyPin:
    """Pin INT del MAX30102, si esta cableado.

    El driver NO lo necesita: vacia la FIFO por sondeo, que a 100 Hz sobra. Se
    lee unicamente como senial de vida, para poder distinguir un sensor
    apagado de uno que esta midiendo. El INT es de colector abierto y activo en
    bajo: en reposo queda alto y baja cuando hay algo que contar.
    """

    def __init__(self, pin: int | None) -> None:
        self._pin = None
        self.available = False
        self.pulses = 0
        if pin is None or DigitalInputDevice is None:
            return
        try:
            self._pin = DigitalInputDevice(pin, pull_up=True)
            self._pin.when_deactivated = self._on_pulse
            self.available = True
        except Exception as exc:
            print(f"[max30102] no se pudo abrir INT en {pin}: {exc}")

    def _on_pulse(self) -> None:
        self.pulses += 1

    @property
    def asserted(self) -> bool:
        """True si el INT esta activo ahora mismo (en bajo)."""
        return self.available and not bool(self._pin.value)

    def close(self) -> None:
        try:
            if self._pin is not None:
                self._pin.close()
        except Exception:
            pass


class MAX30102:
    def __init__(
        self,
        bus: int = 1,
        address: int = 0x57,
        sample_rate_hz: int = 400,
        averaging: int = 4,
        pulse_width_us: int = 411,
        adc_range_na: int = 4096,
        led_red_current: int = 0x24,
        led_ir_current: int = 0x24,
    ) -> None:
        if SMBus is None:
            raise Max30102Error(
                "smbus2 no esta instalado. En el Pi: pip install smbus2"
            )
        for value, table, name in (
            (averaging, _AVG_BITS, "averaging"),
            (sample_rate_hz, _RATE_BITS, "sample_rate_hz"),
            (pulse_width_us, _PW_BITS, "pulse_width_us"),
            (adc_range_na, _RANGE_BITS, "adc_range_na"),
        ):
            if value not in table:
                raise Max30102Error(
                    f"{name}={value} no es un valor valido. Opciones: {sorted(table)}"
                )

        self.address = address
        self.averaging = averaging
        self.sample_rate_hz = sample_rate_hz
        # Frecuencia real a la que salen muestras de la FIFO
        self.output_rate_hz = sample_rate_hz / averaging

        self._bus = SMBus(bus)
        self._cfg = (led_red_current, led_ir_current, pulse_width_us, adc_range_na)
        self._check_part_id()
        self.reset()
        self._configure()

    # -- registros ---------------------------------------------------------

    def _write(self, reg: int, value: int) -> None:
        self._bus.write_byte_data(self.address, reg, value & 0xFF)

    def _read(self, reg: int) -> int:
        return self._bus.read_byte_data(self.address, reg)

    def _read_block(self, reg: int, length: int) -> bytes:
        """Lectura larga con i2c_msg: read_i2c_block_data corta en 32 bytes."""
        write = i2c_msg.write(self.address, [reg])
        read = i2c_msg.read(self.address, length)
        self._bus.i2c_rdwr(write, read)
        return bytes(read)

    def _check_part_id(self) -> None:
        try:
            part = self._read(REG_PART_ID)
        except OSError as exc:
            raise Max30102Error(
                f"No responde nada en 0x{self.address:02X}. "
                "Revisa el cableado y que I2C este habilitado (raspi-config)."
            ) from exc
        if part != PART_ID_MAX30102:
            raise Max30102Error(
                f"PART_ID inesperado: 0x{part:02X} (se esperaba 0x15). "
                "Puede ser un MAX30100 u otro chip."
            )
        self.revision = self._read(REG_REV_ID)

    # -- ciclo de vida -----------------------------------------------------

    def reset(self) -> None:
        self._write(REG_MODE_CONFIG, 0x40)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not (self._read(REG_MODE_CONFIG) & 0x40):
                return
            time.sleep(0.01)
        raise Max30102Error("El MAX30102 no termino el reset")

    def _configure(self) -> None:
        led_red, led_ir, pulse_width, adc_range = self._cfg

        # FIFO: promediado + rollover activado + almost-full a 15 muestras libres
        fifo = (_AVG_BITS[self.averaging] << 5) | 0x10 | 0x0F
        self._write(REG_FIFO_CONFIG, fifo)

        self._write(REG_MODE_CONFIG, MODE_SPO2)

        spo2 = (
            (_RANGE_BITS[adc_range] << 5)
            | (_RATE_BITS[self.sample_rate_hz] << 2)
            | _PW_BITS[pulse_width]
        )
        self._write(REG_SPO2_CONFIG, spo2)

        self._write(REG_LED1_PA, led_red)
        self._write(REG_LED2_PA, led_ir)

        self._write(REG_INTR_ENABLE_1, 0x00)
        self._write(REG_INTR_ENABLE_2, 0x00)
        self.clear_fifo()

    def clear_fifo(self) -> None:
        self._write(REG_FIFO_WR_PTR, 0)
        self._write(REG_OVF_COUNTER, 0)
        self._write(REG_FIFO_RD_PTR, 0)

    def shutdown(self) -> None:
        """Modo bajo consumo: apaga los LED y para el conversor.

        Deja la configuracion intacta, asi despertar es solo volver a escribir
        el modo. Con los LED apagados el chip no se entibia, que es lo que pasa
        si se lo deja encendido con el dedo puesto un rato largo.
        """
        try:
            self._write(REG_MODE_CONFIG, 0x80)
        except OSError:
            pass

    def wake(self) -> None:
        """Sale del modo bajo consumo y empieza a llenar la FIFO de cero."""
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self.clear_fifo()
        # El primer par de muestras sale mientras los LED todavia estabilizan
        time.sleep(0.02)

    def close(self) -> None:
        self.shutdown()
        try:
            self._bus.close()
        except Exception:
            pass

    # -- lectura -----------------------------------------------------------

    def available(self) -> int:
        """Cuantas muestras hay esperando en la FIFO."""
        wr = self._read(REG_FIFO_WR_PTR)
        rd = self._read(REG_FIFO_RD_PTR)
        return (wr - rd) & (FIFO_DEPTH - 1)

    def read_fifo(self) -> tuple[list[int], list[int]]:
        """Vacia la FIFO. Devuelve (rojo, infrarrojo) en cuentas de 18 bits."""
        wr = self._read(REG_FIFO_WR_PTR)
        rd = self._read(REG_FIFO_RD_PTR)
        overflow = self._read(REG_OVF_COUNTER)
        count = (wr - rd) & (FIFO_DEPTH - 1)
        if overflow:
            # Se lleno la FIFO: perdimos muestras. Arrancamos limpio.
            self.clear_fifo()
            if count == 0:
                return [], []
        if count == 0:
            return [], []

        raw = self._read_block(REG_FIFO_DATA, count * BYTES_PER_SAMPLE)
        red: list[int] = []
        ir: list[int] = []
        for i in range(0, len(raw) - BYTES_PER_SAMPLE + 1, BYTES_PER_SAMPLE):
            red.append(((raw[i] << 16) | (raw[i + 1] << 8) | raw[i + 2]) & 0x03FFFF)
            ir.append(((raw[i + 3] << 16) | (raw[i + 4] << 8) | raw[i + 5]) & 0x03FFFF)
        return red, ir

    def read_interrupt_status(self) -> tuple[int, int]:
        """Registros de interrupcion. Leerlos los limpia.

        Util para diagnostico: si el bit A_FULL (0x80 del primero) se prende,
        el sensor esta llenando la FIFO de verdad.
        """
        return self._read(REG_INTR_STATUS_1), self._read(REG_INTR_STATUS_2)

    def read_temperature(self) -> float:
        """Temperatura del die, no del paciente. Sirve para compensar el LED."""
        # Limpiamos interrupciones viejas y pedimos una conversion
        self._read(REG_INTR_STATUS_2)
        self._write(REG_TEMP_CONFIG, 0x01)
        # OJO: el bit TEMP_EN se autolimpia apenas arranca la conversion, no
        # cuando termina. Esperarlo a el devuelve 0.0 siempre. Lo que hay que
        # esperar es DIE_TEMP_RDY del registro de interrupciones; la conversion
        # tarda unos 29 ms.
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if self._read(REG_INTR_STATUS_2) & 0x02:
                break
            time.sleep(0.005)
        integer = self._read(REG_TEMP_INT)
        if integer > 127:
            integer -= 256
        frac = self._read(REG_TEMP_FRAC) & 0x0F
        return integer + frac * 0.0625
