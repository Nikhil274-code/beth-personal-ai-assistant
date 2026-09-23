# Beth — Personal AI Assistant

Beth is a Windows voice-controlled personal assistant built in Python.

It combines custom wake-word detection, local speech recognition, local LLM
intent parsing, PC automation, voice responses, and a real-time desktop UI.

## Features

- Custom "Hey Beth" wake-word detection
- Local speech-to-text using faster-whisper
- Local LLM processing using Ollama
- PC and application automation
- Automatic ASR correction for recurring misrecognitions
- Voice responses using edge-tts
- Timers and reminders
- Web search
- File search
- Short-term conversation memory
- Real-time animated desktop interface
- Text-input fallback

## Architecture

```text
Wake Word
    ↓
Speech Recognition
    ↓
Local LLM Intent Parsing
    ↓
Action Execution
    ↓
Text-to-Speech

Technologies
Python
faster-whisper
Ollama
PyAudio
NumPy
pywebview
JavaScript
HTML5 Canvas
JSON
edge-tts
Speech Recognition Evaluation

Beth was tested using two rounds of self-recorded speech data:

Round 1: 51 sentences
Round 2: 40 sentences
Total: 91 sentences

Speech recognition accuracy:

Before: 66%
After: 86%

The second recording round focused on areas where the first model performed poorly, particularly command phrases.

ASR Correction

Beth includes a correction layer for recurring speech-recognition errors.

For example:

"open crop" → "open Chrome"

This allows recurring recognition mistakes to be corrected without retraining the model.

User Interface

Beth includes a desktop interface built with HTML5 Canvas and JavaScript, connected to the Python backend through pywebview.

The interface provides visual states for:

Idle
Listening
Thinking
Speaking
Error

It also includes live captions and a text-input fallback alongside voice interaction.

Conversation Memory

Beth maintains short-term conversation context using JSON-based persistence.

The system retains the last 10 conversation turns to provide context across recent interactions.

Actions

Beth can perform several PC and assistant actions, including:

Opening and closing applications
Web searches
To-do items
Timers
Reminders
Voice switching
File search
Study-mode links by subject
Project Structure
main.py                 # Application entry point
gui.py                  # Desktop UI integration
beth_ui.html            # HTML/JavaScript interface

phase1_ears_mouth.py    # Audio input and speech processing
phase2_brain.py         # LLM and intent processing
phase3_hands.py         # PC actions
phase4_wakeword.py      # Wake-word detection

accuracy_test.py        # Live accuracy testing
calculate_accuracy.py   # Dataset evaluation

corrections.json        # ASR correction mappings
Model & Dataset

The custom voice dataset and trained Whisper model are not included in this repository.

The dataset contains 91 self-recorded sentences used during development and evaluation.

Project Status

Active development.

License

MIT
