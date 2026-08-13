"""Paleta, tipografias y helpers de dibujo.

Los colores siguen la convencion de los monitores de cabecera: ECG en verde,
pletismografia en cian, respiracion en amarillo, alarmas en rojo/ambar.
"""

from __future__ import annotations

import pygame

# -- colores ----------------------------------------------------------------
BG = (4, 6, 12)
PANEL_BG = (9, 13, 22)
PANEL_BORDER = (28, 40, 60)
# Separador entre las cajas de numeros. Tiene que verse: si no se distingue
# donde termina una caja y empieza la otra, el ojo asocia cada numero con el
# rotulo equivocado, porque el rotulo va arriba y el numero al medio.
BOX_BORDER = (62, 84, 112)
HEADER_BG = (12, 20, 34)

GRID_MINOR = (18, 42, 62)
GRID_MAJOR = (32, 70, 100)

ECG = (32, 245, 92)
PLETH = (0, 226, 255)
# El pulso va en un tono distinto del SpO2 a proposito, aunque los dos salgan
# del MAX30102: son los dos numeros que mas se confunden entre si, y compartir
# color hace que se confundan mas.
PULSE = (140, 170, 255)
RESP = (255, 214, 0)
ART = (255, 60, 60)

TEXT = (222, 232, 242)
TEXT_DIM = (120, 138, 158)
TEXT_FAINT = (74, 90, 108)

ALARM_HIGH = (255, 45, 45)
ALARM_MEDIUM = (255, 179, 0)
ALARM_LOW = (0, 200, 255)
ALARM_TECHNICAL = (150, 165, 185)
OK = (54, 214, 118)

LEVEL_COLORS = {
    "high": ALARM_HIGH,
    "medium": ALARM_MEDIUM,
    "low": ALARM_LOW,
    "technical": ALARM_TECHNICAL,
}

# Fuentes candidatas, en orden de preferencia (Pi primero, Windows despues)
_SANS = ["dejavusans", "notosans", "liberationsans", "segoeui", "arial", "freesans"]
_MONO = ["dejavusansmono", "liberationmono", "consolas", "couriernew", "monospace"]


class Fonts:
    """Cachea las fuentes por tamano: crear una SysFont por frame es carisimo."""

    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self._cache: dict[tuple[str, int, bool], pygame.font.Font] = {}
        self._sans = pygame.font.match_font(",".join(_SANS), bold=False)
        self._sans_bold = pygame.font.match_font(",".join(_SANS), bold=True)
        self._mono = pygame.font.match_font(",".join(_MONO), bold=False)

    def _get(self, path: str | None, size: int, bold: bool) -> pygame.font.Font:
        size = max(8, int(size * self.scale))
        key = (path or "default", size, bold)
        cached = self._cache.get(key)
        if cached is None:
            if path:
                cached = pygame.font.Font(path, size)
                cached.set_bold(bold)
            else:
                cached = pygame.font.SysFont(None, size, bold=bold)
            self._cache[key] = cached
        return cached

    def sans(self, size: int, bold: bool = False) -> pygame.font.Font:
        return self._get(self._sans_bold if bold else self._sans, size, False)

    def mono(self, size: int) -> pygame.font.Font:
        return self._get(self._mono, size, False)

    def digits(self, size: int) -> pygame.font.Font:
        """Numeros grandes de los signos vitales."""
        return self._get(self._sans_bold, size, False)


def blit_text(surface: pygame.Surface, font: pygame.font.Font, text: str,
              color, pos: tuple[int, int], align: str = "left") -> pygame.Rect:
    rendered = font.render(text, True, color)
    rect = rendered.get_rect()
    if align == "left":
        rect.topleft = pos
    elif align == "right":
        rect.topright = pos
    elif align == "center":
        rect.midtop = pos
    elif align == "middle":  # centrado en los dos ejes
        rect.center = pos
    elif align == "midright":
        rect.midright = pos
    elif align == "midleft":
        rect.midleft = pos
    surface.blit(rendered, rect)
    return rect


def draw_heart(surface: pygame.Surface, center: tuple[int, int], size: int, color) -> None:
    """Corazon simple: dos circulos y un triangulo. Parpadea con cada latido."""
    cx, cy = center
    radius = max(2, size // 4)
    pygame.draw.circle(surface, color, (cx - radius, cy - radius // 2), radius)
    pygame.draw.circle(surface, color, (cx + radius, cy - radius // 2), radius)
    pygame.draw.polygon(surface, color, [
        (cx - radius * 2, cy - radius // 2),
        (cx + radius * 2, cy - radius // 2),
        (cx, cy + radius * 2),
    ])


def make_grid(width: int, height: int, px_per_second: float,
              major_color=GRID_MAJOR, minor_color=GRID_MINOR,
              background=BG) -> pygame.Surface:
    """Papel milimetrado de ECG: 5 mm por cuadro grande, 25 mm/s de velocidad.

    A 25 mm/s un cuadro grande son 200 ms y uno chico 40 ms, que es la
    referencia que usa cualquiera que sepa leer un ECG.
    """
    surface = pygame.Surface((width, height)).convert()
    surface.fill(background)

    small = px_per_second * 0.04  # 40 ms por cuadro chico
    if small < 3:  # pantalla muy angosta: solo cuadros grandes
        small = px_per_second * 0.20

    # Celdas cuadradas: el paso vertical es el mismo que el horizontal,
    # ajustado para que entre un numero entero de cuadros grandes.
    big = small * 5
    rows = max(1, round(height / big))
    y_small = height / (rows * 5)

    x = 0.0
    index = 0
    while x < width:
        color = major_color if index % 5 == 0 else minor_color
        pygame.draw.line(surface, color, (int(x), 0), (int(x), height))
        x += small
        index += 1

    y = 0.0
    index = 0
    while y <= height:
        color = major_color if index % 5 == 0 else minor_color
        pygame.draw.line(surface, color, (0, int(y)), (width, int(y)))
        y += y_small
        index += 1

    return surface


def make_plain_background(width: int, height: int, background=BG) -> pygame.Surface:
    surface = pygame.Surface((width, height)).convert()
    surface.fill(background)
    return surface
