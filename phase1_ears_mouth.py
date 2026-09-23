"""
BETH — Phase 1 (v2): Ears + Mouth, now with local Whisper
--------------------------------
CHANGE FROM v1: the main listen() function used to send audio to Google's
free speech API, which struggles with accents and non-US English. This
version replaces it with local Whisper (faster_whisper) — the same engine
you're already using for wake word detection, but a bigger model
("base.en" instead of "tiny.en") since accuracy matters more here than
raw speed.

Nothing sent over the internet for speech recognition anymore. First run
downloads the base.en model (~140MB), then it's fully offline.
"""

import asyncio
import os
import tempfile
import json
import re

import edge_tts
import numpy as np
import pyaudio
from faster_whisper import WhisperModel
from playsound import playsound


# --- Setup ---------------------------------------------------------------

ASSISTANT_NAME = "Beth"
TTS_NAME = "Beth"

# Pick any voice from: python -m edge_tts --list-voices
# A few good English options:
#   en-US-AriaNeural     - US female, warm/clear (default here)
#   en-US-GuyNeural      - US male
#   en-IN-NeerjaNeural   - Indian English female
#   en-IN-PrabhatNeural  - Indian English male
#   en-GB-SoniaNeural    - British female
EDGE_VOICE = "en-US-AriaNeural"
current_voice = EDGE_VOICE
EDGE_RATE = "+0%"     # speed: try "-10%" for slower, "+10%" for faster
EDGE_PITCH = "-8Hz"   # lower = softer/less sharp, higher = brighter/sharper

def set_voice(voice_name: str):
    """Change the current TTS voice."""
    global current_voice
    current_voice = voice_name
    print(f"[Beth Mouth] Voice changed to {voice_name}")



# --- Audio capture config --------------------------------------------------
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1600       # 100ms per chunk
MAX_SECONDS = 8.0          # give up recording after this long
MIN_SECONDS = 0.4          # ignore clips shorter than this (likely noise)
SILENCE_CHUNKS = 10        # Increased to ~1 second to prevent cutting off
SPEECH_RMS = 200           # Lowered to be more sensitive to quieter voices

_whisper_model: WhisperModel | None = None


def _apply_corrections(text: str) -> str:
    """Fix common misinterpretations using a correction map."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "corrections.json"), "r") as f:
            corrections = json.load(f)
    except Exception:
        return text

    text_lower = text.lower()

    # We look for the "wrong" word in the text and replace it with the "correct" one
    for correct, wrongs in corrections.items():
        for wrong in wrongs:
            if wrong.lower() in text_lower:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(wrong), re.IGNORECASE)
                text = pattern.sub(correct, text)

    return text

def _load_whisper_model() -> WhisperModel:
    """Load once and reuse — loading the model fresh every call is slow."""
    global _whisper_model
    if _whisper_model is None:
        print("Loading local speech model (small.en for faster baseline accuracy)...")
        try:
            # Switched back to 'small.en' for speed and stability
            _whisper_model = WhisperModel("small.en", device="cpu", compute_type="int8")
        except ValueError:
            _whisper_model = WhisperModel("small.en", device="cpu", compute_type="float32")
    return _whisper_model


def for_voice(text: str) -> str:
    """Swap the written name for a spelling the TTS engine pronounces right."""
    spoken = text
    for written in (ASSISTANT_NAME, ASSISTANT_NAME.lower(), "Aether", "AETHER"):
        spoken = spoken.replace(written, TTS_NAME)
    return spoken


async def _generate_and_play(text: str):
    """Generate speech with edge-tts (cloud, needs internet) and play it."""
    communicate = edge_tts.Communicate(text, current_voice, rate=EDGE_RATE, pitch=EDGE_PITCH)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await communicate.save(tmp_path)
        playsound(tmp_path)
    finally:
        os.remove(tmp_path)



def speak(text: str):
    """Convert text to speech and play it out loud."""
    print(f"{ASSISTANT_NAME}: {text}")
    try:
        asyncio.run(_generate_and_play(for_voice(text)))
    except Exception as e:
        print(f"(TTS error — check your internet connection: {e})")


def _record_utterance() -> bytes | None:
    """
    Record from the mic until you stop talking (or timeout).
    Returns raw PCM bytes, or None if nothing usable was captured.
    """
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    print("Listening... (speak now)")
    speech_chunks: list[bytes] = []
    in_speech = False
    silence = 0
    idle_limit = int(6.0 * SAMPLE_RATE / CHUNK_SAMPLES)  # ~6 sec to start talking
    idle = 0
    max_chunks = int(MAX_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES)

    try:
        while True:
            data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            samples = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

            if rms >= SPEECH_RMS:
                in_speech = True
                silence = 0
                speech_chunks.append(data)
            elif in_speech:
                speech_chunks.append(data)
                silence += 1
            else:
                idle += 1
                if idle >= idle_limit:
                    return None  # nobody spoke in time
                continue

            done = in_speech and (silence >= SILENCE_CHUNKS or len(speech_chunks) >= max_chunks)
            if done:
                clip = b"".join(speech_chunks)
                min_bytes = int(MIN_SECONDS * SAMPLE_RATE) * 2
                return clip if len(clip) >= min_bytes else None
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def _transcribe(pcm: bytes) -> str | None:
    """Run local Whisper on a raw audio clip and return the recognized text."""
    model = _load_whisper_model()
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    # Boost quiet clips so Whisper has clearer signal to work with
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if 0.01 < peak < 0.35:
        audio = np.clip(audio * (0.4 / peak), -1.0, 1.0)

    segments, _ = model.transcribe(
        audio,
        language="en",
        beam_size=5,          # higher = more accurate, a bit slower than wake-word's beam_size=1
        best_of=5,
        temperature=0.0,
        vad_filter=True,      # trims leading/trailing silence automatically
        condition_on_previous_text=False,
    )
    text = " ".join(segment.text for segment in segments).strip()

    if not text:
        return None

    # Apply Correction Layer
    text = _apply_corrections(text)

    return text


def listen() -> str | None:
    """
    Record a command from the mic and transcribe it locally with Whisper.
    Returns None if nothing usable was heard.
    """
    clip = _record_utterance()
    if clip is None:
        print("(No speech heard)")
        return None

    print("(transcribing locally...)")
    text = _transcribe(clip)
    if text is None:
        print("(Couldn't understand that — try again)")
        return None

    print(f"You said: {text}")
    return text


def main():
    speak(f"{ASSISTANT_NAME} online. Press Enter, then speak.")

    while True:
        user_input = input("\n[Press Enter to talk, or type 'quit' to exit] ")
        if user_input.strip().lower() == "quit":
            speak("Goodbye.")
            break

        text = listen()
        if text is None:
            speak("Sorry, I didn't catch that.")
            continue

        speak(f"You said: {text}")


if __name__ == "__main__":
    main()
