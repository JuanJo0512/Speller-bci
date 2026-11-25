"""
Speller-BCI · controlador de la conversación con voz (sin Qt).

Flujo de un turno:

  LISTENING  -> la otra persona habla (tecla L)           [o se escribe con T]
  RECORDING  -> se graba hasta que deja de hablar
  PROCESSING -> voz -> texto -> el LLM genera 4 opciones
  READING    -> la barra de lectura se llena (luego aquí entra el EEG)
  SELECTING  -> el usuario elige un recuadro (hoy manual; luego P300)
  FEEDBACK   -> se resalta la opción elegida
  CONFIRMING -> ventana corta para cancelar con "Volver" antes de hablar
  SPEAKING   -> se reproduce la respuesta en voz alta  -> vuelve a LISTENING

Las tareas lentas (micrófono, red, voz) corren en hilos; tick() las revisa.
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import Future

from engine import (
    Cat, IDX_BACK, IDX_REFRESH, NoSpeechError, Option, Phase,
    SpellerController, TileInfo, Turn,
)

_CATS = (Cat.POSITIVE, Cat.NEGATIVE, Cat.QUESTION, Cat.TOPIC)


class _Placeholder:
    def start(self) -> Turn:
        return Turn(prompt="", options=tuple(Option("", c) for c in _CATS))

    def refresh(self, turn): return turn
    def advance(self, turn, chosen): return turn


def _spawn(fn, *args) -> Future:
    fut: Future = Future()

    def work():
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)

    threading.Thread(target=work, daemon=True).start()
    return fut


class VoiceSpellerController(SpellerController):
    def __init__(self, generator, listener=None, transcriber=None, speaker=None, *,
                 read_time: float = 6.0, feedback_time: float = 0.45,
                 allow_select_while_reading: bool = False,
                 confirm_time: float = 2.0, auto_listen: bool = False,
                 auto_listen_delay: float = 0.8, context_turns: int = 6, log=print):
        super().__init__(_Placeholder(), read_time=read_time, feedback_time=feedback_time,
                         allow_select_while_reading=allow_select_while_reading)
        self.generator, self.listener = generator, listener
        self.transcriber, self.speaker = transcriber, speaker
        self.confirm_time = confirm_time
        self.auto_listen, self.auto_listen_delay = auto_listen, auto_listen_delay
        self.context_turns = context_turns
        self._log = log

        self.phase = Phase.LISTENING
        self.partner_text = ""
        self.conversation: list[tuple[str, str]] = []  # ("other"|"user", texto)
        self._prior: list[tuple[str, str]] = []        # contexto del turno actual
        self._future: Future | None = None
        self._kind: str | None = None
        self._chosen_index: int | None = None
        self._pending_speech: str | None = None
        self._idle = 0.0

    # -- consulta ------------------------------------------------------------
    @property
    def display_prompt(self) -> str:
        if self.phase is Phase.RECORDING:
            return "Escuchando…"
        if self.phase is Phase.PROCESSING and self._kind == "process":
            return "Procesando lo que dijeron…"
        return self.partner_text or "Esperando a que la otra persona hable"

    @property
    def highlight_index(self) -> int | None:
        if self.phase is Phase.FEEDBACK:
            return self.flash_index
        if self.phase is Phase.CONFIRMING:
            return self._chosen_index
        return None

    @property
    def progress(self) -> float:
        if self.phase is Phase.CONFIRMING:
            return min(self._elapsed / max(self.confirm_time, 1e-6), 1.0)
        if self.phase in (Phase.READING, Phase.SELECTING):
            return super().progress
        return 0.0

    @property
    def busy(self) -> bool:
        return self.phase in (Phase.RECORDING, Phase.PROCESSING, Phase.SPEAKING)

    def status(self) -> tuple[str, str]:
        if self.error:
            return self.error, "err"
        msgs = {
            Phase.LISTENING: ("Pulsa L para que hable la otra persona (o T para escribir)", "muted"),
            Phase.RECORDING: ("Escuchando…", "warn"),
            Phase.PROCESSING: ("Pensando las respuestas…", "muted"),
            Phase.CONFIRMING: (f"Se dirá en {math.ceil(max(self.confirm_time - self._elapsed, 0))} s · "
                               f"6 cancela", "warn"),
            Phase.SPEAKING: ("Hablando…", "ok"),
        }
        return msgs.get(self.phase) or super().status()

    def tiles(self) -> list[TileInfo]:
        live = self.phase in (Phase.READING, Phase.SELECTING, Phase.FEEDBACK)
        info = []
        for i, o in enumerate(self.turn.options):
            on = live or (self.phase is Phase.CONFIRMING and i == self._chosen_index)
            info.append(TileInfo(o.cat.value, o.text or "—", o.cat, on))
        info.append(TileInfo(Cat.CONTROL.value, "Otras opciones", Cat.CONTROL,
                             live and bool(self.partner_text)))
        confirming = self.phase is Phase.CONFIRMING
        info.append(TileInfo(Cat.CONTROL.value, "Cancelar" if confirming else "Volver",
                             Cat.CONTROL, confirming))
        return info

    def can_select(self, i: int) -> bool:
        if self.phase is Phase.CONFIRMING:
            return i == IDX_BACK
        if self.phase not in (Phase.READING, Phase.SELECTING):
            return False
        if i == IDX_BACK:
            return False  # "Volver" solo existe mientras se confirma lo que se va a decir
        return super().can_select(i)

    # -- acciones del usuario --------------------------------------------------
    def start_listening(self) -> bool:
        if self.phase is not Phase.LISTENING:
            return False
        if self.listener is None or self.transcriber is None:
            self.error = "Micrófono desactivado: pulsa T para escribir lo que dice la otra persona"
            return False
        self.error = None
        self.phase = Phase.RECORDING
        self._launch("record", self.listener.listen)
        return True

    def submit_text(self, text: str) -> bool:
        text = (text or "").strip()
        if self.phase is not Phase.LISTENING or not text:
            return False
        self.error = None
        self.phase = Phase.PROCESSING
        self._launch("process", self._process_text, text)
        return True

    def select(self, i: int) -> bool:
        if not self.can_select(i):
            return False
        if self.phase is Phase.CONFIRMING:  # cancelar: vuelve a las mismas opciones
            self._pending_speech = None
            self._start_reading()
            return True
        ok = super().select(i)
        if ok and i < 4:
            self._chosen_index = i
        return ok

    def tick(self, dt: float) -> None:
        if self._future is not None:
            if self._future.done():
                self._collect()
            return
        if self.phase is Phase.CONFIRMING:
            self._elapsed += dt
            if self._elapsed >= self.confirm_time:
                self._speak_now()
        elif self.phase is Phase.LISTENING:
            self._idle += dt
            if self.auto_listen and self.listener is not None and self._idle >= self.auto_listen_delay:
                self.start_listening()
        elif self.phase in (Phase.READING, Phase.SELECTING, Phase.FEEDBACK):
            super().tick(dt)

    # -- interno ---------------------------------------------------------------
    def _launch(self, kind: str, fn, *args) -> None:
        self._kind = kind
        self._future = _spawn(fn, *args)

    def _context(self) -> list[tuple[str, str]]:
        return self.conversation[-self.context_turns:]

    def _process_audio(self, wav: bytes):
        t0 = time.perf_counter()
        text = self.transcriber.transcribe(wav).strip()
        t1 = time.perf_counter()
        if not text:
            raise NoSpeechError()
        options = self.generator.generate(self._context(), text, ())
        self._log(f"[turno] voz→texto {t1 - t0:.2f} s · LLM {time.perf_counter() - t1:.2f} s")
        return text, options

    def _process_text(self, text: str):
        return text, self.generator.generate(self._context(), text, ())

    def _collect(self) -> None:
        fut, kind = self._future, self._kind
        self._future = self._kind = None
        try:
            result = fut.result()
        except NoSpeechError:
            self._fail("No se escuchó nada. Pulsa L e inténtalo de nuevo.", kind)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"{type(exc).__name__}: {exc}", kind)
        else:
            self._done(kind, result)

    def _fail(self, msg: str, kind: str | None) -> None:
        self.error = msg
        self._idle = -5.0  # espera extra antes de reintentar en modo auto-escucha
        self.phase = Phase.SELECTING if kind == "refresh" else Phase.LISTENING

    def _done(self, kind: str, result) -> None:
        if kind == "record":
            self.phase = Phase.PROCESSING
            self._launch("process", self._process_audio, result)
        elif kind == "process":
            text, options = result
            self._prior = self._context()
            self.conversation.append(("other", text))
            self.partner_text = text
            self.turn = Turn(prompt=text, options=tuple(options))
            self._start_reading()
        elif kind == "refresh":
            self.turn = Turn(self.turn.prompt, tuple(result))
            self._start_reading()
        elif kind == "speak":
            self.partner_text = ""
            self._idle = 0.0
            self.phase = Phase.LISTENING

    def _start_reading(self) -> None:
        self.phase = Phase.READING
        self._elapsed = 0.0
        self.flash_index = None
        self._chosen_index = None
        self.error = None

    def _apply_pending(self) -> None:  # lo llama SpellerController.tick al acabar el FEEDBACK
        kind, option = self._pending  # type: ignore[misc]
        self._pending = None
        self.flash_index = None
        self._elapsed = 0.0
        if kind == "advance":
            self._pending_speech = option.text
            if self.confirm_time <= 0:
                self._speak_now()
            else:
                self.phase = Phase.CONFIRMING
        elif kind == "refresh":
            self.phase = Phase.PROCESSING
            avoid = tuple(o.text for o in self.turn.options)
            self._launch("refresh", self.generator.generate, self._prior, self.partner_text, avoid)

    def _speak_now(self) -> None:
        text = self._pending_speech or ""
        self._pending_speech = None
        self.conversation.append(("user", text))
        self.history.append((self.turn, text))
        self._elapsed = 0.0
        self.phase = Phase.SPEAKING
        self._launch("speak", self.speaker.speak if self.speaker else (lambda _t: None), text)
