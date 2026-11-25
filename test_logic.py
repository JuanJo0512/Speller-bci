"""Pruebas de la lógica (sin pantalla, micrófono ni internet).   python test_logic.py"""
import time
from types import SimpleNamespace as NS

from engine import Cat, MockEngine, NoSpeechError, Option, Phase, SpellerController
from llm_engine import (ChatGenerator, FallbackGenerator, ParseError, build_messages,
                        parse_options)
from voice_controller import VoiceSpellerController
from voice_io import Endpointer, pcm16_to_wav


# ---------- 1) modo manual original (no debe romperse) ------------------------
def run(c, secs):
    for _ in range(int(secs * 100)):
        c.tick(0.01)

c = SpellerController(MockEngine(seed=1), read_time=2.0, feedback_time=0.4)
assert not c.select(0)
run(c, 2.1); assert c.phase is Phase.SELECTING
assert c.select(4); run(c, 0.5); assert c.turn.variant == 1
run(c, 2.1); assert c.select(0); run(c, 0.5); assert c.turn.prompt == "¿Quieres comer algo?"
run(c, 2.1); assert c.select(5); run(c, 0.5); assert c.turn.ctx == 0
assert c.status()[1] == "muted" and c.display_prompt == c.turn.prompt
print("1) modo manual OK")

# ---------- 2) lectura de la respuesta del LLM ---------------------------------
ok = parse_options("1) Sí, claro\n2. No, gracias\n- ¿Qué hay?\nTema: Hablemos de otra cosa")
assert [o.text for o in ok] == ["Sí, claro", "No, gracias", "¿Qué hay?", "Hablemos de otra cosa"]
assert [o.cat for o in ok] == [Cat.POSITIVE, Cat.NEGATIVE, Cat.QUESTION, Cat.TOPIC]
assert parse_options('"Claro"\n"No"\n"¿Cuál?"\n"Otro tema"')[0].text == "Claro"
for bad in ["a\nb\nc", "a\nb\nc\nd\ne", "uno dos tres cuatro cinco seis siete\nb\nc\nd"]:
    try:
        parse_options(bad); raise SystemExit("debía fallar: " + bad)
    except ParseError:
        pass
m = build_messages([("other", "Hola"), ("user", "Hola, bien")], "¿Comes algo?", ["Sí", "No"])
assert "Otra persona: Hola" in m[1]["content"] and "Yo: Hola, bien" in m[1]["content"]
assert "¿Comes algo?" in m[1]["content"] and "No repitas" in m[1]["content"]
print("2) prompt y lectura OK")

# ---------- 3) cliente simulado: reintento y respaldo ---------------------------
def fake_client(replies):
    calls = []
    def create(**kw):
        calls.append(kw)
        return NS(choices=[NS(message=NS(content=replies.pop(0)))])
    return NS(chat=NS(completions=NS(create=create))), calls

cl, calls = fake_client(["respuesta rara", "Sí\nNo\n¿Y tú?\nOtro tema"])
g = ChatGenerator(name="fake", base_url="x", api_key="x", model="m", client=cl,
                  extra_body={"reasoning_effort": "low"})
out = g.generate([], "¿Hola?")
assert len(calls) == 2 and out[0].text == "Sí" and calls[0]["extra_body"] == {"reasoning_effort": "low"}
assert calls[0]["temperature"] == 0.5
g.client.chat.completions.create  # no revienta
cl2, calls2 = fake_client(["Sí\nNo\n¿Y tú?\nOtro tema"]); g2 = ChatGenerator(name="f", base_url="", api_key="", model="m", client=cl2)
g2.generate([], "x", ["Sí"]); assert calls2[0]["temperature"] == 0.9  # "otras opciones" = más variedad

class Boom:
    name, model, last_latency_s = "boom", "x", 0.0
    def generate(self, *a): raise ConnectionError("sin internet")
class Good:
    name, model, last_latency_s = "local", "y", 0.1
    def generate(self, *a): return parse_options("a\nb\nc\nd")
fb = FallbackGenerator([Boom(), Good()], log=lambda *_: None)
assert fb.generate([], "x")[0].text == "a" and fb.last_used == "local"
try:
    FallbackGenerator([Boom()], log=lambda *_: None).generate([], "x"); raise SystemExit("debía fallar")
except RuntimeError:
    pass
print("3) reintento y respaldo OK")

# ---------- 4) detección de fin de frase ----------------------------------------
def feed(ep, pattern):  # pattern: [(rms, segundos)]
    for rms, secs in pattern:
        for _ in range(round(secs / ep.frame_s)):
            r = ep.feed(rms)
            if r: return r
    return None
assert feed(Endpointer(), [(0.003, 0.5), (0.1, 1.0), (0.003, 1.2)]) == "done"
assert feed(Endpointer(), [(0.003, 9.0)]) == "nospeech"
assert feed(Endpointer(), [(0.003, 0.5), (0.1, 0.1), (0.003, 1.2)]) == "nospeech"  # un golpe, no voz
assert feed(Endpointer(max_s=3), [(0.003, 0.5), (0.1, 5.0)]) == "done"             # tope de duración
assert feed(Endpointer(), [(0.003, 0.5), (0.1, 0.5), (0.003, 0.5), (0.1, 0.5), (0.003, 1.2)]) == "done"  # pausa corta no corta
w = pcm16_to_wav(b"\x00\x00" * 1600); assert w[:4] == b"RIFF"
print("4) detección de voz OK")

