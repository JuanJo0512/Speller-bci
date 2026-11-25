"""
Speller-BCI · interfaz

Ejecutar:
    python speller_gui.py                 # conversación completa: micrófono + Groq + voz
    python speller_gui.py --text-only     # sin micrófono: la otra persona se escribe con T
    python speller_gui.py --no-tts        # no reproduce voz (solo la imprime en consola)
    python speller_gui.py --mock          # modo manual anterior, sin LLM ni internet
Otras opciones:  --read-time 6  --speak-delay 2  --free-select  --auto-listen
                 --fallback (usar Ollama si Groq falla)  --mic-index N  --list-mics  --list-voices
Teclas: 1-6 eligen · L escuchar a la otra persona · T escribir lo que dice · Espacio salta
        la espera · F11 pantalla completa · Esc sale
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QProgressBar, QSizePolicy, QVBoxLayout, QWidget,
)

from engine import Cat, MockEngine, SpellerController, TileInfo

# ----------------------------------------------------------------------------
# Estilo
# ----------------------------------------------------------------------------
BG = "#0a0f1f"
PANEL = "#121a30"
BORDER = "#26324f"
TEXT = "#eef1f8"
MUTED = "#8a96b4"
YELLOW = "#fcd34d"      # recuadro elegido (igual que en las diapositivas)
READING_BLUE = "#4f7cff"
READY_GREEN = "#34d399"

CAT_COLOR = {
    Cat.POSITIVE: "#34d399",
    Cat.NEGATIVE: "#f87171",
    Cat.QUESTION: "#60a5fa",
    Cat.TOPIC: "#c4a7ff",
    Cat.CONTROL: MUTED,
}
FONT_FAMILIES = ["Segoe UI", "Roboto", "Helvetica Neue", "Arial"]


def make_font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(size)
    f.setWeight(weight)
    return f


# ----------------------------------------------------------------------------
# Recuadro
# ----------------------------------------------------------------------------
class Tile(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, index: int):
        super().__init__()
        self.index = index
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(150)

        self.number = QLabel(str(index + 1))
        self.number.setObjectName("tileNum")
        self.number.setFont(make_font(13, QFont.Weight.DemiBold))
        self.chip = QLabel()
        self.chip.setObjectName("tileChip")
        self.chip.setFont(make_font(12))
        self.text = QLabel()
        self.text.setObjectName("tileText")
        self.text.setFont(make_font(24, QFont.Weight.DemiBold))
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)

        top = QHBoxLayout()
        top.addWidget(self.number)
        top.addStretch()
        top.addWidget(self.chip)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 18)
        lay.addLayout(top)
        lay.addStretch(1)
        lay.addWidget(self.text)
        lay.addStretch(1)

        self._enabled = True

    def render(self, info: TileInfo, flashing: bool) -> None:
        self._enabled = info.enabled
        self.text.setText(info.text)
        self.chip.setText(info.label)
        if flashing:
            bg, bd, fg, num, chip = YELLOW, YELLOW, "#1a1a1a", "#1a1a1a", "#1a1a1a"
        elif not info.enabled:
            bg, bd, fg, num, chip = BG, BORDER, "#4a5573", "#4a5573", "#4a5573"
        else:
            bg, bd, fg, num, chip = PANEL, BORDER, TEXT, MUTED, CAT_COLOR[info.cat]
        self.setStyleSheet(
            f"#tile{{background:{bg};border:2px solid {bd};border-radius:14px;}}"
            f"#tileText{{color:{fg};background:transparent;}}"
            f"#tileNum{{color:{num};background:transparent;}}"
            f"#tileChip{{color:{chip};background:transparent;}}"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nombre de Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)


# ----------------------------------------------------------------------------
# Ventana principal
# ----------------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, controller: SpellerController):
        super().__init__()
        self.ctrl = controller
        self.setWindowTitle("Speller-BCI · Comunicación asistida")
        self.resize(1180, 760)
        self.setStyleSheet(f"background:{BG};")

        # encabezado
        title = QLabel("Comunicación asistida")
        title.setFont(make_font(16, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color:{TEXT};")
        self.status = QLabel()
        self.status.setFont(make_font(13))
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.status)

        # lo que dice la otra persona
        self.prompt = QLabel()
        self.prompt.setFont(make_font(28, QFont.Weight.DemiBold))
        self.prompt.setStyleSheet(f"color:{TEXT};")
        self.prompt.setWordWrap(True)
        who = QLabel("Te preguntan")
        who.setFont(make_font(12))
        who.setStyleSheet(f"color:{MUTED};")
        self.last = QLabel()
        self.last.setFont(make_font(13))
        self.last.setStyleSheet(f"color:{MUTED};")
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(
            f"#card{{background:{PANEL};border:2px solid {BORDER};border-radius:14px;}}"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 14, 22, 14)
        cl.addWidget(who)
        cl.addWidget(self.prompt)
        cl.addWidget(self.last)

        # 6 recuadros (2 filas x 3 columnas)
        self.tiles = [Tile(i) for i in range(6)]
        grid = QGridLayout()
        grid.setSpacing(14)
        for i, t in enumerate(self.tiles):
            grid.addWidget(t, i // 3, i % 3)
            t.clicked.connect(self.on_tile_clicked)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        for r in range(2):
            grid.setRowStretch(r, 1)

        # barra de lectura + ayuda
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(16)
        self._bar_color = None
        self._bar_busy = False
        hint = QLabel("Teclas 1–6 eligen · L escuchar · T escribir · Espacio salta la espera · F11 pantalla completa · Esc sale")
        hint.setFont(make_font(11))
        hint.setStyleSheet(f"color:{MUTED};")

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 18)
        root.setSpacing(14)
        root.addLayout(head)
        root.addWidget(card)
        root.addLayout(grid, 1)
        root.addWidget(self.bar)
        root.addWidget(hint)

        self._last_t = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(30)
        self.refresh_view()

    # -- eventos -------------------------------------------------------------
    def on_tick(self) -> None:
        now = time.monotonic()
        self.ctrl.tick(now - self._last_t)
        self._last_t = now
        self.refresh_view()

    def on_tile_clicked(self, i: int) -> None:
        self.choose(i)

    def choose(self, i: int) -> None:
        """Punto único de entrada para elegir un recuadro.
        Hoy: mouse / teclado. Más adelante: el clasificador P300 llamará aquí."""
        if self.ctrl.select(i):
            self.refresh_view()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        first, last = Qt.Key.Key_1.value, Qt.Key.Key_6.value
        if first <= key <= last:
            self.choose(key - first)
        elif key == Qt.Key.Key_Space.value:
            self.ctrl.skip_wait()
            self.refresh_view()
        elif key == Qt.Key.Key_L.value and hasattr(self.ctrl, "start_listening"):
            self.ctrl.start_listening()
            self.refresh_view()
        elif key == Qt.Key.Key_T.value and hasattr(self.ctrl, "submit_text"):
            text, ok = QInputDialog.getText(self, "La otra persona dice", "Escribe lo que dijo:")
            if ok:
                self.ctrl.submit_text(text)
                self.refresh_view()
        elif key == Qt.Key.Key_F11.value:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key == Qt.Key.Key_Escape.value:
            self.close()
        else:
            super().keyPressEvent(event)

    # -- dibujo --------------------------------------------------------------
    def refresh_view(self) -> None:
        c = self.ctrl
        self.prompt.setText(c.display_prompt)
        self.last.setText(f"Tu última respuesta: {c.last_sent}" if c.last_sent else " ")
        hi = c.highlight_index
        for i, (tile, info) in enumerate(zip(self.tiles, c.tiles())):
            tile.render(info, flashing=(hi == i))

        busy = getattr(c, "busy", False)  # grabando / pensando / hablando: barra "en espera"
        if busy != self._bar_busy:
            self._bar_busy = busy
            self.bar.setRange(0, 0) if busy else self.bar.setRange(0, 1000)
        if not busy:
            self.bar.setValue(int(c.progress * 1000))
        msg, kind = c.status()
        color = {"muted": MUTED, "ok": READY_GREEN, "warn": YELLOW, "err": "#f87171"}[kind]
        bar_color = READY_GREEN if kind == "ok" else READING_BLUE
        if bar_color != self._bar_color:
            self._bar_color = bar_color
            self.bar.setStyleSheet(
                f"QProgressBar{{background:{PANEL};border:none;border-radius:8px;}}"
                f"QProgressBar::chunk{{background:{bar_color};border-radius:8px;}}"
            )
        self.status.setText(msg)
        self.status.setStyleSheet(f"color:{color};")


# ----------------------------------------------------------------------------
def build_controller(args):
    from engine import SpellerController
    common = dict(read_time=args.read_time, allow_select_while_reading=args.free_select)
    if args.mock:
        return SpellerController(MockEngine(seed=args.seed), **common)

    from llm_engine import FallbackGenerator, groq_generator, load_env, ollama_generator
    from voice_controller import VoiceSpellerController
    from voice_io import GroqTranscriber, MicListener, PrintSpeaker, WindowsSpeechSpeaker

    load_env(Path(__file__).with_name(".env"))
    generators = [groq_generator()]
    if args.fallback:  # por defecto SOLO Groq; Ollama únicamente si se pide con --fallback
        generators.append(ollama_generator())
    listener = transcriber = None
    if not args.text_only:
        listener = MicListener(device=args.mic_index)
        transcriber = GroqTranscriber()
    speaker = PrintSpeaker() if args.no_tts else WindowsSpeechSpeaker()
    return VoiceSpellerController(
        FallbackGenerator(generators), listener, transcriber, speaker,
        confirm_time=args.speak_delay, auto_listen=args.auto_listen, **common,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--read-time", type=float, default=6.0, help="segundos de lectura (barra)")
    ap.add_argument("--free-select", action="store_true", help="permitir elegir mientras carga la barra")
    ap.add_argument("--mock", action="store_true", help="modo manual sin LLM (frases fijas)")
    ap.add_argument("--text-only", action="store_true", help="sin micrófono: escribir con la tecla T")
    ap.add_argument("--no-tts", action="store_true", help="no reproducir voz (solo imprimir)")
    ap.add_argument("--fallback", action="store_true", help="usar Ollama local si Groq falla (por defecto: solo Groq)")
    ap.add_argument("--auto-listen", action="store_true", help="volver a escuchar solo tras hablar")
    ap.add_argument("--speak-delay", type=float, default=2.0, help="segundos para cancelar antes de hablar (0 = al instante)")
    ap.add_argument("--mic-index", type=int, default=None)
    ap.add_argument("--list-mics", action="store_true")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if args.list_mics or args.list_voices:
        from voice_io import list_microphones, list_windows_voices
        print(list_microphones() if args.list_mics else list_windows_voices())
        return

    try:
        ctrl = build_controller(args)
    except RuntimeError as exc:
        sys.exit(f"No se pudo iniciar: {exc}")
    app = QApplication(sys.argv)
    win = MainWindow(ctrl)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()