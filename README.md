# Speller-BCI predictivo en español

Prototipo de un **comunicador asistido por inteligencia artificial** para personas que no pueden hablar ni moverse (por ejemplo, con ELA o parálisis). Una persona le habla al sistema, una IA propone **cuatro respuestas cortas en español** y quien no puede hablar elige una. En el futuro, esa elección se hará con la actividad cerebral (potencial **P300**, medido con EEG); hoy se maneja con teclado y ratón.

Proyecto del Semillero de Investigación de la Institución Universitaria de Envigado (IUE).
Autor: Juan José Flórez Cardona · Investigador principal: Hernán Darío Villota Bolaños.

> **Estado: prototipo en desarrollo.** Todavía **no** usa señales del cerebro y no se ha probado con personas con discapacidad.

## Estado actual

| Componente | Estado |
|---|---|
| Interfaz de 6 recuadros (4 respuestas, "Otras opciones", "Volver") con barra de lectura | Funciona |
| Generación de respuestas con IA (Groq) a partir de la conversación | Funciona (escribiendo lo que dice la otra persona con la tecla T) |
| Entrada de voz (micrófono → texto) y salida de voz (texto → voz) | En desarrollo / pruebas |
| Confirmación antes de hablar | En desarrollo |
| Detección de P300 con casco EEG (Unicorn Hybrid Black) | Pendiente |
| Teclado para escribir palabras propias con P300 | Pendiente (en diseño) |
| Pruebas con voluntarios | Pendiente |

## Cómo funciona

1. La otra persona habla (o se escribe lo que dijo).
2. La IA propone 4 respuestas: positiva, negativa, una pregunta y un cambio de tema.
3. La persona lee las opciones (una barra de tiempo se llena) y elige una.
4. El sistema dice la frase en voz alta.

La **lógica** (`engine.py`, `voice_controller.py`) está separada de la **ventana** (`speller_gui.py`), así más adelante el clasificador P300 solo tiene que llamar a la función que elige un recuadro.

## Requisitos

- Python 3.10 o superior
- Una clave de API de [Groq](https://console.groq.com) (gratuita, con límites)
- Opcional: micrófono y voz de Windows en español (para el modo con voz)

## Instalación (Windows, PowerShell)

```powershell
git clone https://github.com/USUARIO/speller-bci.git
cd speller-bci
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Crea tu archivo de configuración copiando el ejemplo:

```powershell
copy .env.example .env
```

Abre `.env` y pega tu clave en `GROQ_API_KEY`. **Nunca subas el archivo `.env` al repositorio** (ya está en `.gitignore`).

## Uso

```powershell
# Modo recomendado para empezar: sin micrófono; la otra persona se escribe con la tecla T
.\.venv\Scripts\python.exe speller_gui.py --text-only --no-tts

# Con voz de salida
.\.venv\Scripts\python.exe speller_gui.py --text-only

# Modo completo (micrófono + IA + voz)
.\.venv\Scripts\python.exe speller_gui.py

# Modo manual sin IA ni internet (frases fijas de prueba)
.\.venv\Scripts\python.exe speller_gui.py --mock
```

Teclas: **1–6** eligen un recuadro · **L** escuchar · **T** escribir lo que dice la otra persona · **Espacio** salta la espera · **F11** pantalla completa · **Esc** sale.

Otras opciones: `--read-time`, `--speak-delay`, `--free-select`, `--auto-listen`, `--fallback` (usa Ollama local si falla Groq), `--mic-index`, `--list-mics`, `--list-voices`.

## Pruebas

```powershell
.\.venv\Scripts\python.exe test_logic.py
```

Prueban la lógica sin pantalla, micrófono ni internet (con servicios simulados). No prueban el contacto real con Groq, el micrófono ni la voz.

## Estructura

| Archivo | Qué hace |
|---|---|
| `speller_gui.py` | Ventana (PyQt6) y arranque del programa |
| `engine.py` | Lógica de los 6 recuadros, fases y modo manual de prueba |
| `voice_controller.py` | Flujo de la conversación con voz |
| `llm_engine.py` | Prompt, lectura de la respuesta y conexión con Groq / Ollama |
| `voice_io.py` | Micrófono, voz a texto (Whisper en Groq) y texto a voz (voces de Windows) |
| `test_logic.py` | Pruebas de la lógica |

## Privacidad

Con el modo completo, **el texto (y el audio, si se usa el micrófono) se envía a Groq** para generar las respuestas y transcribir. Es aceptable para pruebas con conversaciones inventadas; para uso real con personas se necesita su consentimiento y, preferiblemente, un modelo que funcione sin internet.

## Próximos pasos

1. Pulir la interfaz y la voz, y medir la velocidad de respuesta.
2. Conectar el casco EEG y detectar el P300 para elegir con el cerebro.
3. Teclado en pantalla para palabras propias, también con P300.
4. Pruebas con voluntarios (precisión, velocidad y comodidad), con consentimiento informado.

## Licencia

Pendiente de definir con la dirección del proyecto.
