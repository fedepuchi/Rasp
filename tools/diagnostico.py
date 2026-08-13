"""Prueba cada modulo por separado y dice que anda y que no.

No abre ninguna ventana, asi que corre por SSH sin pantalla. Es lo primero que
conviene ejecutar cuando el monitor "no lee nada": separa un problema de
cableado de uno de configuracion o de software.

    python tools/diagnostico.py
    python tools/diagnostico.py --segundos 10 --config config.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402

OK = "[ OK ]"
FALLA = "[FALLA]"
AVISO = "[AVISO]"

_problemas: list[str] = []


def titulo(texto: str) -> None:
    print(f"\n{'=' * 62}\n  {texto}\n{'=' * 62}")


def falla(texto: str, arreglo: str = "") -> None:
    print(f"{FALLA} {texto}")
    _problemas.append(texto + (f"\n         -> {arreglo}" if arreglo else ""))


def aviso(texto: str) -> None:
    print(f"{AVISO} {texto}")


def ok(texto: str) -> None:
    print(f"{OK} {texto}")


# ---------------------------------------------------------------------------


def probar_i2c(cfg: Config) -> set[int]:
    titulo("BUS I2C")
    try:
        from smbus2 import SMBus
    except ImportError:
        falla("smbus2 no esta instalado",
              "cd ~/monitor_vital && .venv/bin/pip install smbus2")
        return set()

    encontrados = set()
    try:
        with SMBus(cfg.ecg.i2c_bus) as bus:
            for address in range(0x03, 0x78):
                try:
                    bus.write_quick(address)
                    encontrados.add(address)
                except OSError:
                    pass
    except (OSError, PermissionError) as exc:
        falla(f"no se pudo abrir el bus I2C {cfg.ecg.i2c_bus}: {exc}",
              "sudo raspi-config nonint do_i2c 0  y reinicia")
        return set()

    if encontrados:
        ok("direcciones encontradas: " +
           "  ".join(f"0x{a:02X}" for a in sorted(encontrados)))
    else:
        falla("no hay ningun dispositivo en el bus I2C",
              "revisa alimentacion y que SDA/SCL vayan a los pines 3 y 5")

    for address, nombre in ((cfg.ecg.ads_address, "ADS1115"),
                            (cfg.ppg.address, "MAX30102")):
        if address in encontrados:
            ok(f"{nombre} responde en 0x{address:02X}")
        else:
            falla(f"{nombre} NO responde en 0x{address:02X}",
                  "revisa el cableado, o corregi la direccion en config.py")
    return encontrados


def probar_max30102(cfg: Config, segundos: float) -> None:
    titulo("MAX30102  (SpO2 y pulso)")
    from sensors.max30102 import MAX30102, DataReadyPin, Max30102Error

    try:
        sensor = MAX30102(
            bus=cfg.ppg.i2c_bus, address=cfg.ppg.address,
            sample_rate_hz=cfg.ppg.sample_rate_hz, averaging=cfg.ppg.averaging,
            pulse_width_us=cfg.ppg.pulse_width_us,
            adc_range_na=cfg.ppg.adc_range_na,
            led_red_current=cfg.ppg.led_red_current,
            led_ir_current=cfg.ppg.led_ir_current,
        )
    except (Max30102Error, OSError) as exc:
        falla(f"no se pudo inicializar: {exc}")
        return

    ok(f"PART_ID correcto (0x15), revision 0x{sensor.revision:02X}")
    ok(f"configurado a {sensor.output_rate_hz:.0f} Hz de salida")

    pin_int = DataReadyPin(cfg.ppg.int_pin)
    if cfg.ppg.int_pin is None:
        aviso("INT no configurado (no hace falta: la FIFO se lee por sondeo)")
    elif not pin_int.available:
        aviso(f"INT configurado en GPIO{cfg.ppg.int_pin} pero no se pudo abrir")

    print(f"\n  Poné el dedo en el sensor. Midiendo {segundos:.0f} s...\n")
    sensor.wake()
    time.sleep(0.1)

    rojo: list[int] = []
    ir: list[int] = []
    fin = time.monotonic() + segundos
    ultimo_aviso = 0.0
    try:
        while time.monotonic() < fin:
            r, i = sensor.read_fifo()
            rojo.extend(r)
            ir.extend(i)
            ahora = time.monotonic()
            if ir and ahora - ultimo_aviso > 1.0:
                ultimo_aviso = ahora
                print(f"    IR {ir[-1]:7d}   ROJO {rojo[-1]:7d}   "
                      f"muestras {len(ir):5d}", flush=True)
            time.sleep(0.02)
    except OSError as exc:
        falla(f"se corto la lectura: {exc}")

    temperatura = None
    try:
        temperatura = sensor.read_temperature()
    except OSError:
        pass
    sensor.shutdown()

    print()
    if not ir:
        falla("no llego ni una muestra",
              "el chip responde pero no convierte: revisa la alimentacion")
        pin_int.close()
        return

    esperadas = segundos * sensor.output_rate_hz
    ok(f"{len(ir)} muestras en {segundos:.0f} s "
       f"(esperadas ~{esperadas:.0f})")
    if len(ir) < esperadas * 0.5:
        aviso("llegaron muchas menos de las esperadas: el bus I2C puede estar "
              "a 100 kHz. Subilo a 400 kHz en /boot/firmware/config.txt")

    dc_ir = sum(ir) / len(ir)
    dc_rojo = sum(rojo) / len(rojo)
    variacion = max(ir) - min(ir)
    print(f"       DC infrarrojo {dc_ir:9.0f}   DC rojo {dc_rojo:9.0f}")
    print(f"       variacion del infrarrojo: {variacion:.0f} cuentas")

    # Con un dedo puesto el infrarrojo SIEMPRE tiene que dar mas alto que el
    # rojo: el tejido absorbe mucho mas el rojo. Al reves significa que el
    # modulo entrega los LED cambiados respecto de la hoja de datos, cosa
    # frecuente en los clones. Con los canales invertidos la relacion R queda
    # dada vuelta y el SpO2 sale disparatado.
    if dc_ir < cfg.ppg.finger_threshold:
        aviso("NO HABIA DEDO EN EL SENSOR durante esta prueba")
        print(f"         El DC del infrarrojo dio {dc_ir:.0f} y el umbral de dedo")
        print(f"         es {cfg.ppg.finger_threshold}. Con un dedo apoyado tiene que")
        print("         dar decenas de miles.")
        print("         La comprobacion de canales rojo/IR invertidos NO se pudo")
        print("         hacer: repeti la prueba con el dedo puesto desde el")
        print("         principio y sin moverlo.")
        _problemas.append("la prueba del MAX30102 se hizo sin el dedo puesto"
                          "\n         -> repetila apoyando el dedo antes de que "
                          "arranque la cuenta")

    if dc_ir > cfg.ppg.finger_threshold and dc_rojo > dc_ir:
        if cfg.ppg.swap_leds:
            falla("el rojo sigue leyendo mas alto que el infrarrojo aun con "
                  "swap_leds activado",
                  "proba volver swap_leds a false: puede que el problema sea otro")
        else:
            falla("el ROJO lee mas alto que el INFRARROJO, y tiene que ser al reves",
                  "tu modulo trae los LED invertidos. Arreglo:\n"
                  "            echo '{\"ppg\": {\"swap_leds\": true}}' "
                  "> ~/monitor_vital/config.json\n"
                  "            y despues corre todo con  --config config.json")
    elif dc_ir > cfg.ppg.finger_threshold:
        ok("el infrarrojo lee mas alto que el rojo, como corresponde")

    if dc_ir >= cfg.ppg.finger_threshold:
        ok("hay dedo detectado")
        if variacion < dc_ir * 0.002:
            aviso("la senial casi no varia: el dedo esta muy apretado, o muy "
                  "flojo, o el sensor no hace buen contacto")
        else:
            ok("la senial pulsa")

    if temperatura is not None:
        ok(f"temperatura del chip {temperatura:.1f} C (del chip, NO del paciente)")
    if cfg.ppg.int_pin is not None and pin_int.available:
        if pin_int.pulses:
            ok(f"INT aviso {pin_int.pulses} veces en GPIO{cfg.ppg.int_pin}")
        else:
            aviso(f"INT nunca cambio: no esta conectado a GPIO{cfg.ppg.int_pin}, "
                  f"o esta en otro pin. No afecta la medicion")
    pin_int.close()


def probar_ads1115(cfg: Config, segundos: float) -> None:
    titulo("AD8232 + ADS1115  (ECG)")
    from sensors.ecg_ads1115 import ADS1115, AdsError

    try:
        adc = ADS1115(
            bus=cfg.ecg.i2c_bus, address=cfg.ecg.ads_address,
            channel=cfg.ecg.ads_channel, data_rate=cfg.ecg.sample_rate_hz,
            pga_volts=cfg.ecg.pga_volts,
        )
    except (AdsError, OSError) as exc:
        falla(f"no se pudo inicializar el ADS1115: {exc}")
        return

    ok(f"ADS1115 en 0x{cfg.ecg.ads_address:02X}, canal A{cfg.ecg.ads_channel}, "
       f"{cfg.ecg.sample_rate_hz} SPS")

    print(f"\n  Poné los electrodos. Midiendo {segundos:.0f} s...\n")
    muestras: list[float] = []
    fin = time.monotonic() + segundos
    ultimo_aviso = 0.0
    periodo = 1.0 / cfg.ecg.sample_rate_hz
    try:
        while time.monotonic() < fin:
            valor = adc.read_counts() * adc.volts_per_count
            muestras.append(valor)
            ahora = time.monotonic()
            if ahora - ultimo_aviso > 1.0:
                ultimo_aviso = ahora
                print(f"    {valor:6.3f} V   muestras {len(muestras):5d}",
                      flush=True)
            time.sleep(periodo)
    except OSError as exc:
        falla(f"se corto la lectura: {exc}")
    adc.close()

    print()
    if not muestras:
        falla("no llego ni una muestra del ADS1115")
        return

    media = sum(muestras) / len(muestras)
    minimo, maximo = min(muestras), max(muestras)
    esperado = cfg.ecg.supply_volts / 2.0
    print(f"       media {media:.3f} V   min {minimo:.3f} V   max {maximo:.3f} V")
    print(f"       esperado en reposo: cerca de {esperado:.2f} V")

    if media < 0.2:
        falla("la salida del AD8232 esta pegada a masa",
              "revisa que el modulo tenga 3.3 V y GND, y que OUTPUT vaya a A0")
    elif media > cfg.ecg.supply_volts - 0.2:
        falla("la salida del AD8232 esta pegada a la alimentacion",
              "electrodos despegados o modulo alimentado a 5 V en vez de 3.3 V")
    elif abs(media - esperado) > 0.5:
        aviso(f"la continua esta lejos de {esperado:.2f} V: el frente analogico "
              f"puede estar mal polarizado")
    else:
        ok("la continua esta bien polarizada (cerca de VCC/2)")

    amplitud = maximo - minimo
    if amplitud < 0.002:
        falla("la senial no varia: es una linea plana",
              "electrodos sin gel, mal contacto, o el AD8232 sin alimentar")
    elif amplitud > cfg.ecg.supply_volts * 0.9:
        aviso("la senial recorre casi toda la escala: probablemente satura")
    else:
        ok(f"la senial varia {amplitud * 1000:.0f} mV pico a pico")


def probar_gpio(cfg: Config) -> None:
    titulo("GPIO  (electrodo suelto y buzzer)")

    # Esto es logica de configuracion pura y va primero: es el chequeo que
    # detecta el error mas dificil de encontrar a ojo, que es tener dos cosas
    # asignadas al mismo pin.
    usados: dict[int, str] = {}
    for pin, nombre in ((cfg.ecg.lo_plus_pin, "ECG LO+"),
                        (cfg.ecg.lo_minus_pin, "ECG LO-"),
                        (cfg.ppg.int_pin, "MAX30102 INT"),
                        (cfg.ui.buzzer_pin, "buzzer"),
                        (cfg.ecg.sdn_pin, "AD8232 SDN")):
        if pin is None:
            continue
        if pin in usados:
            falla(f"GPIO{pin} esta asignado a dos cosas: "
                  f"{usados[pin]} y {nombre}",
                  "corregi uno de los dos en config.py o en config.json")
        else:
            usados[pin] = nombre

    for pin, nombre in sorted(usados.items()):
        print(f"       GPIO{pin:<3d} (pin fisico {_pin_fisico(pin)})  {nombre}")

    if cfg.ecg.lo_plus_pin is None and cfg.ecg.lo_minus_pin is None:
        aviso("LO+ y LO- deshabilitados: no vas a tener aviso de electrodo suelto")
        return

    try:
        from gpiozero import DigitalInputDevice  # noqa: F401
    except ImportError:
        falla("gpiozero no esta instalado",
              "sudo apt install -y python3-gpiozero python3-lgpio")
        return

    from sensors.ecg_ads1115 import LeadsOffDetector

    detector = LeadsOffDetector(cfg.ecg.lo_plus_pin, cfg.ecg.lo_minus_pin)
    if not detector.available:
        falla("no se pudieron abrir los pines de electrodo suelto")
        return

    lo_plus, lo_minus = detector.read()
    print()
    if lo_plus or lo_minus:
        cuales = " y ".join(n for n, v in (("LO+", lo_plus), ("LO-", lo_minus)) if v)
        aviso(f"{cuales} en alto: el programa lo lee como ELECTRODO SUELTO")
        print("         Si tenes los electrodos puestos y bien pegados, "
              "entonces esos\n         pines tienen otra cosa conectada. "
              "El sintoma es ECG plano.")
    else:
        ok("LO+ y LO- en bajo: electrodos haciendo contacto")
    detector.close()


def probar_buzzer(cfg: Config) -> None:
    if cfg.ui.buzzer_pin is None:
        aviso("sin buzzer configurado")
        return
    titulo(f"BUZZER  (GPIO{cfg.ui.buzzer_pin}, pin fisico "
           f"{_pin_fisico(cfg.ui.buzzer_pin)})")
    from sensors.buzzer import Buzzer

    buzzer = Buzzer(cfg.ui.buzzer_pin, cfg.ui.buzzer_tone_hz)
    if not buzzer.available:
        falla("no se pudo abrir el pin del buzzer")
        return

    print("  Tenes que escuchar cuatro bips, del mas agudo al mas grave.")
    for factor in (1.15, 1.0, 0.85, 0.7):
        buzzer.beep(cfg.ui.buzzer_tone_hz * factor, 0.18)
        time.sleep(0.32)
    time.sleep(0.5)
    buzzer.close()
    ok("secuencia enviada")
    print("       Si no sono nada: revisa que sea un buzzer PASIVO y que este")
    print(f"       en el pin fisico {_pin_fisico(cfg.ui.buzzer_pin)} con GND.")
    print("       Si sono muy flojo, proba otro valor de ui.buzzer_tone_hz")
    print("       (los pasivos rinden mucho mas cerca de su resonancia).")


_GPIO_A_FISICO = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23,
    5: 29, 6: 31, 13: 33, 19: 35, 26: 37, 14: 8, 15: 10, 18: 12, 23: 16,
    24: 18, 25: 22, 8: 24, 7: 26, 12: 32, 16: 36, 20: 38, 21: 40,
}


def _pin_fisico(gpio: int) -> str:
    return str(_GPIO_A_FISICO.get(gpio, "?"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostico de hardware del monitor")
    parser.add_argument("--config", help="archivo JSON de configuracion")
    parser.add_argument("--segundos", type=float, default=6.0,
                        help="cuanto medir cada sensor (default 6)")
    parser.add_argument("--saltear-buzzer", action="store_true")
    args = parser.parse_args()

    cfg = Config.load(args.config)

    print("\nDIAGNOSTICO DEL MONITOR DE SIGNOS VITALES")
    print("Probando cada modulo por separado. Ctrl+C para cortar.\n")

    encontrados = probar_i2c(cfg)
    if cfg.ppg.address in encontrados:
        probar_max30102(cfg, args.segundos)
    if cfg.ecg.ads_address in encontrados:
        probar_ads1115(cfg, args.segundos)
    probar_gpio(cfg)
    if not args.saltear_buzzer:
        probar_buzzer(cfg)

    titulo("RESUMEN")
    if _problemas:
        print(f"  {len(_problemas)} problema(s) que hay que resolver:\n")
        for numero, problema in enumerate(_problemas, 1):
            print(f"  {numero}. {problema}")
        print()
        return 1

    print("  Todo el hardware responde.\n")
    print("  Si el monitor igual no muestra nada, acordate de que los sensores")
    print("  arrancan APAGADOS: hay que apretar X para que midan 10 segundos.")
    print("  Para que mida sin parar, como un monitor de cama:\n")
    print("      .venv/bin/python main.py --continuo\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCortado.")
        sys.exit(130)
