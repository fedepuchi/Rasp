"""AD8232 leido por un ADS1115 (I2C), mas deteccion de electrodo suelto.

El ADS1115 se deja en modo de conversion continua sobre un solo canal: asi
alcanza con leer el registro de conversion, sin esperar a que termine cada
medicion. El techo del chip son 860 SPS; usamos 250 por defecto, que ya deja
ver bien el QRS y le sobra margen al Pi.

Cableado tipico:
    AD8232 OUTPUT -> ADS1115 A0
    AD8232 LO+    -> GPIO17
    AD8232 LO-    -> GPIO27
    AD8232 3.3V/GND y ADS1115 VDD/GND -> 3.3V del Pi (NO 5V: el AD8232 es de 3.3V)
    ADS1115 SDA/SCL -> GPIO2/GPIO3
"""

from __future__ import annotations

import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:  # pragma: no cover
    SMBus = None
    i2c_msg = None

try:
    from gpiozero import DigitalInputDevice, DigitalOutputDevice
except ImportError:  # pragma: no cover
    DigitalInputDevice = None
    DigitalOutputDevice = None


REG_CONVERSION = 0x00
REG_CONFIG = 0x01

# Bits del registro de configuracion
MUX_SINGLE = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}
PGA_BITS = {6.144: 0b000, 4.096: 0b001, 2.048: 0b010,
            1.024: 0b011, 0.512: 0b100, 0.256: 0b101}
DR_BITS = {8: 0b000, 16: 0b001, 32: 0b010, 64: 0b011,
           128: 0b100, 250: 0b101, 475: 0b110, 860: 0b111}

MODE_CONTINUOUS = 0
COMP_DISABLE = 0b11


class AdsError(RuntimeError):
    pass


class ADS1115:
    """Lectura continua de un canal simple."""

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x48,
        channel: int = 0,
        data_rate: int = 250,
        pga_volts: float = 4.096,
    ) -> None:
        if SMBus is None:
            raise AdsError("smbus2 no esta instalado. En el Pi: pip install smbus2")
        if channel not in MUX_SINGLE:
            raise AdsError(f"canal {channel} invalido (0..3)")
        if data_rate not in DR_BITS:
            raise AdsError(f"data_rate {data_rate} invalido. Opciones: {sorted(DR_BITS)}")
        if pga_volts not in PGA_BITS:
            raise AdsError(f"pga_volts {pga_volts} invalido. Opciones: {sorted(PGA_BITS)}")

        self.address = address
        self.data_rate = data_rate
        self.pga_volts = pga_volts
        self._bus_number = bus
        # 15 bits utiles con signo
        self.volts_per_count = pga_volts / 32768.0

        self._bus = SMBus(bus)
        self._config = (
            (0 << 15)  # OS: en modo continuo no se usa
            | (MUX_SINGLE[channel] << 12)
            | (PGA_BITS[pga_volts] << 9)
            | (MODE_CONTINUOUS << 8)
            | (DR_BITS[data_rate] << 5)
            | (0 << 4)  # comparador tradicional
            | (0 << 3)  # activo en bajo
            | (0 << 2)  # no latcheado
            | COMP_DISABLE
        )
        self._start()

    def _read_register(self, reg: int) -> int:
        """Lee un registro de 16 bits con transacciones explicitas.

        Se usa i2c_rdwr en vez de read_i2c_block_data a proposito: es el mismo
        camino que ya funciona con el MAX30102 en este Pi. read_i2c_block_data
        arma internamente el START repetido de otra forma, y hay
        combinaciones de placa y kernel donde eso devuelve EIO aunque las
        escrituras al mismo chip anden perfecto.
        """
        write = i2c_msg.write(self.address, [reg])
        read = i2c_msg.read(self.address, 2)
        self._bus.i2c_rdwr(write, read)
        data = list(read)
        return (data[0] << 8) | data[1]

    def _write_config(self, config: int) -> None:
        write = i2c_msg.write(self.address,
                              [REG_CONFIG, (config >> 8) & 0xFF, config & 0xFF])
        self._bus.i2c_rdwr(write)

    def _start(self) -> None:
        try:
            self._write_config(self._config)
        except OSError as exc:
            raise AdsError(
                f"No responde el ADS1115 en 0x{self.address:02X}. "
                "Verifica el cableado y 'i2cdetect -y 1'."
            ) from exc

        # Leemos de vuelta la configuracion: si el chip devuelve lo mismo que
        # le escribimos, es efectivamente un ADS1115 y el bus anda en los dos
        # sentidos. Sin esta comprobacion, un dispositivo distinto en la misma
        # direccion se descubre recien cuando las lecturas dan basura.
        try:
            readback = self._read_register(REG_CONFIG)
        except OSError as exc:
            raise AdsError(
                f"El chip en 0x{self.address:02X} acepta escrituras pero no se "
                f"deja leer ({exc}). Suele ser SDA flojo, o un dispositivo que "
                f"no es un ADS1115 respondiendo en esa direccion. "
                f"Comproba a mano con:  i2cget -y {self._bus_number} "
                f"0x{self.address:02X} 0x01 w"
            ) from exc

        # El bit 15 (OS) es de solo lectura y refleja el estado, no lo que se
        # escribio: se ignora en la comparacion.
        if (readback & 0x7FFF) != (self._config & 0x7FFF):
            raise AdsError(
                f"El chip en 0x{self.address:02X} responde pero no se comporta "
                f"como un ADS1115: se escribio 0x{self._config:04X} y devolvio "
                f"0x{readback:04X}. Revisa que sea realmente un ADS1115 y que "
                f"el pin ADDR este firme a GND."
            )

        # Le damos tiempo a que salga la primera conversion
        time.sleep(2.0 / self.data_rate)

    def read_counts(self) -> int:
        """Ultima conversion, en cuentas con signo (-32768..32767)."""
        value = self._read_register(REG_CONVERSION)
        if value & 0x8000:
            value -= 0x10000
        return value

    def read_volts(self) -> float:
        return self.read_counts() * self.volts_per_count

    def sleep(self) -> None:
        """Pasa a disparo unico: el ADS1115 se apaga solo entre conversiones.

        Como no lo vamos a disparar, se queda en bajo consumo indefinidamente.
        """
        try:
            self._write_config(self._config | (1 << 8))  # MODE = single-shot
        except OSError:
            pass

    def wake(self) -> None:
        """Vuelve a conversion continua."""
        self._start()

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass


class LeadsOffDetector:
    """Pines LO+ / LO- del AD8232. En alto = ese electrodo esta despegado."""

    def __init__(self, lo_plus_pin: int | None, lo_minus_pin: int | None) -> None:
        self._lo_plus = None
        self._lo_minus = None
        self.available = False
        if DigitalInputDevice is None:
            return
        try:
            if lo_plus_pin is not None:
                self._lo_plus = DigitalInputDevice(lo_plus_pin, pull_up=False)
            if lo_minus_pin is not None:
                self._lo_minus = DigitalInputDevice(lo_minus_pin, pull_up=False)
            self.available = self._lo_plus is not None or self._lo_minus is not None
        except Exception as exc:  # pines ocupados, sin permisos, etc.
            print(f"[ecg] no se pudo abrir LO+/LO-: {exc}")
            self.available = False

    def read(self) -> tuple[bool, bool]:
        """(lo_plus, lo_minus). True = electrodo suelto."""
        if not self.available:
            return False, False
        plus = bool(self._lo_plus.value) if self._lo_plus is not None else False
        minus = bool(self._lo_minus.value) if self._lo_minus is not None else False
        return plus, minus

    def close(self) -> None:
        for pin in (self._lo_plus, self._lo_minus):
            try:
                if pin is not None:
                    pin.close()
            except Exception:
                pass


class AnalogFrontendPower:
    """Pin SDN del AD8232, si esta cableado. Si no, no hace nada."""

    def __init__(self, sdn_pin: int | None) -> None:
        self._pin = None
        self.available = False
        if sdn_pin is None or DigitalOutputDevice is None:
            return
        try:
            # initial_value=False: arranca apagado, se enciende al medir
            self._pin = DigitalOutputDevice(sdn_pin, initial_value=False)
            self.available = True
        except Exception as exc:
            print(f"[ecg] no se pudo abrir SDN: {exc}")

    def on(self) -> None:
        if self._pin is not None:
            self._pin.on()

    def off(self) -> None:
        if self._pin is not None:
            self._pin.off()

    def close(self) -> None:
        try:
            if self._pin is not None:
                self._pin.close()
        except Exception:
            pass
