"""Pantalla del monitor: barrido de ondas, panel numerico y alarmas.

El barrido usa la tecnica clasica de los monitores de cabecera: la traza se
dibuja de izquierda a derecha sobre una superficie propia y una "barra de
borrado" limpia lo que hay adelante del cursor. Asi solo se redibujan los
pixeles nuevos en vez de la onda entera en cada frame.
"""

from __future__ import annotations

import time
from collections import deque

import pygame

import session as session_mod
from config import Config
from . import theme
from .theme import (
    ALARM_HIGH, ALARM_MEDIUM, BG, ECG, HEADER_BG, LEVEL_COLORS, OK, PANEL_BG,
    PANEL_BORDER, PLETH, RESP, TEXT, TEXT_DIM, TEXT_FAINT, Fonts, blit_text,
    draw_heart, make_grid, make_plain_background,
)

# Velocidades de barrido en mm/s, como en el papel de ECG
SWEEP_SPEEDS = {1: 12.5, 2: 25.0, 3: 50.0}


class SweepTrace:
    """Un carril de onda con grilla, autoescala y barra de borrado."""

    def __init__(self, rect: pygame.Rect, fs: float, sweep_seconds: float,
                 color, label: str, sublabel: str = "",
                 with_grid: bool = True, thickness: int = 2,
                 baseline_ratio: float = 0.5, autoscale: bool = True,
                 min_span: float = 1e-6):
        self.rect = rect
        self.fs = max(1.0, fs)
        self.color = color
        self.label = label
        self.sublabel = sublabel
        self.thickness = thickness
        self.baseline_ratio = baseline_ratio
        self.autoscale = autoscale
        self.min_span = min_span
        self.with_grid = with_grid
        self.gain_multiplier = 1.0
        self.flatline = False

        self.gain = 1.0
        self.baseline = 0.0
        self._history: deque[float] = deque(maxlen=int(self.fs * 4))
        self._rescale_at = 0.0

        self.cursor = 0.0
        self._last_x = 0
        self._last_y: int | None = None

        self.set_sweep_seconds(sweep_seconds)

    # -- geometria ---------------------------------------------------------

    def set_sweep_seconds(self, sweep_seconds: float) -> None:
        self.sweep_seconds = max(1.0, sweep_seconds)
        width, height = self.rect.width, self.rect.height
        self.px_per_second = width / self.sweep_seconds
        self.px_per_sample = self.px_per_second / self.fs
        self.erase_width = max(6, int(width * 0.018))
        if self.with_grid:
            self.background = make_grid(width, height, self.px_per_second)
        else:
            self.background = make_plain_background(width, height)
        self.surface = self.background.copy()
        self.cursor = 0.0
        self._last_x = 0
        self._last_y = None

    @property
    def center_y(self) -> int:
        return int(self.rect.height * self.baseline_ratio)

    # -- escala ------------------------------------------------------------

    def _update_scale(self, values: list[float]) -> None:
        self._history.extend(values)
        now = time.monotonic()
        if now < self._rescale_at or len(self._history) < 8:
            return
        self._rescale_at = now + 0.25

        data = self._history
        low = min(data)
        high = max(data)
        span = max(high - low, self.min_span)

        if self.autoscale:
            usable = self.rect.height * 0.78
            target = usable / span
            # Suavizado: que la ganancia no salte de un frame al otro
            self.gain += (target - self.gain) * 0.25
            mid = (high + low) / 2.0
            self.baseline += (mid - self.baseline) * 0.15

    # -- dibujo ------------------------------------------------------------

    def push(self, values: list[float]) -> None:
        if not values:
            return
        self._update_scale(values)

        height = self.rect.height
        width = self.rect.width
        center = self.center_y
        gain = self.gain * self.gain_multiplier
        top_limit = 2
        bottom_limit = height - 3

        for value in values:
            if self.flatline:
                y = center
            else:
                y = int(center - (value - self.baseline) * gain)
                if y < top_limit:
                    y = top_limit
                elif y > bottom_limit:
                    y = bottom_limit

            previous_x = self._last_x
            self.cursor += self.px_per_sample
            wrapped = False
            if self.cursor >= width:
                self.cursor -= width
                wrapped = True
            x = int(self.cursor)

            if wrapped or self._last_y is None:
                self._last_x, self._last_y = x, y
                self._erase_ahead(x)
                continue

            if x == previous_x:
                # Varias muestras caen en la misma columna: trazo vertical
                pygame.draw.line(self.surface, self.color,
                                 (x, self._last_y), (x, y), self.thickness)
            else:
                pygame.draw.line(self.surface, self.color,
                                 (previous_x, self._last_y), (x, y), self.thickness)
                self._erase_ahead(x)

            self._last_x, self._last_y = x, y

    def _erase_ahead(self, x: int) -> None:
        width = self.rect.width
        start = x + self.thickness
        if start >= width:
            start -= width
        span = self.erase_width
        if start + span <= width:
            area = pygame.Rect(start, 0, span, self.rect.height)
            self.surface.blit(self.background, area.topleft, area)
        else:
            first = width - start
            area = pygame.Rect(start, 0, first, self.rect.height)
            self.surface.blit(self.background, area.topleft, area)
            area = pygame.Rect(0, 0, span - first, self.rect.height)
            self.surface.blit(self.background, area.topleft, area)

    def clear(self) -> None:
        self.surface = self.background.copy()
        self._last_y = None
        self._history.clear()

    def blit(self, screen: pygame.Surface) -> None:
        screen.blit(self.surface, self.rect.topleft)


