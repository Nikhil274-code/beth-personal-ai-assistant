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
