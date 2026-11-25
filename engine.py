"""
Speller-BCI · lógica de la interfaz (sin Qt, sin EEG, sin LLM).

Este módulo no sabe nada de ventanas ni de señales cerebrales. Solo maneja:
  - Qué opciones se muestran (Turn / Option)
  - En qué fase está el sistema (leyendo, eligiendo, mostrando la elección)
  - El historial para el botón "Volver"

Así se puede probar sin pantalla y, más adelante, se cambian dos piezas
sin tocar la interfaz:
  - MockEngine  -> un motor con LLM (misma interfaz: start / refresh / advance)
  - select(i)   -> lo llama el clasificador P300 en vez del mouse o el teclado
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


# ----------------------------------------------------------------------------
# Datos
# ----------------------------------------------------------------------------
class Cat(Enum):
    POSITIVE = "Positiva"
    NEGATIVE = "Negativa"
    QUESTION = "Pregunta"
    TOPIC = "Cambiar de tema"
    CONTROL = "Control"


@dataclass(frozen=True)
class Option:
    text: str
    cat: Cat


@dataclass(frozen=True)
class Turn:
    """Lo que ve el usuario en un momento dado."""
    prompt: str                  # lo que dice la otra persona
    options: tuple[Option, ...]  # 4 opciones: positiva, negativa, pregunta, tema
    ctx: int = 0                 # índices internos del motor de prueba
    variant: int = 0


@dataclass(frozen=True)
class TileInfo:
    """Lo que debe dibujar cada uno de los 6 recuadros."""
    label: str
    text: str
    cat: Cat
    enabled: bool


class Phase(Enum):
    READING = "reading"      # la barra se llena; el usuario lee
    SELECTING = "selecting"  # (más adelante: aquí empieza a leerse el EEG)
    FEEDBACK = "feedback"    # se resalta la opción elegida un instante
    # --- fases del modo con voz (voice_controller.py) ---
    LISTENING = "listening"    # esperando a que la otra persona hable
    RECORDING = "recording"    # grabando con el micrófono
    PROCESSING = "processing"  # transcribiendo / generando opciones
    CONFIRMING = "confirming"  # ventana corta para cancelar antes de hablar
    SPEAKING = "speaking"      # reproduciendo la respuesta en voz alta


class NoSpeechError(Exception):
    """No se detectó voz en la grabación."""


# ----------------------------------------------------------------------------
# Motor de opciones
# ----------------------------------------------------------------------------
class OptionEngine(Protocol):
    """Contrato que debe cumplir el motor. Hoy: MockEngine. Luego: LLM."""
    def start(self) -> Turn: ...
    def refresh(self, turn: Turn) -> Turn: ...                 # "Otras opciones"
    def advance(self, turn: Turn, chosen: Option) -> Turn: ... # tras elegir


# (positiva, negativa, pregunta, cambiar de tema): frases cortas a propósito
_BANK: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("¿Cómo te sientes hoy?", [
        ("Estoy bien", "Estoy mal", "¿Y tú?", "Otro tema, por favor"),
        ("Mucho mejor", "Algo cansado", "¿Qué hora es?", "Cambiemos de tema"),
        ("Tranquilo, gracias", "Con dolor", "¿Vienes mañana?", "Hablemos de otra cosa"),
    ]),
    ("¿Quieres comer algo?", [
        ("Sí, por favor", "No, gracias", "¿Qué hay?", "Otro tema, por favor"),
        ("Tengo hambre", "No tengo hambre", "¿Está caliente?", "Cambiemos de tema"),
        ("Un poco de agua", "Ahora no", "¿Y tú comes?", "Hablemos de otra cosa"),
    ]),
    ("¿Te duele algo?", [
        ("Sí, un poco", "No, nada", "¿Qué medicina es?", "Otro tema, por favor"),
        ("Me duele la cabeza", "Estoy cómodo", "¿Llamas al doctor?", "Cambiemos de tema"),
        ("Sí, la espalda", "Ya pasó", "¿Cuánto falta?", "Hablemos de otra cosa"),
    ]),
    ("¿Quieres ver a la familia?", [
        ("Sí, claro", "Hoy no", "¿Cuándo vienen?", "Otro tema, por favor"),
        ("Me encantaría", "Estoy cansado", "¿Quién viene?", "Cambiemos de tema"),
        ("Sí, que vengan", "Mejor mañana", "¿A qué hora?", "Hablemos de otra cosa"),
    ]),
]


class MockEngine:
    """Opciones fijas para probar la interfaz sin LLM."""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def _make(self, ctx: int, variant: int) -> Turn:
        prompt, variants = _BANK[ctx]
        pos, neg, que, tema = variants[variant]
        return Turn(
            prompt=prompt,
            options=(
                Option(pos, Cat.POSITIVE),
                Option(neg, Cat.NEGATIVE),
                Option(que, Cat.QUESTION),
                Option(tema, Cat.TOPIC),
            ),
            ctx=ctx,
            variant=variant,
        )

    def start(self) -> Turn:
        return self._make(0, 0)

    def refresh(self, turn: Turn) -> Turn:
        n = len(_BANK[turn.ctx][1])
        return self._make(turn.ctx, (turn.variant + 1) % n)

    def advance(self, turn: Turn, chosen: Option) -> Turn:
        if chosen.cat is Cat.TOPIC:  # cambiar de tema: salta a otro contexto
            others = [i for i in range(len(_BANK)) if i != turn.ctx]
            return self._make(self._rng.choice(others), 0)
        return self._make((turn.ctx + 1) % len(_BANK), 0)


# ----------------------------------------------------------------------------
# Controlador (máquina de estados)
# ----------------------------------------------------------------------------
NUM_TILES = 6
IDX_REFRESH = 4  # recuadro 5: "Otras opciones"
IDX_BACK = 5     # recuadro 6: "Volver"


class SpellerController:
    def __init__(
        self,
        engine: OptionEngine,
        read_time: float = 6.0,
        feedback_time: float = 0.45,
        allow_select_while_reading: bool = False,
    ):
        self.engine = engine
        self.read_time = read_time
        self.feedback_time = feedback_time
        self.allow_select_while_reading = allow_select_while_reading

        self.turn: Turn = engine.start()
        self.history: list[tuple[Turn, str]] = []  # (turno anterior, texto enviado)
        self.phase = Phase.READING
        self.flash_index: int | None = None
        self._elapsed = 0.0
        self._pending: tuple[str, Option | None] | None = None
        self.error: str | None = None

    # -- consulta ------------------------------------------------------------
    @property
    def progress(self) -> float:
        """0.0 a 1.0: avance de la barra de lectura."""
        if self.phase is Phase.READING:
            return min(self._elapsed / self.read_time, 1.0)
        return 1.0 if self.phase is Phase.SELECTING else 0.0

    @property
    def seconds_left(self) -> float:
        return max(self.read_time - self._elapsed, 0.0) if self.phase is Phase.READING else 0.0

    @property
    def display_prompt(self) -> str:
        """Texto de la tarjeta superior (lo que dice la otra persona)."""
        return self.turn.prompt

    @property
    def highlight_index(self) -> int | None:
        """Recuadro que debe verse resaltado en amarillo."""
        return self.flash_index if self.phase is Phase.FEEDBACK else None

    def status(self) -> tuple[str, str]:
        """(mensaje, tipo) para la línea de estado. Tipos: muted, ok, warn, err."""
        if self.error:
            return self.error, "err"
        if self.phase is Phase.READING:
            return f"Lee con calma · {math.ceil(self.seconds_left)} s", "muted"
        if self.phase is Phase.SELECTING:
            return "Elige una opción", "ok"
        return "Opción elegida", "warn"

    @property
    def last_sent(self) -> str | None:
        return self.history[-1][1] if self.history else None

    def tiles(self) -> list[TileInfo]:
        info = [TileInfo(o.cat.value, o.text, o.cat, True) for o in self.turn.options]
        info.append(TileInfo(Cat.CONTROL.value, "Otras opciones", Cat.CONTROL, True))
        info.append(TileInfo(Cat.CONTROL.value, "Volver", Cat.CONTROL, bool(self.history)))
        return info

    def can_select(self, i: int) -> bool:
        if not 0 <= i < NUM_TILES or self.phase is Phase.FEEDBACK:
            return False
        if self.phase is Phase.READING and not self.allow_select_while_reading:
            return False
        return not (i == IDX_BACK and not self.history)

    # -- acciones ------------------------------------------------------------
    def select(self, i: int) -> bool:
        """Elegir un recuadro (0-5). Hoy lo llama el mouse/teclado; luego, el EEG."""
        if not self.can_select(i):
            return False
        if i < 4:
            self._pending = ("advance", self.turn.options[i])
        elif i == IDX_REFRESH:
            self._pending = ("refresh", None)
        else:
            self._pending = ("back", None)
        self.flash_index = i
        self.phase = Phase.FEEDBACK
        self._elapsed = 0.0
        return True

    def skip_wait(self) -> None:
        """Atajo de desarrollo: termina la lectura sin esperar."""
        if self.phase is Phase.READING:
            self._elapsed = self.read_time
            self.phase = Phase.SELECTING

    def tick(self, dt: float) -> None:
        """Avanza el reloj interno dt segundos (lo llama un temporizador)."""
        self._elapsed += dt
        if self.phase is Phase.READING and self._elapsed >= self.read_time:
            self.phase = Phase.SELECTING
        elif self.phase is Phase.FEEDBACK and self._elapsed >= self.feedback_time:
            self._apply_pending()

    # -- interno -------------------------------------------------------------
    def _apply_pending(self) -> None:
        kind, option = self._pending  # type: ignore[misc]
        if kind == "advance":
            self.history.append((self.turn, option.text))
            self.turn = self.engine.advance(self.turn, option)
        elif kind == "refresh":
            self.turn = self.engine.refresh(self.turn)
        else:  # back
            self.turn, _ = self.history.pop()
        self._pending = None
        self.flash_index = None
        self._elapsed = 0.0
        self.phase = Phase.READING
