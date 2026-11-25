"""
Speller-BCI · entrada y salida de voz.

  MicListener          graba del micrófono hasta que la otra persona deja de hablar
  GroqTranscriber      voz -> texto (Whisper en Groq)
  WindowsSpeechSpeaker texto -> voz con las voces de Windows (sin instalar nada extra)
  PrintSpeaker         "habla" imprimiendo en consola (para pruebas sin audio)

Cada clase tiene UN método bloqueante (listen / transcribe / speak); el
controlador los ejecuta en hilos para que la ventana no se congele.
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import wave

from engine import NoSpeechError

SAMPLE_RATE = 16_000


# ----------------------------------------------------------------------------
# Detección de fin de frase (lógica pura, sin audio: se puede probar)
# ----------------------------------------------------------------------------
class Endpointer:
    """Decide cuándo empezó y cuándo terminó de hablar, a partir del volumen (RMS)
    de fragmentos consecutivos. Devuelve None / "done" / "nospeech"."""

    def __init__(self, frame_s=0.03, silence_s=1.0, max_s=12.0, wait_s=8.0,
                 min_speech_s=0.25, calib_s=0.3, ratio=3.0, floor=0.012):
        self.frame_s, self.silence_s, self.max_s = frame_s, silence_s, max_s
        self.wait_s, self.min_speech_s = wait_s, min_speech_s
        self.calib_frames = max(1, int(calib_s / frame_s))
        self.ratio, self.floor = ratio, floor
        self._calib: list[float] = []
        self.threshold: float | None = None
        self.started = False
        self._t = 0.0
        self._speech = 0.0
        self._silence = 0.0

    def feed(self, rms: float) -> str | None:
        self._t += self.frame_s
        if self.threshold is None:  # primeros fragmentos: medir el ruido de fondo
            self._calib.append(rms)
            if len(self._calib) >= self.calib_frames:
                noise = sorted(self._calib)[len(self._calib) // 2]
                self.threshold = max(noise * self.ratio, self.floor)
            return None
        loud = rms > self.threshold
        if not self.started:
            if loud:
                self.started = True
                self._speech = self.frame_s
            elif self._t >= self.wait_s:
                return "nospeech"
            return None
        if loud:
            self._speech += self.frame_s
            self._silence = 0.0
        else:
            self._silence += self.frame_s
        if self._silence >= self.silence_s:
            return "done" if self._speech >= self.min_speech_s else "nospeech"
        if self._t >= self.max_s:
            return "done"
        return None


def pcm16_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class MicListener:
    def __init__(self, device: int | None = None, endpointer_kwargs: dict | None = None):
        self.device = device
        self.kw = endpointer_kwargs or {}

    def listen(self) -> bytes:
        import numpy as np
        import sounddevice as sd

        ep = Endpointer(**self.kw)
        block = int(SAMPLE_RATE * ep.frame_s)
        chunks: list[bytes] = []
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=block, device=self.device) as stream:
            while True:
                data, _overflow = stream.read(block)
                raw = bytes(data)
                chunks.append(raw)
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                state = ep.feed(float(np.sqrt(np.mean(x * x))))
                if state == "nospeech":
                    raise NoSpeechError()
                if state == "done":
                    break
        return pcm16_to_wav(b"".join(chunks))


# ----------------------------------------------------------------------------
# Voz -> texto
# ----------------------------------------------------------------------------
# Whisper a veces "inventa" estas frases cuando oye silencio o ruido.
_HALLUCINATIONS = ("amara.org", "gracias por ver", "suscríbete", "subtítulos")


class GroqTranscriber:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 language: str = "es", timeout: float = 20.0, client=None):
        self.model = model or os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
        self.language = language
        self._client = client
        self._cfg = dict(base_url="https://api.groq.com/openai/v1",
                         api_key=api_key or os.environ.get("GROQ_API_KEY", ""), timeout=timeout)

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(max_retries=0, **self._cfg)
        return self._client

    def transcribe(self, wav_bytes: bytes) -> str:
        resp = self.client.audio.transcriptions.create(
            file=("audio.wav", wav_bytes, "audio/wav"),
            model=self.model, language=self.language, temperature=0,
        )
        text = (getattr(resp, "text", "") or "").strip()
        if any(h in text.lower() for h in _HALLUCINATIONS):
            return ""
        return text


# ----------------------------------------------------------------------------
# Texto -> voz
# ----------------------------------------------------------------------------
class PrintSpeaker:
    def speak(self, text: str) -> None:
        print(f"[VOZ] {text}")


_PS_SPEAK = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$hint = $env:SPELLER_VOICE
$es = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'es*' }
$pick = $null
if ($hint) { $pick = $es | Where-Object { $_.VoiceInfo.Name -like "*$hint*" } | Select-Object -First 1 }
if (-not $pick) { $pick = $es | Where-Object { $_.VoiceInfo.Culture.Name -eq 'es-CO' } | Select-Object -First 1 }
if (-not $pick) { $pick = $es | Select-Object -First 1 }
if ($pick) { $s.SelectVoice($pick.VoiceInfo.Name) } else { [Console]::Error.WriteLine('SIN_VOZ_ES') }
$s.Rate = [int]$env:SPELLER_RATE
$s.Speak([Console]::In.ReadToEnd())
"""

_PS_LIST = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + '  [' + $_.VoiceInfo.Culture.Name + ']' }
"""


def _run_ps(script: str, stdin: bytes = b"", env_extra: dict | None = None, timeout: float = 60):
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, **(env_extra or {})}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        input=stdin, capture_output=True, timeout=timeout, env=env, creationflags=flags,
    )


class WindowsSpeechSpeaker:
    """Usa las voces que ya trae Windows. Necesita una voz en español instalada
    (Configuración > Hora e idioma > Voz). Si no hay, usará la voz por defecto."""

    def __init__(self, voice_hint: str | None = None, rate: int = 0):
        self.voice_hint = voice_hint or os.environ.get("SPELLER_VOICE", "")
        self.rate = max(-10, min(10, rate))
        self._warned = False

    def speak(self, text: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsSpeechSpeaker solo funciona en Windows; usa --no-tts")
        res = _run_ps(_PS_SPEAK, text.encode("utf-8"),
                      {"SPELLER_VOICE": self.voice_hint, "SPELLER_RATE": str(self.rate)})
        err = res.stderr.decode("utf-8", "ignore")
        if "SIN_VOZ_ES" in err and not self._warned:
            self._warned = True
            print("[VOZ] No hay voz en español instalada: se usó la voz por defecto. "
                  "Instálala en Configuración > Hora e idioma > Voz.")
        if res.returncode != 0 and "SIN_VOZ_ES" not in err:
            raise RuntimeError(f"PowerShell devolvió {res.returncode}: {err.strip()[:200]}")


def list_windows_voices() -> str:
    if sys.platform != "win32":
        return "(solo disponible en Windows)"
    return _run_ps(_PS_LIST).stdout.decode("utf-8", "ignore")


def list_microphones() -> str:
    import sounddevice as sd
    return str(sd.query_devices())
