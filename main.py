"""
BETH — Main (with GUI)
--------------------------------
This replaces running phase4_wakeword.py directly. It's the same wake word
-> listen -> brain -> hands loop, but now it also updates the visual
window at each step instead of only printing to the terminal.

Run this file instead of phase4_wakeword.py from now on:
    python main.py
"""

import time
import os
import json

from gui import start_gui, set_state, update_caption
from phase1_ears_mouth import speak
from phase2_brain import parse_intent
from phase3_hands import execute_steps

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beth_history.json")

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[System] Error loading history: {e}")
        return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[System] Error saving history: {e}")



def process_command(text, conversation_history):
    """The core Brain -> Hands pipeline. Can be called by voice or text."""
    from phase2_brain import parse_intent
    from phase3_hands import execute_steps

    if not text or not text.strip():
        return "I didn't hear anything."

    print(f"[Process] Processing command: {text}")
    # 1. Running the local LLM to figure out what you want
    set_state("thinking")
    print(f"[Process] Requesting intent from Brain...")
    intent = parse_intent(text, history=conversation_history)
    print(f"[Process] Intent received: {intent}")

    print(f"[Process] Executing steps via Hands...")
    result_message = execute_steps(intent, raw_text=text, history=conversation_history)
    print(f"[Process] Result: {result_message}")

    # Visual state: Green for success, Red for confusion/errors
    if intent[0]["action"] == "unknown":
        set_state("error")
    else:
        set_state("speaking")

    update_caption(f"Beth: {result_message}")
    speak(result_message)
    time.sleep(0.3)
    set_state("idle")

    # Update history (keep last 10 turns)
    conversation_history.append(("User", text))
    conversation_history.append(("Beth", result_message))
    while len(conversation_history) > 20:
        conversation_history.pop(0)

    save_history(conversation_history)

    return result_message

def get_current_history():
    """Helper for the GUI API to get the latest history."""
    return conversation_history

def voice_loop():
    """The full assistant loop, running in a background thread while the
    GUI owns the main thread.
    """
    from phase1_ears_mouth import ASSISTANT_NAME, listen, speak
    from phase2_brain import parse_intent
    from phase3_hands import execute_steps
    from phase4_wakeword import load_wake_model, wait_for_wake_word

    # 1. Announce immediately so you know she's alive
    print(f"[System] Voice loop started. Announcing online status...")
    speak(f"{ASSISTANT_NAME} online.")
    set_state("idle")

    # 2. PRE-LOAD ALL MODELS HERE
    print(f"[System] Pre-loading AI models into memory... Please wait.")
    wake_model = load_wake_model()

    from phase1_ears_mouth import _load_whisper_model
    _load_whisper_model()

    print(f"[System] All models loaded. Beth is now fully ready!")

    while True:
        wait_for_wake_word(wake_model)
        speak("Yes?")
        set_state("listening")
        text = listen()

        if text is None:
            speak("Sorry, I didn't catch that.")
            set_state("idle")
            update_caption("")
            continue

        update_caption(f"You: {text}")

        if text.strip().lower() in {"quit", "goodbye", "exit", "go to sleep", "bye"}:
            speak("Goodbye.")
            set_state("idle")
            update_caption("")
            time.sleep(1)
            os._exit(0)

        # Use the extracted process_command function
        process_command(text, conversation_history)

        set_state("idle")


if __name__ == "__main__":
    # Initialize global history before starting the GUI
    conversation_history = load_history()
    start_gui(voice_loop, process_command, get_current_history)
