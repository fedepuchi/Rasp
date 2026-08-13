"""Monitor de signos vitales para Raspberry Pi.

    MAX30102 (SpO2/pulso) + AD8232 (ECG) -> ADS1115 -> Raspberry Pi
        -> pantalla estilo monitor de cabecera (pygame)
        -> JSON por HTTP a un backend en la misma red

Uso tipico:
    python main.py                          # con hardware
    python main.py --demo --windowed        # para probar en la PC
    python main.py --backend http://192.168.0.50:8000
    python main.py --save-config config.json    # escribe la config por defecto

AVISO: esto no es un equipo medico ni esta certificado. Sirve para aprender,
prototipar y ver tendencias, no para diagnosticar ni para tomar decisiones
clinicas sobre una persona.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Permite ejecutar el archivo directamente desde cualquier carpeta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pygame

from alarms import AlarmManager
from config import Config
from net import Publisher
from processing import EcgProcessor, PpgProcessor
from sensors import AcquisitionManager
from session import MeasurementSession
from state import VitalsSnapshot
from ui import MonitorUI
from ui.sound import SoundEngine


def _fmt(value: float | None, decimals: int) -> str:
    return "---" if value is None else f"{value:.{decimals}f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor de signos vitales (RPi)")
    parser.add_argument("--config", help="archivo JSON de configuracion")
    parser.add_argument("--save-config", metavar="RUTA",
                        help="escribe la configuracion actual y sale")
    parser.add_argument("--demo", action="store_true",
                        help="seniales simuladas, sin hardware")
    parser.add_argument("--windowed", action="store_true",
                        help="en ventana en vez de pantalla completa")
    parser.add_argument("--backend", metavar="URL",
                        help="URL del backend (ej: http://192.168.0.50:8000)")
    parser.add_argument("--no-backend", action="store_true",
                        help="no manda nada por red")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--continuo", action="store_true",
                        help="mide siempre, sin esperar la tecla")
    parser.add_argument("--duracion", type=float, metavar="SEG",
                        help="segundos de cada medicion (default 10)")
    parser.add_argument("--medir-al-inicio", action="store_true",
                        help="dispara una medicion apenas arranca, sin esperar la tecla")
    parser.add_argument("--sin-paneles", action="store_true",
                        help="sin pantallas de espera ni de resultado: la vista "
                             "queda siempre igual y la tecla solo controla la medicion")
    parser.add_argument("--notch", type=float, metavar="HZ",
                        help="frecuencia de red para el notch (50 o 60)")
    parser.add_argument("--captura", metavar="ARCHIVO.png",
                        help="guarda una captura de pantalla y sale (para el informe)")
    parser.add_argument("--segundos", type=float, default=12.0,
                        help="cuanto esperar antes de la captura (default 12)")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.demo:
        cfg.demo = True
    if args.windowed:
        cfg.ui.fullscreen = False
    if args.backend:
        cfg.backend.url = args.backend
        cfg.backend.enabled = True
    if args.no_backend:
        cfg.backend.enabled = False
    if args.no_sound:
        cfg.ui.sound_enabled = False
    if args.notch:
        cfg.ecg.notch_hz = args.notch
    if args.continuo:
        cfg.session.manual = False
    if args.duracion:
        cfg.session.duration_s = args.duracion
    if args.sin_paneles:
        cfg.session.show_overlays = False
    return cfg


def main() -> int:
    args = parse_args()
    cfg = build_config(args)

    if args.save_config:
        cfg.save(args.save_config)
        print(f"Configuracion escrita en {args.save_config}")
        return 0

    pygame.init()
    sound = SoundEngine(
        enabled=cfg.ui.sound_enabled,
        buzzer_pin=cfg.ui.buzzer_pin,
        buzzer_tone_hz=cfg.ui.buzzer_tone_hz,
        output=cfg.ui.sound_output,
    )
    alarms = AlarmManager(cfg.alarms)
    ui = MonitorUI(cfg, alarms, sound)

    # -- adquisicion -------------------------------------------------------
    acquisition = AcquisitionManager(cfg)
    acquisition.start()
    ui.demo_source = acquisition
    for message in acquisition.errors:
        print(f"[aviso] {message}")

    ecg_fs = cfg.ecg.sample_rate_hz
    ppg_fs = acquisition.ppg.fs
    volts_per_count = cfg.ecg.pga_volts / 32768.0
    # Cuentas del ADS -> mV en el electrodo (dividiendo por la ganancia del AD8232).
    # Es una conversion nominal: la ganancia real del modulo no esta calibrada.
    counts_to_mv = volts_per_count * 1000.0 / cfg.ecg.frontend_gain

    def make_processors():
        """Procesadores nuevos y limpios.

        Se recrean en cada medicion en vez de resetearlos campo por campo:
        entre los filtros, los detectores y los buffers hay decenas de
        variables de estado, y olvidarse una sola arrastra el latido anterior
        a la medicion siguiente.
        """
        return (
            EcgProcessor(
                fs=ecg_fs,
                highpass_hz=cfg.ecg.highpass_hz,
                lowpass_hz=cfg.ecg.lowpass_hz,
                notch_hz=cfg.ecg.notch_hz,
                notch_q=cfg.ecg.notch_q,
                counts_to_mv=counts_to_mv,
                volts_per_count=volts_per_count,
                supply_volts=cfg.ecg.supply_volts,
            ),
            PpgProcessor(
                fs=ppg_fs,
                highpass_hz=cfg.ppg.highpass_hz,
                lowpass_hz=cfg.ppg.lowpass_hz,
                spo2_window_s=cfg.ppg.spo2_window_s,
                finger_threshold=cfg.ppg.finger_threshold,
                resp_fs=cfg.resp.fs_hz,
                resp_low_hz=cfg.resp.highpass_hz,
                resp_high_hz=cfg.resp.lowpass_hz,
            ),
        )

    ecg_proc, ppg_proc = make_processors()

    # -- red ---------------------------------------------------------------
    publisher = Publisher(cfg)
    publisher.register_channel(
        "ecg", "mV", ecg_fs, scale=0.001,
        decimation=cfg.backend.ecg_send_decimation,
        note=f"AD8232 -> ADS1115. mV nominales con ganancia {cfg.ecg.frontend_gain:g}, sin calibrar",
    )
    publisher.register_channel(
        "pleth", "raw", ppg_fs, scale=1.0,
        note="MAX30102, canal infrarrojo filtrado. Cuentas del ADC, sin unidad fisica",
    )
    if cfg.resp.enabled:
        publisher.register_channel(
            "resp", "raw", ppg_fs / max(1, round(ppg_fs / cfg.resp.fs_hz)), scale=0.1,
            note="estimada de como la respiracion mueve la linea de base del pleth",
        )
    publisher.start()
    publisher.session_start()

    if cfg.backend.enabled:
        print(f"[red] mandando JSON a {publisher.endpoint}")
    else:
        print("[red] envio deshabilitado")

    # -- medicion a demanda ------------------------------------------------
    measurement = MeasurementSession(cfg, acquisition)
    ui.session = measurement if cfg.session.manual else None

    def on_measurement_start() -> None:
        nonlocal ecg_proc, ppg_proc, snapshot, last_temp
        ecg_proc, ppg_proc = make_processors()
        snapshot = VitalsSnapshot()
        last_temp = None
        ui.clear_traces()
        alarms.clear()

    def on_measurement_finish(summary) -> None:
        publisher.measurement(summary, measurement.measurements)
        alarms.clear()
        estado = "cancelada" if summary.aborted else "lista"
        print(f"[medicion #{measurement.measurements}] {estado} - "
              f"FC {_fmt(summary.hr.mean, 0)}  SpO2 {_fmt(summary.spo2.mean, 1)}  "
              f"PR {_fmt(summary.pr.mean, 0)}  latidos {summary.beats}")
        for problem in summary.problems:
            print(f"    aviso: {problem}")

    measurement.on_start = on_measurement_start
    # Al abrir la ventana se limpian las trazas: asi lo que se ve en pantalla
    # es exactamente lo que se esta midiendo, sin el transitorio previo.
    measurement.on_window_start = ui.clear_traces
    measurement.on_finish = on_measurement_finish

    if cfg.session.manual:
        print(f"[medicion] modo manual: apreta {cfg.session.key.upper()} para medir "
              f"{cfg.session.duration_s:.0f} s "
              f"(mas {cfg.session.warmup_s:.0f} s de estabilizacion)")
    else:
        print("[medicion] modo continuo")

    if args.medir_al_inicio and cfg.session.manual:
        measurement.start()

    started_at = time.time()
    snapshot = VitalsSnapshot()
    last_temp = None
    exit_reason = "shutdown"

    try:
        while ui.handle_events():
            # Con los sensores apagados no hay nada que procesar: los numeros
            # quedan congelados en lo ultimo que se midio.
            acquiring = measurement.acquiring
            # Durante la estabilizacion las muestras alimentan los filtros,
            # pero no se dibujan ni se mandan al backend.
            recording = measurement.recording

            if acquiring:
                # ---- ECG ----
                for chunk in acquisition.drain_ecg():
                    leads_off = chunk.lo_plus or chunk.lo_minus
                    ecg_proc.set_leads_off(leads_off)
                    ui.set_leads_off(leads_off)
                    snapshot.ecg_lo_plus = chunk.lo_plus
                    snapshot.ecg_lo_minus = chunk.lo_minus
                    snapshot.ecg_leads_off = leads_off

                    millivolts, beats = ecg_proc.process(chunk.values)
                    if recording:
                        ui.push_ecg(millivolts)
                        publisher.add_samples("ecg", millivolts, chunk.t0)
                        for _ in range(beats):
                            ui.on_beat(ppg_proc.spo2)
                            measurement.note_beat()

                # ---- PPG ----
                for chunk in acquisition.drain_ppg():
                    pleth, resp, pulses = ppg_proc.process(chunk.red, chunk.ir)
                    if recording:
                        ui.push_pleth(pleth)
                        ui.push_resp(resp)
                        publisher.add_samples("pleth", pleth, chunk.t0)
                        if resp:
                            publisher.add_samples("resp", resp, chunk.t0)
                        if pulses:
                            ui.on_pulse()
                    ui.set_finger_off(not ppg_proc.finger_detected)

                ecg_proc.tick()
                ppg_proc.tick()
                if acquisition.ppg.die_temp_c is not None:
                    last_temp = acquisition.ppg.die_temp_c

                # ---- signos vitales ----
                snapshot.hr_bpm = ecg_proc.hr_bpm
                snapshot.pr_bpm = ppg_proc.pr_bpm
                snapshot.spo2_pct = ppg_proc.spo2
                snapshot.perfusion_index = ppg_proc.perfusion_index
                snapshot.resp_rpm = ppg_proc.resp_rpm
                snapshot.rr_last_ms = ecg_proc.last_rr_ms
                snapshot.hrv_rmssd_ms = ecg_proc.rmssd_ms
                snapshot.ecg_noise = ecg_proc.noise_level
                snapshot.ecg_saturated = ecg_proc.saturated
                snapshot.finger_detected = ppg_proc.finger_detected
                snapshot.spo2_out_of_range = ppg_proc.out_of_range
                # Diagnostico del equipo, no del paciente
                snapshot.sensor_die_temp_c = last_temp
                snapshot.ecg_baseline_v = ecg_proc.baseline_v
                snapshot.ir_dc = ppg_proc.ir_dc_value or None
                snapshot.red_dc = ppg_proc.red_dc_value or None
                snapshot.seconds_since_beat = ecg_proc.seconds_since_beat()
                snapshot.ts = time.time()

            # ---- estado del equipo, siempre ----
            snapshot.ecg_active = acquisition.ecg_ready
            snapshot.ppg_active = acquisition.ppg_ready
            snapshot.backend_enabled = cfg.backend.enabled
            snapshot.backend_ok = publisher.status.connected
            snapshot.backend_pending = publisher.status.pending_batches
            snapshot.uptime_s = time.time() - started_at

            if recording:
                alarms.evaluate(snapshot)
                if alarms.should_sound():
                    sound.alarm(alarms.highest_level)
                publisher.tick(snapshot, alarms)

            measurement.update(snapshot)
            ui.render(snapshot)

            if args.captura and snapshot.uptime_s >= args.segundos:
                pygame.image.save(ui.screen, args.captura)
                print(f"Captura guardada en {args.captura}")
                exit_reason = "captura"
                break

    except KeyboardInterrupt:
        exit_reason = "interrupt"
    finally:
        print("\nCerrando...")
        publisher.session_end(exit_reason)
        publisher.stop()
        acquisition.stop()
        ui.close()
        sound.close()
        pygame.quit()

    status = publisher.status
    if cfg.backend.enabled:
        print(f"[red] lotes enviados: {status.sent_ok}  fallidos: {status.failed}  "
              f"descartados: {status.dropped}")
        if status.last_error:
            print(f"[red] ultimo error: {status.last_error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