class MonitorUI:
    def __init__(self, cfg: Config, alarm_manager, sound):
        self.cfg = cfg
        self.alarms = alarm_manager
        self.sound = sound

        pygame.display.set_caption("Monitor de signos vitales")
        flags = pygame.FULLSCREEN if cfg.ui.fullscreen else 0
        size = cfg.ui.window_size if cfg.ui.window_size != (0, 0) else (0, 0)
        if not cfg.ui.fullscreen and size == (0, 0):
            size = (1280, 720)
        self.screen = pygame.display.set_mode(size, flags | pygame.DOUBLEBUF)
        self.width, self.height = self.screen.get_size()
        if cfg.ui.hide_cursor:
            pygame.mouse.set_visible(False)

        self.fonts = Fonts(scale=min(self.width / 1280.0, self.height / 720.0))
        self.clock = pygame.time.Clock()
        self.sweep_speed_key = 2  # 25 mm/s
        self.show_debug = cfg.ui.show_debug
        self.running = True
        # Se completan desde main.py
        self.demo_source = None   # solo en modo demo
        self.session = None       # MeasurementSession, solo en modo manual

        self._last_beat_flash = 0.0
        self._last_pulse_flash = 0.0
        self._blink_phase = 0.0
        self.fps = 0.0

        self._layout()

    # -- layout ------------------------------------------------------------

    def _layout(self) -> None:
        self.header_h = max(34, int(self.height * 0.065))
        self.footer_h = max(24, int(self.height * 0.042))
        self.sidebar_w = max(200, int(self.width * 0.255))

        waves_x = 0
        waves_y = self.header_h
        waves_w = self.width - self.sidebar_w
        waves_h = self.height - self.header_h - self.footer_h

        lanes = 3 if self.cfg.resp.enabled else 2
        lane_h = waves_h // lanes
        pad = max(2, int(self.height * 0.004))
        inner_w = waves_w - pad * 2

        ecg_fs = self.cfg.ecg.sample_rate_hz
        ppg_fs = self.cfg.ppg.sample_rate_hz / self.cfg.ppg.averaging
        sweep = self.cfg.ui.sweep_seconds

        self.ecg_trace = SweepTrace(
            pygame.Rect(waves_x + pad, waves_y + pad, inner_w, lane_h - pad * 2),
            fs=ecg_fs, sweep_seconds=sweep, color=ECG,
            label=self.cfg.ecg.lead_label, sublabel="", with_grid=True,
            baseline_ratio=0.60, min_span=0.05,
        )
        self.pleth_trace = SweepTrace(
            pygame.Rect(waves_x + pad, waves_y + lane_h + pad, inner_w, lane_h - pad * 2),
            fs=ppg_fs, sweep_seconds=sweep, color=PLETH,
            label="PLETH", sublabel="MAX30102 · infrarrojo", with_grid=False,
            baseline_ratio=0.55, min_span=50.0,
        )
        self.traces = [self.ecg_trace, self.pleth_trace]

        if self.cfg.resp.enabled:
            self.resp_trace = SweepTrace(
                pygame.Rect(waves_x + pad, waves_y + lane_h * 2 + pad,
                            inner_w, lane_h - pad * 2),
                fs=self.cfg.resp.fs_hz, sweep_seconds=sweep * 2, color=RESP,
                label="RESP", sublabel="estimada del pleth · no medida",
                with_grid=False, baseline_ratio=0.5, min_span=20.0,
            )
            self.traces.append(self.resp_trace)
        else:
            self.resp_trace = None

        # Panel numerico: una caja por signo vital
        boxes = 4
        box_h = (self.height - self.header_h - self.footer_h) // boxes
        self.boxes = []
        for i in range(boxes):
            self.boxes.append(pygame.Rect(
                self.width - self.sidebar_w + 2,
                self.header_h + i * box_h + 2,
                self.sidebar_w - 4, box_h - 4,
            ))

    def set_sweep_speed(self, key: int) -> None:
        if key not in SWEEP_SPEEDS:
            return
        self.sweep_speed_key = key
        # 25 mm/s es la referencia; el ancho de pantalla se reparte proporcional
        factor = 25.0 / SWEEP_SPEEDS[key]
        base = self.cfg.ui.sweep_seconds * factor
        self.ecg_trace.set_sweep_seconds(base)
        self.pleth_trace.set_sweep_seconds(base)
        if self.resp_trace is not None:
            self.resp_trace.set_sweep_seconds(base * 2)

    # -- entrada -----------------------------------------------------------

    def handle_events(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.USEREVENT + 1:
                self.sound.repeat_high()
            elif event.type == pygame.KEYDOWN:
                self._on_key(event)
        return self.running

    def _on_key(self, event) -> None:
        key = event.key
        session_key = self.cfg.session.key.lower()
        if self.session is not None and event.unicode.lower() == session_key:
            self.session.trigger()
            return
        if key in (pygame.K_ESCAPE, pygame.K_q):
            self.running = False
        elif key == pygame.K_m:
            self.alarms.toggle_mute()
        elif key == pygame.K_s:
            self.sound.toggle()
        elif key == pygame.K_d:
            self.show_debug = not self.show_debug
        elif key == pygame.K_f:
            pygame.display.toggle_fullscreen()
        elif key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            self.ecg_trace.gain_multiplier = min(4.0, self.ecg_trace.gain_multiplier * 1.25)
        elif key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.ecg_trace.gain_multiplier = max(0.25, self.ecg_trace.gain_multiplier / 1.25)
        elif key in (pygame.K_1, pygame.K_2, pygame.K_3):
            self.set_sweep_speed({pygame.K_1: 1, pygame.K_2: 2, pygame.K_3: 3}[key])
        elif key == pygame.K_c:
            for trace in self.traces:
                trace.clear()
        elif self.demo_source is not None and self.cfg.demo:
            self._on_demo_key(key)

    def _on_demo_key(self, key: int) -> None:
        """Teclas F1..F6: mueven la senial simulada para probar las alarmas."""
        source = self.demo_source
        if key == pygame.K_F1:
            source.demo_shift_spo2(-2)
        elif key == pygame.K_F2:
            source.demo_shift_spo2(+2)
        elif key == pygame.K_F3:
            source.demo_shift_hr(-5)
        elif key == pygame.K_F4:
            source.demo_shift_hr(+5)
        elif key == pygame.K_F5:
            source.demo_toggle_finger()
        elif key == pygame.K_F6:
            source.demo_toggle_leads()

    # -- datos -------------------------------------------------------------

    def push_ecg(self, values: list[float]) -> None:
        self.ecg_trace.push(values)

    def push_pleth(self, values: list[float]) -> None:
        self.pleth_trace.push(values)

    def push_resp(self, values: list[float]) -> None:
        if self.resp_trace is not None:
            self.resp_trace.push(values)

    def on_beat(self, spo2: float | None) -> None:
        self._last_beat_flash = time.monotonic()
        if self.cfg.ui.beat_beep:
            self.sound.beat(spo2)

    def on_pulse(self) -> None:
        self._last_pulse_flash = time.monotonic()

    def set_leads_off(self, off: bool) -> None:
        self.ecg_trace.flatline = off

    def set_finger_off(self, off: bool) -> None:
        self.pleth_trace.flatline = off
        if self.resp_trace is not None:
            self.resp_trace.flatline = off

    # -- render ------------------------------------------------------------

    def clear_traces(self) -> None:
        for trace in self.traces:
            trace.clear()

    @property
    def acquiring(self) -> bool:
        """True si los sensores estan leyendo. Sin sesion manual, siempre."""
        return self.session is None or self.session.acquiring

    @property
    def waves_rect(self) -> pygame.Rect:
        return pygame.Rect(0, self.header_h, self.width - self.sidebar_w,
                           self.height - self.header_h - self.footer_h)

    def render(self, snapshot) -> None:
        self._blink_phase = (time.monotonic() * 2.0) % 2.0
        self.screen.fill(BG)
        self._draw_header(snapshot)
        for trace in self.traces:
            trace.blit(self.screen)
            self._draw_trace_label(trace)
        self._draw_sidebar(snapshot)
        if self.session is not None:
            self._draw_session(snapshot)
        self._draw_footer(snapshot)
        if self.show_debug:
            self._draw_debug(snapshot)
        pygame.display.flip()
        self.clock.tick(self.cfg.ui.fps)
        self.fps = self.clock.get_fps()

    def _draw_header(self, snapshot) -> None:
        rect = pygame.Rect(0, 0, self.width, self.header_h)
        pygame.draw.rect(self.screen, HEADER_BG, rect)
        pygame.draw.line(self.screen, PANEL_BORDER, (0, self.header_h),
                         (self.width, self.header_h))

        pad = int(self.width * 0.01)
        name_font = self.fonts.sans(24, bold=True)
        blit_text(self.screen, name_font, self.cfg.device.patient_name, TEXT,
                  (pad, int(self.header_h * 0.18)))
        small = self.fonts.sans(15)
        blit_text(self.screen, small,
                  f"{self.cfg.device.bed}   ID {self.cfg.device.patient_id}",
                  TEXT_DIM, (pad, int(self.header_h * 0.62)))

        # Banda de alarmas al centro
        alarms = self.alarms.sorted_alarms()
        center_x = self.width // 2
        if alarms:
            top = alarms[0]
            color = LEVEL_COLORS.get(top.level, TEXT)
            blink = top.level in ("high", "medium") and not self.alarms.muted
            if not blink or self._blink_phase < 1.0:
                banner = pygame.Rect(center_x - int(self.width * 0.22), 3,
                                     int(self.width * 0.44), self.header_h - 8)
                pygame.draw.rect(self.screen, color, banner, border_radius=4)
                text_color = (10, 10, 10) if top.level != "technical" else TEXT
                label = top.message
                if len(alarms) > 1:
                    label += f"   (+{len(alarms) - 1})"
                blit_text(self.screen, self.fonts.sans(22, bold=True), label,
                          text_color, banner.center, align="middle")
        if self.alarms.muted:
            remaining = int(self.alarms.mute_remaining_s)
            blit_text(self.screen, self.fonts.sans(14, bold=True),
                      f"ALARMAS SILENCIADAS {remaining // 60}:{remaining % 60:02d}",
                      ALARM_MEDIUM, (center_x, self.header_h - 18), align="center")

        # Reloj a la derecha
        now = time.localtime()
        blit_text(self.screen, self.fonts.sans(26, bold=True),
                  time.strftime("%H:%M:%S", now), TEXT,
                  (self.width - pad, int(self.header_h * 0.14)), align="right")
        blit_text(self.screen, small, time.strftime("%d/%m/%Y", now), TEXT_DIM,
                  (self.width - pad, int(self.header_h * 0.64)), align="right")

    def _draw_trace_label(self, trace: SweepTrace) -> None:
        x = trace.rect.left + 8
        y = trace.rect.top + 4
        blit_text(self.screen, self.fonts.sans(16, bold=True), trace.label,
                  trace.color, (x, y))
        detail = trace.sublabel
        if trace is self.ecg_trace:
            detail = f"{SWEEP_SPEEDS[self.sweep_speed_key]:g} mm/s   x{trace.gain_multiplier:.2f}"
        if detail:
            blit_text(self.screen, self.fonts.sans(13), detail, TEXT_FAINT,
                      (x + self.fonts.sans(16, bold=True).size(trace.label)[0] + 12, y + 2))
        pygame.draw.rect(self.screen, PANEL_BORDER, trace.rect, 1)

    def _draw_sidebar(self, snapshot) -> None:
        self._draw_hr_box(self.boxes[0], snapshot)
        self._draw_spo2_box(self.boxes[1], snapshot)
        self._draw_pr_box(self.boxes[2], snapshot)
        self._draw_resp_box(self.boxes[3], snapshot)

    def _box_frame(self, rect: pygame.Rect, label: str, unit: str, color,
                   limits: str | None = None, source: str | None = None) -> None:
        pygame.draw.rect(self.screen, PANEL_BG, rect, border_radius=5)
        pygame.draw.rect(self.screen, PANEL_BORDER, rect, 1, border_radius=5)
        blit_text(self.screen, self.fonts.sans(19, bold=True), label, color,
                  (rect.left + 10, rect.top + 7))
        blit_text(self.screen, self.fonts.sans(13), unit, TEXT_FAINT,
                  (rect.left + 10, rect.top + 31))
        if limits:
            blit_text(self.screen, self.fonts.sans(13), limits, TEXT_FAINT,
                      (rect.right - 10, rect.top + 9), align="right")
        if source:
            # De que modulo sale este numero. Que este a la vista evita que
            # alguien lea la pantalla y le atribuya al equipo algo que no mide.
            blit_text(self.screen, self.fonts.sans(11), source, TEXT_FAINT,
                      (rect.right - 10, rect.bottom - 17), align="right")

    def _big_number(self, rect: pygame.Rect, value, color, size: int = 76,
                    decimals: int = 0, prefix: str = "") -> None:
        if value is None:
            text = "- - -"
            color = TEXT_FAINT
        elif decimals:
            text = f"{prefix}{value:.{decimals}f}"
        else:
            text = f"{prefix}{value:.0f}"
        font = self.fonts.digits(size)
        blit_text(self.screen, font, text, color,
                  (rect.right - 14, rect.centery + 4), align="midright")

    def _draw_hr_box(self, rect: pygame.Rect, snapshot) -> None:
        limits = self.cfg.alarms
        alarming = "HR_LOW" in self.alarms.active or "HR_HIGH" in self.alarms.active \
            or "NO_BEATS" in self.alarms.active
        color = ALARM_HIGH if (alarming and self._blink_phase < 1.0) else ECG
        self._box_frame(rect, "FC", "lpm", color,
                        f"{limits.hr_low:.0f} - {limits.hr_high:.0f}",
                        source="AD8232 → ADS1115")
        self._big_number(rect, snapshot.hr_bpm, color)

        # Corazon que late
        if time.monotonic() - self._last_beat_flash < 0.18:
            draw_heart(self.screen, (rect.left + 26, rect.bottom - 30), 22, color)

        detail = []
        if snapshot.rr_last_ms is not None:
            detail.append(f"RR {snapshot.rr_last_ms:.0f} ms")
        if snapshot.hrv_rmssd_ms is not None:
            detail.append(f"RMSSD {snapshot.hrv_rmssd_ms:.0f}")
        if detail:
            blit_text(self.screen, self.fonts.sans(13), "   ".join(detail), TEXT_DIM,
                      (rect.left + 48, rect.bottom - 34))

    def _draw_spo2_box(self, rect: pygame.Rect, snapshot) -> None:
        limits = self.cfg.alarms
        alarming = "SPO2_LOW" in self.alarms.active
        color = ALARM_HIGH if (alarming and self._blink_phase < 1.0) else PLETH
        self._box_frame(rect, "SpO2", "%", color, f">= {limits.spo2_low:.0f}",
                        source="MAX30102")
        self._big_number(rect, snapshot.spo2_pct, color)

        if not self.acquiring:
            # Con el sensor apagado no se puede decir que "no hay dedo": lo que
            # se ve es el ultimo valor medido, congelado.
            label = "ultima medicion" if snapshot.spo2_pct is not None \
                else "sensor apagado"
            blit_text(self.screen, self.fonts.sans(14), label, TEXT_FAINT,
                      (rect.left + 10, rect.bottom - 30))
        elif not snapshot.finger_detected:
            blit_text(self.screen, self.fonts.sans(14, bold=True), "SIN DEDO",
                      ALARM_MEDIUM, (rect.left + 10, rect.bottom - 30))
        elif snapshot.perfusion_index is not None:
            blit_text(self.screen, self.fonts.sans(14),
                      f"PI {snapshot.perfusion_index:.1f} %", TEXT_DIM,
                      (rect.left + 10, rect.bottom - 30))

    def _draw_pr_box(self, rect: pygame.Rect, snapshot) -> None:
        """Frecuencia de pulso: el latido que de verdad llega al dedo."""
        self._box_frame(rect, "PR", "lpm", PLETH, source="MAX30102 · pleth")
        self._big_number(rect, snapshot.pr_bpm, PLETH, size=64)

        deficit = snapshot.pulse_deficit
        if deficit is not None:
            # Que FC y PR se separen no es un error de medicion: son dos cosas
            # distintas (actividad electrica contra pulso periferico).
            color = ALARM_MEDIUM if abs(deficit) >= 8 else TEXT_DIM
            blit_text(self.screen, self.fonts.sans(13),
                      f"FC - PR = {deficit:+d}", color,
                      (rect.left + 10, rect.bottom - 30))

    def _draw_resp_box(self, rect: pygame.Rect, snapshot) -> None:
        limits = self.cfg.alarms
        alarming = "RESP_LOW" in self.alarms.active or "RESP_HIGH" in self.alarms.active
        color = ALARM_MEDIUM if (alarming and self._blink_phase < 1.0) else RESP
        self._box_frame(rect, "RESP", "rpm", color,
                        f"{limits.resp_low:.0f} - {limits.resp_high:.0f}",
                        source="estimada del pleth")
        # El "~" delante del numero avisa que esto es una estimacion, no una
        # medicion directa: no hay sensor de flujo ni de impedancia toracica.
        self._big_number(rect, snapshot.resp_rpm, color, size=60, prefix="~")
        blit_text(self.screen, self.fonts.sans(12, bold=True), "ESTIMADA", TEXT_FAINT,
                  (rect.left + 10, rect.bottom - 30))

    def _draw_footer(self, snapshot) -> None:
        top = self.height - self.footer_h
        rect = pygame.Rect(0, top, self.width, self.footer_h)
        pygame.draw.rect(self.screen, HEADER_BG, rect)
        pygame.draw.line(self.screen, PANEL_BORDER, (0, top), (self.width, top))

        pad = int(self.width * 0.01)
        y = top + self.footer_h // 2
        font = self.fonts.sans(14)

        if not snapshot.backend_enabled:
            net_text, net_color = "BACKEND OFF", TEXT_FAINT
        elif snapshot.backend_ok:
            net_text, net_color = "SERVIDOR OK", OK
        else:
            net_text, net_color = "SIN SERVIDOR", ALARM_MEDIUM
        dot_x = pad + 6
        pygame.draw.circle(self.screen, net_color, (dot_x, y), 5)
        rect_text = blit_text(self.screen, font, net_text, net_color,
                              (dot_x + 12, y), align="midleft")

        parts = []
        if snapshot.backend_enabled:
            parts.append(f"cola {snapshot.backend_pending}")
        if self.cfg.demo:
            parts.append("MODO DEMO (senial simulada)")
        if not snapshot.ecg_active:
            parts.append("ECG sin hardware")
        if not snapshot.ppg_active:
            parts.append("SpO2 sin hardware")
        if parts:
            blit_text(self.screen, font, "   |   ".join(parts), TEXT_DIM,
                      (rect_text.right + 24, y), align="midleft")

        hints = "M silenciar   S sonido   1/2/3 velocidad   +/- ganancia   D debug   ESC salir"
        if self.session is not None:
            hints = f"{self._key_hint()} medir   " + hints
        if self.cfg.demo:
            hints = "F1/F2 SpO2   F3/F4 FC   F5 dedo   F6 electrodos   |   " + hints
        blit_text(self.screen, self.fonts.sans(13), hints, TEXT_FAINT,
                  (self.width - pad, y), align="midright")

    def _draw_debug(self, snapshot) -> None:
        """Diagnostico del equipo. Nada de esto es un signo vital del paciente."""
        lines = [
            "-- equipo (no es del paciente) --",
            f"temp del die MAX30102  {_fmt(snapshot.sensor_die_temp_c, 1, 'C')}",
            f"base AD8232            {_fmt(snapshot.ecg_baseline_v, 3, 'V')}",
            f"  (deberia dar ~{self.cfg.ecg.supply_volts / 2:.2f} V)",
            f"saturacion ECG         {'SI' if snapshot.ecg_saturated else 'no'}",
            f"DC infrarrojo          {_fmt(snapshot.ir_dc, 0)}",
            f"DC rojo                {_fmt(snapshot.red_dc, 0)}",
            f"ruido ECG              {snapshot.ecg_noise:.3f}",
            "-- render y red --",
            f"fps {self.fps:5.1f}   cola de red {snapshot.backend_pending}",
            f"ganancia ECG   {self.ecg_trace.gain:9.3f}",
            f"ganancia pleth {self.pleth_trace.gain:9.5f}",
            f"ultimo latido hace {snapshot.seconds_since_beat:.1f} s",
        ]
        font = self.fonts.mono(13)
        x = self.width - self.sidebar_w - 320
        y = self.header_h + 8
        box = pygame.Rect(x - 8, y - 6, 310, len(lines) * 18 + 12)
        overlay = pygame.Surface(box.size, pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        self.screen.blit(overlay, box.topleft)
        for line in lines:
            blit_text(self.screen, font, line, TEXT_DIM, (x, y))
            y += 18

    # -- medicion a demanda ------------------------------------------------

    def _draw_session(self, snapshot) -> None:
        state = self.session.state
        # Sin paneles, la vista queda siempre igual y el estado se resume en un
        # cartelito arriba, que no tapa las ondas.
        compacto = not self.cfg.session.show_overlays

        if state == session_mod.IDLE:
            if compacto:
                self._draw_status_badge(
                    f"EN ESPERA  ·  {self._key_hint()} PARA MEDIR", TEXT_DIM)
            else:
                self._draw_idle_screen()
        elif state == session_mod.WARMUP:
            self._draw_progress_badge("ESTABILIZANDO SENSORES", TEXT_DIM)
        elif state == session_mod.MEASURING:
            self._draw_progress_badge("MIDIENDO", OK, countdown=True)
        elif state == session_mod.RESULT:
            if compacto:
                self._draw_result_badge()
            else:
                self._draw_result_screen()

    def _badge_rect(self, height: int, ancho: float = 0.34) -> pygame.Rect:
        rect = self.waves_rect
        badge = pygame.Rect(0, 0, int(rect.width * ancho), height)
        badge.midtop = (rect.centerx, rect.top + 10)
        return badge

    def _badge_bg(self, badge: pygame.Rect, color) -> None:
        overlay = pygame.Surface(badge.size, pygame.SRCALPHA)
        overlay.fill((3, 6, 12, 215))
        self.screen.blit(overlay, badge.topleft)
        pygame.draw.rect(self.screen, color, badge, 1, border_radius=4)

    def _draw_status_badge(self, texto: str, color) -> None:
        badge = self._badge_rect(34)
        self._badge_bg(badge, color)
        blit_text(self.screen, self.fonts.sans(16, bold=True), texto, color,
                  badge.center, align="middle")

    def _draw_result_badge(self) -> None:
        """Resumen de una linea, para el modo sin paneles."""
        summary = self.session.last_summary
        if summary is None:
            self._draw_status_badge(
                f"{self._key_hint()} PARA MEDIR", TEXT_DIM)
            return

        partes = [f"MEDICION #{self.session.measurements}"]
        if summary.hr.mean is not None:
            partes.append(f"FC {summary.hr.mean:.0f}")
        if summary.spo2.mean is not None:
            partes.append(f"SpO2 {summary.spo2.mean:.0f}")
        if summary.pr.mean is not None:
            partes.append(f"PR {summary.pr.mean:.0f}")

        color = ALARM_MEDIUM if (summary.problems or summary.aborted) else OK
        badge = self._badge_rect(46, ancho=0.42)
        self._badge_bg(badge, color)
        blit_text(self.screen, self.fonts.sans(16, bold=True),
                  "   ·   ".join(partes), color,
                  (badge.centerx, badge.top + 5), align="center")
        detalle = summary.problems[0] if summary.problems \
            else f"{self._key_hint()} para repetir"
        blit_text(self.screen, self.fonts.sans(12), detalle, TEXT_FAINT,
                  (badge.centerx, badge.bottom - 17), align="center")

    def _dim_panel(self, rect: pygame.Rect, alpha: int = 225) -> None:
        overlay = pygame.Surface(rect.size, pygame.SRCALPHA)
        overlay.fill((3, 6, 12, alpha))
        self.screen.blit(overlay, rect.topleft)
        pygame.draw.rect(self.screen, PANEL_BORDER, rect, 1)

    def _key_hint(self) -> str:
        return self.cfg.session.key.upper()

    def _draw_idle_screen(self) -> None:
        rect = self.waves_rect
        self._dim_panel(rect)
        cx = rect.centerx
        y = rect.centery - int(rect.height * 0.16)

        blit_text(self.screen, self.fonts.sans(30, bold=True),
                  "SENSORES EN ESPERA", TEXT_DIM, (cx, y), align="center")

        # La tecla, dibujada como una tecla
        key_font = self.fonts.digits(64)
        label = self._key_hint()
        size = key_font.size(label)
        box = pygame.Rect(0, 0, size[0] + 48, size[1] + 24)
        box.center = (cx, y + int(rect.height * 0.22))
        pygame.draw.rect(self.screen, PANEL_BG, box, border_radius=10)
        pygame.draw.rect(self.screen, OK, box, 2, border_radius=10)
        blit_text(self.screen, key_font, label, OK, box.center, align="middle")

        blit_text(self.screen, self.fonts.sans(22),
                  f"Apreta {label} para encender los modulos y medir "
                  f"{self.cfg.session.duration_s:.0f} segundos",
                  TEXT, (cx, box.bottom + 26), align="center")

        detail = "MAX30102 y ADS1115 apagados" if self.cfg.session.power_down_idle \
            else "modulos encendidos, lectura pausada"
        blit_text(self.screen, self.fonts.sans(15), detail, TEXT_FAINT,
                  (cx, box.bottom + 58), align="center")

        if self.session.measurements:
            blit_text(self.screen, self.fonts.sans(15),
                      f"mediciones en esta sesion: {self.session.measurements}",
                      TEXT_FAINT, (cx, box.bottom + 82), align="center")

    def _draw_progress_badge(self, label: str, color, countdown: bool = False) -> None:
        """Cartel compacto arriba: deja ver las ondas mientras mide."""
        badge = self._badge_rect(54)
        self._badge_bg(badge, color)

        blit_text(self.screen, self.fonts.sans(17, bold=True), label, color,
                  (badge.left + 14, badge.top + 8))
        if countdown:
            blit_text(self.screen, self.fonts.digits(34),
                      f"{self.session.remaining_s:04.1f} s", color,
                      (badge.right - 14, badge.centery), align="midright")
        blit_text(self.screen, self.fonts.sans(12),
                  f"{self._key_hint()} cancela", TEXT_FAINT,
                  (badge.left + 14, badge.bottom - 18))

        # Barra de avance pegada al borde inferior del cartel
        bar = pygame.Rect(badge.left + 1, badge.bottom - 4, badge.width - 2, 3)
        pygame.draw.rect(self.screen, PANEL_BORDER, bar)
        done = pygame.Rect(bar.left, bar.top, int(bar.width * self.session.progress),
                           bar.height)
        pygame.draw.rect(self.screen, color, done)

    def _draw_result_screen(self) -> None:
        summary = self.session.last_summary
        if summary is None:
            self._draw_idle_screen()
            return

        rect = self.waves_rect
        self._dim_panel(rect, alpha=248)
        pad = int(rect.width * 0.05)
        x = rect.left + pad
        y = rect.top + int(rect.height * 0.06)

        title = f"MEDICION #{self.session.measurements}"
        if summary.aborted:
            title += "  (CANCELADA)"
        blit_text(self.screen, self.fonts.sans(26, bold=True), title,
                  ALARM_MEDIUM if summary.aborted else TEXT, (x, y))
        blit_text(self.screen, self.fonts.sans(15),
                  f"{summary.duration_s:.1f} s  ·  {summary.beats} latidos detectados",
                  TEXT_DIM, (rect.right - pad, y + 6), align="right")
        y += 44

        # Encabezados de la tabla
        col = [x, x + int(rect.width * 0.30), x + int(rect.width * 0.46),
               x + int(rect.width * 0.62), x + int(rect.width * 0.78)]
        head = self.fonts.sans(14, bold=True)
        for text, cx in zip(("", "PROMEDIO", "MINIMO", "MAXIMO", "MUESTRAS"), col):
            if text:
                blit_text(self.screen, head, text, TEXT_FAINT, (cx, y))
        y += 26
        pygame.draw.line(self.screen, PANEL_BORDER, (x, y), (rect.right - pad, y))
        y += 12

        rows = [
            ("FC", summary.hr, ECG, 0),
            ("SpO2", summary.spo2, PLETH, 1),
            ("PR", summary.pr, PLETH, 0),
            ("PI", summary.perfusion, PLETH, 2),
            ("RESP", summary.resp, RESP, 0),
        ]
        name_font = self.fonts.sans(20, bold=True)
        value_font = self.fonts.digits(26)
        small = self.fonts.sans(14)

        for name, stat, color, decimals in rows:
            blit_text(self.screen, name_font, name, color, (col[0], y))
            blit_text(self.screen, small, stat.unit, TEXT_FAINT,
                      (col[0] + name_font.size(name)[0] + 8, y + 7))
            if stat.n == 0:
                blit_text(self.screen, small, "sin dato en esta ventana",
                          TEXT_FAINT, (col[1], y + 6))
            else:
                for value, cx in ((stat.mean, col[1]), (stat.minimum, col[2]),
                                  (stat.maximum, col[3])):
                    blit_text(self.screen, value_font, _num(value, decimals),
                              color, (cx, y))
                blit_text(self.screen, small, str(stat.n), TEXT_FAINT, (col[4], y + 6))
            y += 38

        y += 6
        pygame.draw.line(self.screen, PANEL_BORDER, (x, y), (rect.right - pad, y))
        y += 14

        problems = summary.problems
        if problems:
            blit_text(self.screen, self.fonts.sans(15, bold=True),
                      "REVISAR:", ALARM_MEDIUM, (x, y))
            for problem in problems[:3]:
                blit_text(self.screen, self.fonts.sans(15), f"· {problem}",
                          ALARM_MEDIUM, (x + 90, y))
                y += 22
        else:
            blit_text(self.screen, self.fonts.sans(15, bold=True),
                      "Medicion completa, sin problemas de senial", OK, (x, y))
            y += 22

        blit_text(self.screen, self.fonts.sans(17),
                  f"Apreta {self._key_hint()} para medir de nuevo", TEXT,
                  (rect.centerx, rect.bottom - 38), align="center")

    def close(self) -> None:
        pygame.mouse.set_visible(True)


def _num(value: float | None, decimals: int) -> str:
    if value is None:
        return "---"
    return f"{value:.{decimals}f}"


def _fmt(value: float | None, decimals: int, unit: str = "") -> str:
    if value is None:
        return "---"
    suffix = f" {unit}" if unit else ""
    return f"{value:.{decimals}f}{suffix}"
