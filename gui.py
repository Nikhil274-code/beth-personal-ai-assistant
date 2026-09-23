"""
BETH — GUI Bridge
--------------------------------
Opens beth_ui.html in a real desktop window using pywebview, and gives your
voice loop a simple function, set_state(name), to update what's on screen
in real time — no more plain command-line output.

HOW IT WORKS: pywebview must run on the MAIN thread (a library requirement,
not optional). So the pattern is: start your voice loop in a BACKGROUND
thread, then call webview.start() on the main thread last. That's exactly
what start_gui() below does for you.

States you can pass to set_state(): 'idle', 'listening', 'thinking', 'speaking'
"""

import os
import threading

import webview

_window = None

class Api:
    def __init__(self, process_fn, history_provider):
        self.process_fn = process_fn
        self.history_provider = history_provider

    def send_text(self, text):
        """Called from JS when user types a command."""
        print(f"[GUI API] Text received: {text}")
        history = self.history_provider()
        result = self.process_fn(text, history)
        return result

def set_state(name: str):
    """
    Update the UI's visual state. Safe to call from any thread (including
    your voice loop's background thread) once the window is open.
    """
    if _window is None:
        return  # window not ready yet, ignore silently
    try:
        _window.evaluate_js(f"setState('{name}')")
    except Exception:
        pass  # window may be closing / not fully loaded yet, don't crash the voice loop

def update_caption(text: str):
    """
    Update the caption text on the UI. Safe to call from any thread.
    """
    if _window is None:
        return
    try:
        # Escape single quotes to prevent JS errors
        escaped_text = text.replace("'", "\\'")
        _window.evaluate_js(f"updateCaption('{escaped_text}')")
    except Exception:
        pass


def _run_voice_loop(voice_loop_fn):
    """Wraps the user's voice loop so Windows COM is initialized on this
    background thread — pyttsx3's SAPI5 backend needs this, and without it
    the very first speak() call hangs silently instead of erroring."""
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError:
        pass  # not on Windows, nothing to do

    voice_loop_fn()


def start_gui(voice_loop_fn, process_fn=None, history_provider=None):
    """
    Opens the Beth window and runs voice_loop_fn() in a background thread.
    voice_loop_fn should be a zero-argument function containing your normal
    listen -> brain -> hands loop (see main.py for an example).
    """
    global _window

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beth_ui.html")

    # Initialize the JS API if provided
    api = None
    if process_fn and history_provider:
        api = Api(process_fn, history_provider)

    _window = webview.create_window(
        "Beth",
        html_path,
        width=900,
        height=650,
        background_color="#050508",
        js_api=api,
    )

    # Run the voice assistant loop in the background so it doesn't block
    # the GUI's own event loop.
    voice_thread = threading.Thread(target=_run_voice_loop, args=(voice_loop_fn,), daemon=True)
    voice_thread.start()

    webview.start()  # blocks here until the window closes