# ---------- 5) conversación completa con voz (hilos reales) ----------------------
class FakeListener:
    def __init__(self): self.fail = None
    def listen(self):
        time.sleep(0.05)
        if self.fail: raise self.fail
        return b"wav"
class FakeSTT:
    def __init__(self): self.said = ["¿Cómo estás?", "¿Quieres comer algo?"]
    def transcribe(self, wav): return self.said.pop(0) if self.said else ""
class FakeGen:
    name, model, last_latency_s = "fake", "f", 0.0
    def __init__(self): self.calls = []
    def generate(self, history, partner, avoid=()):
        self.calls.append((list(history), partner, tuple(avoid)))
        tag = "bis" if avoid else ""
        return tuple(Option(f"{t}{tag}", c) for t, c in
                     zip(["Bien", "Mal", "¿Y tú?", "Otro tema"], (Cat.POSITIVE, Cat.NEGATIVE, Cat.QUESTION, Cat.TOPIC)))
class FakeSpeaker:
    def __init__(self): self.said = []
    def speak(self, t): time.sleep(0.05); self.said.append(t)

def pump(c, cond, timeout=5.0):
    t0 = time.time()
    while not cond():
        c.tick(0.01); time.sleep(0.004)
        assert time.time() - t0 < timeout, f"tiempo agotado en fase {c.phase}"

lis, stt, gen, spk = FakeListener(), FakeSTT(), FakeGen(), FakeSpeaker()
v = VoiceSpellerController(gen, lis, stt, spk, read_time=0.3, feedback_time=0.1, confirm_time=0.4, log=lambda *_: None)
assert v.phase is Phase.LISTENING and not any(t.enabled for t in v.tiles())
assert not v.select(0), "no se elige sin opciones"
assert v.start_listening() and v.phase is Phase.RECORDING and v.busy
pump(c := v, lambda: v.phase is Phase.READING)
assert v.display_prompt == "¿Cómo estás?" and gen.calls[0][0] == []
assert not v.select(0), "bloqueado mientras carga la barra"
pump(v, lambda: v.phase is Phase.SELECTING)
assert not v.select(5), "Volver apagado fuera de la confirmación"

# "Otras opciones": pide otras y no repite
assert v.select(4); pump(v, lambda: v.phase is Phase.READING)
assert gen.calls[-1][2] == ("Bien", "Mal", "¿Y tú?", "Otro tema") and v.turn.options[0].text == "Bienbis"

# elegir -> confirmación -> CANCELAR con 6 -> vuelve a las mismas opciones
pump(v, lambda: v.phase is Phase.SELECTING)
assert v.select(0); pump(v, lambda: v.phase is Phase.CONFIRMING)
assert v.highlight_index == 0 and v.tiles()[5].text == "Cancelar" and v.tiles()[5].enabled
assert not v.select(1), "en confirmación solo se puede cancelar"
assert v.select(5) and v.phase is Phase.READING and spk.said == []
assert v.turn.options[0].text == "Bienbis"

# elegir de nuevo y dejar que hable
pump(v, lambda: v.phase is Phase.SELECTING); assert v.select(2)
pump(v, lambda: v.phase is Phase.LISTENING)
assert spk.said == ["¿Y tú?bis"] and v.last_sent == "¿Y tú?bis" and v.partner_text == ""
assert v.conversation == [("other", "¿Cómo estás?"), ("user", "¿Y tú?bis")]

# segundo turno: el LLM recibe el historial
assert v.start_listening(); pump(v, lambda: v.phase is Phase.READING)
assert gen.calls[-1][0] == [("other", "¿Cómo estás?"), ("user", "¿Y tú?bis")] and gen.calls[-1][1] == "¿Quieres comer algo?"
print("5) conversación con voz OK")

# ---------- 6) errores y texto escrito ---------------------------------------------
v2 = VoiceSpellerController(FakeGen(), lis, FakeSTT(), FakeSpeaker(), read_time=0.2, confirm_time=0, log=lambda *_: None)
lis.fail = NoSpeechError(); v2.start_listening(); pump(v2, lambda: v2.phase is Phase.LISTENING)
assert "No se escuchó" in v2.error and v2.status()[1] == "err"
lis.fail = None
v2.stt = None
assert v2.submit_text("  Hola, ¿cómo va?  ") and v2.phase is Phase.PROCESSING
pump(v2, lambda: v2.phase is Phase.READING); assert v2.display_prompt == "Hola, ¿cómo va?" and v2.error is None
pump(v2, lambda: v2.phase is Phase.SELECTING); v2.select(1)
pump(v2, lambda: v2.phase is Phase.LISTENING)            # confirm_time=0: habla directo
assert v2.last_sent == "Mal"
sin_mic = VoiceSpellerController(FakeGen(), None, None, None, log=lambda *_: None)
assert not sin_mic.start_listening() and "T" in sin_mic.error
class Rota:
    name, model, last_latency_s = "r", "r", 0
    def generate(self, *a): raise RuntimeError("límite de peticiones")
v3 = VoiceSpellerController(Rota(), lis, FakeSTT(), FakeSpeaker(), log=lambda *_: None)
v3.submit_text("hola"); pump(v3, lambda: v3.phase is Phase.LISTENING)
assert "límite" in v3.error
# auto-escucha
v4 = VoiceSpellerController(FakeGen(), lis, FakeSTT(), FakeSpeaker(), auto_listen=True, auto_listen_delay=0.1, log=lambda *_: None)
pump(v4, lambda: v4.phase is Phase.RECORDING)
print("6) errores, texto y auto-escucha OK")
print("\nTODAS LAS PRUEBAS PASARON")
