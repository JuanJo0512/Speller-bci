"""
Speller-BCI · generación de opciones con LLM (Groq, Ollama, OpenRouter...).

Todos estos servicios hablan el mismo "idioma" (API compatible con OpenAI), así
que una sola clase sirve para los tres; solo cambian base_url, clave y modelo.

Contrato (lo usa voice_controller.py):
    generate(history, partner_text, avoid=()) -> tuple[Option, Option, Option, Option]
        history      : [("other"|"user", texto), ...] conversación previa
        partner_text: lo que acaba de decir la otra persona
        avoid        : textos de opciones que NO debe repetir ("Otras opciones")
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Sequence

from engine import Cat, Option

CATS = (Cat.POSITIVE, Cat.NEGATIVE, Cat.QUESTION, Cat.TOPIC)
MAX_WORDS = 8  

SYSTEM_PROMPT = (
    "Eres el asistente de comunicación de una persona que no puede hablar y que "
    "conversa con otra persona. Tu trabajo es proponer lo que ELLA podría responder, "
    "siempre en primera persona, en español natural y cálido. "
    "Las respuestas deben ser MUY cortas (de 4 a 8 palabras) porque la persona "
    "las elige con su actividad cerebral y leer cansa."
)


class ParseError(ValueError):
    pass


# ----------------------------------------------------------------------------
# Prompt y lectura de la respuesta
# ----------------------------------------------------------------------------
def build_messages(history: Sequence[tuple[str, str]], partner_text: str,
                   avoid: Sequence[str] = ()) -> list[dict]:
    lines = []
    if history:
        lines.append("Conversación reciente:")
        for role, text in history:
            lines.append(f"{'Otra persona' if role == 'other' else 'Yo'}: {text}")
        lines.append("")
    lines.append(f'La otra persona acaba de decir: "{partner_text}"')
    lines.append("")
    lines.append(
        "Escribe exactamente 4 respuestas que YO podría decir, una por línea, en este orden:\n"
        "1) positiva (acepta o está de acuerdo)\n"
        "2) negativa (rechaza o no está de acuerdo)\n"
        "3) una pregunta breve para la otra persona\n"
        "4) una frase para cambiar de tema\n"
        "Máximo 8 palabras por línea. Solo las 4 líneas: sin numeración, sin comillas, "
        "sin explicaciones."
    )
    if avoid:
        lines.append("")
        lines.append("No repitas ninguna de estas frases: " + " | ".join(avoid))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


_LABEL = re.compile(r"^(positiva|negativa|pregunta|cambiar de tema|tema)\s*[:\-–]\s*", re.I)
_BULLET = re.compile(r"^\s*(?:\d+\s*[\).:\-]|[-*•])\s*")


def parse_options(text: str) -> tuple[Option, ...]:
    """Convierte la respuesta del modelo en 4 Option. Lanza ParseError si no cuadra."""
    clean = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        line = _BULLET.sub("", raw.strip())
        line = _LABEL.sub("", line)
        line = line.strip().strip("\"'“”«»").strip()
        if line:
            clean.append(line)
    if len(clean) != 4:
        raise ParseError(f"se esperaban 4 líneas y llegaron {len(clean)}: {clean!r}")
    for line in clean:
        if len(line.split()) > MAX_WORDS:
            raise ParseError(f"línea demasiado larga ({len(line.split())} palabras): {line!r}")
    return tuple(Option(line, cat) for line, cat in zip(clean, CATS))


# ----------------------------------------------------------------------------
# Generador genérico (Groq / Ollama / OpenRouter)
# ----------------------------------------------------------------------------
class ChatGenerator:
    def __init__(self, *, name: str, base_url: str, api_key: str, model: str,
                 temperature: float = 0.5, refresh_temperature: float = 0.9,
                 max_tokens: int = 300, timeout: float = 15.0,
                 extra_body: dict | None = None, client=None):
        self.name = name
        self.model = model
        self.temperature = temperature
        self.refresh_temperature = refresh_temperature
        self.max_tokens = max_tokens
        self.extra_body = extra_body or None
        self.last_latency_s = 0.0
        self._client = client
        self._cfg = dict(base_url=base_url, api_key=api_key, timeout=timeout)

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI  # import tardío: solo se exige si se usa
            self._client = OpenAI(max_retries=0, **self._cfg)
        return self._client

    def _chat(self, messages: list[dict], temperature: float) -> str:
        kwargs = dict(model=self.model, messages=messages, temperature=temperature,
                      max_completion_tokens=self.max_tokens)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def generate(self, history, partner_text, avoid=()):
        messages = build_messages(history, partner_text, avoid)
        temp = self.refresh_temperature if avoid else self.temperature
        t0 = time.perf_counter()
        last_err: Exception | None = None
        for _ in range(2):  # un reintento si el formato sale mal
            text = self._chat(messages, temp)
            try:
                options = parse_options(text)
                self.last_latency_s = time.perf_counter() - t0
                return options
            except ParseError as err:
                last_err = err
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "Formato incorrecto. Responde SOLO con 4 líneas "
                                                "de máximo 8 palabras, sin numeración."},
                ]
        raise last_err  # type: ignore[misc]


class FallbackGenerator:
    """Prueba los generadores en orden; si uno falla (sin internet, límite), usa el siguiente."""

    def __init__(self, generators: Sequence, log=print):
        self.generators = list(generators)
        self.last_used = ""
        self._log = log

    def generate(self, history, partner_text, avoid=()):
        errors = []
        for g in self.generators:
            try:
                out = g.generate(history, partner_text, avoid)
                self.last_used = g.name
                self._log(f"[LLM] {g.name} ({g.model}) · {g.last_latency_s:.2f} s")
                return out
            except Exception as exc:  # noqa: BLE001 — queremos probar el siguiente
                errors.append(f"{g.name}: {type(exc).__name__}: {exc}")
                self._log(f"[LLM] {g.name} falló → {errors[-1]}")
        raise RuntimeError("Todos los modelos fallaron · " + " | ".join(errors))


# ----------------------------------------------------------------------------
# Fábricas (leen la configuración del entorno / archivo .env)
# ----------------------------------------------------------------------------
def load_env(path: str | Path = ".env") -> None:
    """Lector mínimo de .env (KEY=VALUE). No pisa variables que ya existan."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def groq_generator(**overrides) -> ChatGenerator:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("Falta GROQ_API_KEY (ponla en el archivo .env)")
    effort = os.environ.get("GROQ_REASONING_EFFORT", "low")
    cfg = dict(
        name="groq", base_url="https://api.groq.com/openai/v1", api_key=key,
        model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        extra_body={"reasoning_effort": effort} if effort else None,
        timeout=12.0,
    )
    cfg.update(overrides)
    return ChatGenerator(**cfg)


def ollama_generator(**overrides) -> ChatGenerator:
    cfg = dict(
        name="ollama", base_url="http://localhost:11434/v1", api_key="ollama",
        model=os.environ.get("OLLAMA_MODEL", "llama3.2:3b"), timeout=60.0,
    )
    cfg.update(overrides)
    return ChatGenerator(**cfg)
