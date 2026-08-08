"""Tonos del monitor: bip de latido y tonos de alarma.

Los sonidos se sintetizan al vuelo, asi no hay que distribuir archivos wav.
El bip de latido baja de tono cuando cae la saturacion, igual que en un monitor
real: te enteras del desaturo sin mirar la pantalla.
"""

from __future__ import annotations

import math
import time

import pygame

SAMPLE_RATE = 22050


def _tone(freq: float, duration_s: float, volume: float = 0.35,
          attack_s: float = 0.004, release_s: float = 0.03) -> pygame.mixer.Sound:
    """Senoide con envolvente suave (sin envolvente hace 'click')."""
    n = int(SAMPLE_RATE * duration_s)
    attack = max(1, int(SAMPLE_RATE * attack_s))
    release = max(1, int(SAMPLE_RATE * release_s))
    buffer = bytearray()
    two_pi_f = 2.0 * math.pi * freq / SAMPLE_RATE

    for i in range(n):
        if i < attack:
            envelope = i / attack
        elif i > n - release:
            envelope = max(0.0, (n - i) / release)
        else:
            envelope = 1.0
        sample = math.sin(two_pi_f * i) * envelope * volume
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        # stereo, 16 bits, little endian
        buffer += value.to_bytes(2, "little", signed=True) * 2

    return pygame.mixer.Sound(buffer=bytes(buffer))


class SoundEngine:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.available = False
        self._sounds: dict[str, pygame.mixer.Sound] = {}
        self._beat_cache: dict[int, pygame.mixer.Sound] = {}
        self._last_alarm_at = 0.0

        if not enabled:
            return
        try:
            pygame.mixer.pre_init(SAMPLE_RATE, -16, 2, 512)
            pygame.mixer.init()
            self._build()
            self.available = True
        except Exception as exc:  # sin placa de audio, sin ALSA, etc.
            print(f"[sonido] deshabilitado: {exc}")
            self.enabled = False

    def _build(self) -> None:
        # Alarma de alta prioridad: dos pulsos secos y agudos
        self._sounds["high"] = _tone(880, 0.14, volume=0.45)
        self._sounds["medium"] = _tone(660, 0.18, volume=0.32)
        self._sounds["low"] = _tone(520, 0.20, volume=0.22)

    def _beat_sound(self, spo2: float | None) -> pygame.mixer.Sound:
        """Tono del latido segun SpO2: 100% -> agudo, 85% -> grave."""
        if spo2 is None:
            freq = 700
        else:
            clamped = max(85.0, min(100.0, spo2))
            freq = int(500 + (clamped - 85.0) * (350.0 / 15.0))
        key = int(freq / 10) * 10
        sound = self._beat_cache.get(key)
        if sound is None:
            sound = _tone(key, 0.055, volume=0.22, release_s=0.02)
            self._beat_cache[key] = sound
        return sound

    # -- API ---------------------------------------------------------------

    def beat(self, spo2: float | None) -> None:
        if not self.available or not self.enabled:
            return
        try:
            self._beat_sound(spo2).play()
        except pygame.error:
            pass

    def alarm(self, level: str) -> None:
        """Repite el tono de alarma a un ritmo acorde a la prioridad."""
        if not self.available or not self.enabled:
            return
        interval = {"high": 1.0, "medium": 2.5, "low": 8.0}.get(level)
        if interval is None:
            return
        now = time.monotonic()
        if now - self._last_alarm_at < interval:
            return
        self._last_alarm_at = now
        sound = self._sounds.get(level)
        if sound is None:
            return
        try:
            sound.play()
            if level == "high":  # el tono alto va doble
                pygame.time.set_timer(pygame.USEREVENT + 1, 170, loops=1)
        except pygame.error:
            pass

    def repeat_high(self) -> None:
        if self.available and self.enabled:
            try:
                self._sounds["high"].play()
            except pygame.error:
                pass

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        return self.enabled

    def close(self) -> None:
        if self.available:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
