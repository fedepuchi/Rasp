"""Cola local de lecturas pendientes.

La WiFi del hospital se cae, el broker se reinicia, el Pi se queda sin red. Sin
esta cola esas lecturas se pierden. Aca se guardan en SQLite y se reenvian
cuando vuelve la conexion.

Misma forma que `iot/src/buffer.py` de SIAPPC a proposito: mismo esquema, mismo
comportamiento. Esta duplicado para que el monitor funcione tambien fuera de ese
repo; dentro de SIAPPC cada proceso usa su propio archivo igual, asi que no hay
riesgo de que uno borre lo que el otro todavia no confirmo.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS pendiente (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  payload    TEXT NOT NULL,
  hash       TEXT NOT NULL UNIQUE,
  creado_en  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pendiente_creado ON pendiente (creado_en);
"""


class Buffer:
    def __init__(self, path: str, max_rows: int):
        self._max_rows = max_rows
        # check_same_thread=False: el callback de paho corre en su propio hilo.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def add(self, payload: str, hash_: str) -> None:
        try:
            self._db.execute(
                "INSERT INTO pendiente (payload, hash, creado_en) VALUES (?, ?, ?)",
                (payload, hash_, time.time()),
            )
        except sqlite3.IntegrityError:
            # Mismo hash: la lectura ya estaba encolada. No es un error.
            return
        self._trim()
        self._db.commit()

    def _trim(self) -> None:
        """Descarta lo mas viejo cuando la cola crece sin limite.

        Ante una desconexion larga preferimos perder las lecturas antiguas y
        conservar las recientes: son las que importan para el monitoreo.
        """
        self._db.execute(
            """
            DELETE FROM pendiente
            WHERE id NOT IN (
              SELECT id FROM pendiente ORDER BY id DESC LIMIT ?
            )
            """,
            (self._max_rows,),
        )

    def pending(self, limit: int = 200) -> Iterator[tuple[int, str]]:
        cursor = self._db.execute(
            "SELECT id, payload FROM pendiente ORDER BY id LIMIT ?", (limit,)
        )
        yield from cursor.fetchall()

    def drop(self, row_id: int) -> None:
        """Se llama solo cuando el broker confirmo la entrega (QoS 1)."""
        self._db.execute("DELETE FROM pendiente WHERE id = ?", (row_id,))
        self._db.commit()

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM pendiente").fetchone()[0]

    def close(self) -> None:
        self._db.close()
