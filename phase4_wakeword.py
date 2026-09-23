"""
AETHER — Phase 4: Wake Word
--------------------------------
Google STT is a bad fit for a custom wake name — it often returns no text.
This phase uses a tiny local Whisper model instead:

  1. Always-on mic with a simple volume gate (ignore silence)
  2. When you speak, transcribe that clip locally
  3. Bias Whisper toward "Hey Beth"
  4. Then the usual listen -> brain -> hands loop

First run downloads Whisper tiny.en (~75MB). After that it's offline.
"""

import time
import os

import numpy as np
import pyaudio
from faster_whisper import WhisperModel

from phase1_ears_mouth import ASSISTANT_NAME, listen, speak
from phase2_brain import parse_intent
from phase3_hands import execute_action

# --- Config ------------------------------------------------------------

WAKE_PHRASE = "hey beth"
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1600  # 100ms
MAX_SECONDS = 8.0
MIN_SECONDS = 0.55
SILENCE_CHUNKS = 2.5  # 800ms of quiet = end of utterance
SPEECH_RMS = 400

WAKE_NAME_VARIANTS = {
    "beth",
    "beths",
    "beth's",
}
WAKE_PREFIXES = {"hey", "hi", "ok", "okay", "yo", "hello", "hay"}


def normalize_heard(text: str) -> str:
    text = text.lower().strip()
    for ch in ".,!?;:\"'":
        text = text.replace(ch, " ")
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


def is_wake_phrase(text: str | None) -> bool:
    if not text:
        return False

    heard = normalize_heard(text)
    words = heard.split()
    compact = heard.replace(" ", "")

    if "beth" in compact:
        return True

    if heard in {"beth"}:
        return True

    if len(words) >= 2 and words[-1] in WAKE_NAME_VARIANTS:
        if any(w in WAKE_PREFIXES for w in words[:-1]):
            return True

    return False


def load_wake_model() -> WhisperModel:
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper-beth-ct2-v2")
    print(f"Loading custom wake-word model from {model_path}...")
    try:
        return WhisperModel(model_path, device="cpu", compute_type="int8")
    except ValueError:
        return WhisperModel(model_path, device="cpu", compute_type="float32")


def transcribe_wake(model: WhisperModel, pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # Quiet mics: boost before Whisper so it doesn't treat speech as silence
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if 0.01 < peak < 0.35:
        audio = np.clip(audio * (0.4 / peak), -1.0, 1.0)

    segments, _ = model.transcribe(
        audio,
        language="en",
        beam_size=1,
        best_of=1,
        temperature=0.0,
        vad_filter=False,
        without_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt="Hey Beth.",
    )
    return " ".join(segment.text for segment in segments).strip()


def wait_for_wake_word(model: WhisperModel) -> None:
    """Listen locally until the wake phrase is heard. Closes the mic before return."""
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )

    print(f"Listening for wake word... (say '{WAKE_PHRASE}')")
    print("This is local Whisper, not Google. Dots = silence.")

    speech_chunks: list[bytes] = []
    in_speech = False
    silence = 0
    idle = 0
    max_chunks = int(MAX_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES)
    min_chunks = int(MIN_SECONDS * SAMPLE_RATE / CHUNK_SAMPLES)

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
                print(".", end="", flush=True)
                if idle % 20 == 0:
                    print(" still listening")
                continue

            done = in_speech and (
                silence >= SILENCE_CHUNKS or len(speech_chunks) >= max_chunks
            )
            if not done:
                continue

            clip = b"".join(speech_chunks)
            speech_chunks = []
            in_speech = False
            silence = 0
            idle = 0

            if len(clip) < min_chunks * CHUNK_SAMPLES * 2:
                print("\n(too short — ignored)")
                continue

            print("\n(transcribing locally...)")
            heard = transcribe_wake(model, clip)
            if not heard:
                print("(no words — try again, a little louder)")
                continue

            print(f"(heard: {heard})")
            if is_wake_phrase(heard):
                print("Wake word matched.")
                return
            print("(not the wake word — still listening)")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def main():
    model = load_wake_model()
    speak(f"{ASSISTANT_NAME} online. Say {WAKE_PHRASE} whenever you need me.")
    time.sleep(0.4)

    while True:
        wait_for_wake_word(model)
        speak("Yes?")
        time.sleep(0.5)

        text = listen()

        if text is None:
            speak("Sorry, I didn't catch that.")
            continue

        if text.strip().lower() in {"quit", "goodbye", "exit", "go to sleep"}:
            speak("Goodbye.")
            break

        intent = parse_intent(text)
        print(f"Intent: {intent}")

        result_message = execute_action(intent)
        speak(result_message)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
