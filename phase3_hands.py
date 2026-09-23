"""
BETH — Phase 3 (v3): Hands
----------------------------

Responsible for EXECUTING actions produced by Phase 2.

Features:
- Multi-step sequential execution
- Timers
- Persistent reminders
- Timer restoration after restart
- Timer listing
- Timer cancellation
- Microsoft Edge web searches
- Website opening
- App opening/closing
- Local file search
- Existing URL shortcuts
- Backward compatibility with execute_action()

Timer data is stored locally in:
    beth_timers.json

IMPORTANT:
Timers can only speak when Beth is running.
If Beth was closed when a timer expired, overdue timers are
announced when Beth starts again.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import ctypes
import math
from ctypes import wintypes
import webbrowser
from datetime import datetime, timedelta, timezone
import requests

import psutil

from phase1_ears_mouth import listen, speak, set_voice
from phase2_brain import generate_chat_response


ASSISTANT_NAME = "Beth"

# ---------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------

MAX_STEPS = 8

TIMER_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_timers.json",
)

TODO_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_todo.json",
)

MEMORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_memory.json",
)

KNOWLEDGE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_knowledge.json",
)

SUBJECTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_subjects.json",
)

MACRO_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_macros.json",
)

CALENDAR_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "beth_calendar.json",
)


timer_lock = threading.RLock()  # Re-entrant: callbacks can safely call timer-file helpers.

active_timers = {}
tts_lock = threading.Lock()


def _speak_timer_message(message: str):
    """Speak timer/reminder alerts without allowing overlapping TTS calls."""
    try:
        with tts_lock:
            speak(message)
    except Exception as e:
        print(f"[Beth Timer] TTS error: {e}")


# ---------------------------------------------------------------------
# URL SHORTCUTS
# ---------------------------------------------------------------------

URL_SHORTCUTS = {
    "lms": "https://coach.mastersunion.org/",
    "my portfolio": "https://github.com",
    "anime": "https://crunchyroll.com",
    "dashboard": "https://notion.so",
    "music": "https://music.youtube.com",
}


# ---------------------------------------------------------------------
# LOCAL FILE SEARCH
# ---------------------------------------------------------------------

SEARCH_DIRECTORIES = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Pictures"),
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/Music"),
]


# ---------------------------------------------------------------------
# WINDOW AWARENESS (Screen Awareness)
# ---------------------------------------------------------------------

def get_active_window_info() -> tuple[str, str]:
    """Get the title and process name of the currently active window."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Unknown", "Unknown"

        # Get Window Title
        length = user32.GetWindowTextLength(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value

        # Get Process Name
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        try:
            proc = psutil.Process(int(pid.value))
            process_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = "Unknown"

        return title, process_name
    except Exception as e:
        print(f"[Beth Screen] Error: {e}")
        return "Unknown", "Unknown"

def identify_app() -> str:
    """Tell the user what app they are currently using."""
    title, proc = get_active_window_info()
    if title == "Unknown":
        return "I'm not sure what you're looking at right now."
    return f"You are currently using {proc} (Window: {title})."

def close_current_app() -> str:
    """Close the currently active window."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "I couldn't find an active window to close."

        # Send WM_CLOSE message
        user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0) # 0x0010 is WM_CLOSE
        title, _ = get_active_window_info()
        return f"Closing {title}."
    except Exception as e:
        return f"I couldn't close the current app. {e}"

# ---------------------------------------------------------------------
# SYSTEM HEALTH
# ---------------------------------------------------------------------

def get_system_health() -> str:
    """Check CPU and RAM usage and report the health of the system."""
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent

        status = "healthy"
        if cpu > 80 or ram > 80:
            status = "under heavy load"
        elif cpu > 50 or ram > 50:
            status = "moderate"

        return f"Your system is currently {status}. CPU usage is at {cpu}% and RAM is at {ram}%."
    except Exception as e:
        print(f"[Beth Health] Error: {e}")
        return "I had trouble checking your system health."

# ---------------------------------------------------------------------
# DOCUMENT INTELLIGENCE (RAG-lite)
# ---------------------------------------------------------------------

def _load_knowledge() -> dict:
    if not os.path.exists(KNOWLEDGE_FILE):
        return {}
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[Beth Knowledge] Load error: {e}")
    return {}

def _save_knowledge(data: dict):
    try:
        with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Beth Knowledge] Save error: {e}")

def load_document(target: str) -> str:
    """Load a text or PDF document into Beth's knowledge base."""
    target = target.strip()
    if not target:
        return "Please tell me which file you want me to read."

    # Try to find the file in the project root first, then in common dirs
    file_path = target
    if not os.path.exists(file_path):
        # Simple search in current directory
        potential_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), target)
        if os.path.exists(potential_path):
            file_path = potential_path
        else:
            return f"I couldn't find the file '{target}'."

    try:
        if file_path.endswith(".txt"):
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        elif file_path.endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as f:
                content = str(json.load(f))
        elif file_path.endswith(".pdf"):
            try:
                import pypdf
                reader = pypdf.PdfReader(file_path)
                content = ""
                for page in reader.pages:
                    content += page.extract_text() + "\n"
                if not content.strip():
                    return "I found the PDF, but it seems to be empty or contains only images."
            except ImportError:
                return "I can read PDFs, but you need to install the pypdf library first. Please run 'pip install pypdf'."
            except Exception as pdf_e:
                return f"I had trouble reading the PDF: {pdf_e}"
        else:
            return "I can only read .txt, .json, and .pdf files for now."

        knowledge = _load_knowledge()
        filename = os.path.basename(file_path)
        knowledge[filename] = content
        _save_knowledge(knowledge)

        return f"I've successfully read {filename}. You can now ask me questions about it."
    except Exception as e:
        return f"I had trouble reading the file. {e}"

def ask_document(query: str) -> str:
    """Answer a question based on the loaded knowledge base."""
    query = query.strip()
    if not query:
        return "What would you like to know about the document?"

    knowledge = _load_knowledge()
    if not knowledge:
        return "I haven't loaded any documents yet. Please ask me to read a file first."

    # Simple RAG-lite: Combine all knowledge into one context block
    all_text = "\n\n".join([f"--- {name} ---\n{content}" for name, content in knowledge.items()])

    # We use generate_chat_response but inject the knowledge into the prompt
    contextual_prompt = (
        f"You are answering a question based on the following provided documents:\n"
        f"{all_text}\n\n"
        f"User Question: {query}\n"
        f"Answer concisely based ONLY on the documents above. If the answer isn't there, say you don't know."
    )

    return generate_chat_response(contextual_prompt)

# ---------------------------------------------------------------------
# PERSONAL ASSISTANT (Todo, Weather, News, Voice)
# ---------------------------------------------------------------------

def change_voice(target: str) -> str:
    """Change Beth's voice based on the target accent/gender."""
    target = target.lower().strip()

    # Mapping common requests to edge-tts voices
    voice_map = {
        "british": "en-GB-SoniaNeural",
        "indian": "en-IN-NeerjaNeural",
        "male": "en-US-GuyNeural",
        "female": "en-US-AriaNeural",
        "us": "en-US-AriaNeural",
        "american": "en-US-AriaNeural",
    }

    voice = voice_map.get(target)
    if not voice:
        # If not in map, try to see if the user provided a direct voice name
        # (though unlikely via voice command)
        voice = target

    set_voice(voice)
    return f"Changing my voice to {target}."

def _load_todo_file() -> list:
    if not os.path.exists(TODO_FILE):
        return []
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[Beth Todo] Load error: {e}")
    return []

def _save_todo_file(data: list):
    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Beth Todo] Save error: {e}")

def add_todo(target: str) -> str:
    target = target.strip()
    if not target:
        return "I need to know what to add to your list."

    todos = _load_todo_file()
    todos.append(target)
    _save_todo_file(todos)
    return f"Added '{target}' to your todo list."

def list_todo() -> str:
    todos = _load_todo_file()
    if not todos:
        return "Your todo list is empty."

    items = [f"{i+1}. {item}" for i, item in enumerate(todos)]
    return "Your todo list contains: " + ", ".join(items) + "."

def remove_todo(target: str) -> str:
    target = target.strip()
    if not target:
        return "Which item should I remove?"

    todos = _load_todo_file()
    if not todos:
        return "Your todo list is already empty."

    # Try to parse as index
    try:
        idx = int(target) - 1
        if 0 <= idx < len(todos):
            removed = todos.pop(idx)
            _save_todo_file(todos)
            return f"Removed '{removed}' from your list."
    except ValueError:
        pass

    # Search by keyword
    target_low = target.lower()
    for i, item in enumerate(todos):
        if target_low in item.lower():
            removed = todos.pop(i)
            _save_todo_file(todos)
            return f"Removed '{removed}' from your list."

    return f"I couldn't find '{target}' in your todo list."

def _load_memory() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[Beth Memory] Load error: {e}")
    return {}

def _save_memory(data: dict):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Beth Memory] Save error: {e}")

def remember_fact(target: str, value: str) -> str:
    target = target.strip().lower()
    value = value.strip()
    if not target or not value:
        return "I need both a fact and a value to remember."

    memory = _load_memory()
    memory[target] = value
    _save_memory(memory)
    return f"Got it. I'll remember that your {target} is {value}."

def recall_fact(target: str) -> str:
    target = target.strip().lower()
    if not target:
        return "What would you like me to recall?"

    memory = _load_memory()
    value = memory.get(target)

    if value:
        return f"Your {target} is {value}."
    else:
        return f"I don't remember your {target} yet."

def get_weather(city: str) -> str:
    city = city.strip()
    if not city:
        return "Which city should I check the weather for?"

    # If the user just says "India", default to New Delhi
    if city.lower() == "india":
        city = "New Delhi, India"

    try:
        # 1. Geocoding city -> lat/lon using Nominatim (Free)
        geo_url = "https://nominatim.openstreetmap.org/search"
        geo_params = {"q": city, "format": "json", "limit": 1}
        geo_headers = {"User-Agent": "BethAssistant/1.0"}

        geo_res = requests.get(geo_url, params=geo_params, headers=geo_headers, timeout=5)
        geo_res.raise_for_status()
        geo_data = geo_res.json()

        if not geo_data:
            return f"I couldn't find the coordinates for {city}."

        lat = geo_data[0]["lat"]
        lon = geo_data[0]["lon"]
        display_name = geo_data[0].get("display_name", city).split(',')[0]

        # 2. Get weather from Open-Meteo (Free)
        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": "true",
            "temperature_unit": "celsius",
            "windspeed_unit": "kmh"
        }

        weather_res = requests.get(weather_url, params=weather_params, timeout=5)
        weather_res.raise_for_status()
        weather_data = weather_res.json()

        current = weather_data.get("current_weather", {})
        temp = current.get("temperature")
        wind = current.get("windspeed")

        if temp is None:
            return f"I found {display_name}, but I couldn't get the current temperature."

        return f"The current temperature in {display_name} is {temp} degrees Celsius with a wind speed of {wind} kilometers per hour."

    except Exception as e:
        print(f"[Beth Weather] Error: {e}")
        return f"I had trouble fetching the weather for {city}."

def get_news(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        topic = "latest news"

    # Since we don't have an API key for NewsAPI, we'll provide a curated
    # Google News search link or a similar approach.
    # However, to keep it "voice-like", we can't just "open a link" if
    # the user wants Beth to "tell" them.
    # For a true voice experience without a key, we can use a public RSS feed.
    # But for simplicity and reliability, we will perform a web search
    # for the news and tell the user we've opened it for them,
    # OR if we want to "read" news, we'd need a scraper.
    # Let's implement a "search and open" approach for news.

    encoded_topic = urllib.parse.quote_plus(topic)
    url = f"https://www.google.com/search?q={encoded_topic}+news"

    try:
        chrome_path = find_chrome_path()
        if chrome_path:
            subprocess.Popen([chrome_path, url], shell=False)
            return f"I've opened the latest news about {topic} in Chrome for you."
        else:
            webbrowser.open(url)
            return f"I've opened the latest news about {topic} for you."
    except Exception as e:
        return f"I couldn't fetch the news for {topic}. {e}"


# ---------------------------------------------------------------------
# GOOGLE CALENDAR (read-only, via your calendar's secret iCal address)
# ---------------------------------------------------------------------

def _load_calendar_config() -> dict:
    if not os.path.exists(CALENDAR_FILE):
        return {}
    try:
        with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[Beth Calendar] Config error: {e}")
    return {}


def _fetch_ical(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"[Beth Calendar] HTTP {resp.status_code}")
            return None
        return resp.text
    except Exception as e:
        print(f"[Beth Calendar] Fetch error: {e}")
        return None


def _ical_lines(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Lines starting with space/tab are continuations of the previous line.
        if raw.startswith((" ", "\t")):
            if lines:
                lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    is_utc = value.endswith("Z")
    if is_utc:
        value = value[:-1]
    dt = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if is_utc:
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


def _parse_events(text: str) -> list[dict]:
    events = []
    cur = None
    for line in _ical_lines(text):
        if line.startswith("BEGIN:VEVENT"):
            cur = {"all_day": False}
        elif line.startswith("END:VEVENT"):
            if cur and cur.get("dtstart"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, _, value = line.partition(":")
            key = head.split(";")[0].upper()
            params = dict(
                p.split("=", 1)
                for p in head.split(";")[1:]
                if "=" in p
            )
            if key == "DTSTART":
                cur["all_day"] = params.get("VALUE") == "DATE"
                cur["dtstart"] = _parse_dt(value)
            elif key in ("SUMMARY", "LOCATION", "UID"):
                cur[key.lower()] = value.strip()
            elif key == "RRULE":
                cur["rrule"] = value.strip()
            elif key == "EXDATE":
                cur.setdefault("exdate", []).extend(
                    d for d in (_parse_dt(v) for v in value.split(",")) if d
                )
    return events


def _expand_occurrences(event: dict, start: datetime, end: datetime) -> list[datetime]:
    """Expand a (possibly recurring) event into its upcoming start times."""
    dtstart = event.get("dtstart")
    if not dtstart:
        return []
    all_day = bool(event.get("all_day"))

    rrule = event.get("rrule")
    if not rrule:
        if all_day and start.date() <= dtstart.date() <= end.date():
            return [dtstart]
        if not all_day and start <= dtstart <= end:
            return [dtstart]
        return []

    props = {}
    for token in rrule.split(";"):
        if "=" in token:
            k, _, v = token.partition("=")
            props[k.upper()] = v

    freq = props.get("FREQ", "DAILY")
    if freq not in ("DAILY", "WEEKLY"):
        # ponytail: only daily/weekly recurrences are expanded; a MONTHLY or
        # YEARLY event surfaces just its first instance. Add real expansion if
        # those matter to you.
        if all_day and start.date() <= dtstart.date() <= end.date():
            return [dtstart]
        if not all_day and start <= dtstart <= end:
            return [dtstart]
        return []

    try:
        interval = max(1, int(props.get("INTERVAL", 1)))
    except ValueError:
        interval = 1
    until = _parse_dt(props["UNTIL"]) if props.get("UNTIL") else None

    weekdays = None
    if freq == "WEEKLY" and props.get("BYDAY"):
        days = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        weekdays = [days[d] for d in props["BYDAY"].split(",") if d in days]

    excludes = set(event.get("exdate") or [])
    step = timedelta(days=interval) if freq == "DAILY" else timedelta(weeks=interval)

    occurs = []
    occ = dtstart
    while occ <= end and (until is None or occ <= until) and len(occurs) < 60:
        if weekdays is None or occ.weekday() in weekdays:
            in_window = (
                start.date() <= occ.date() <= end.date()
                if all_day
                else start <= occ <= end
            )
            if in_window and occ not in excludes:
                occurs.append(occ)
        occ += step
    return occurs


def _describe_occurrence(occ: datetime, event: dict, with_day: bool = True) -> str:
    name = event.get("summary") or "an event"
    if event.get("all_day"):
        if not with_day:
            return name
        now_date = datetime.now().date()
        day = occ.date()
        if day == now_date:
            return f"{name}, all day today"
        if day == now_date + timedelta(days=1):
            return f"{name}, all day tomorrow"
        return f"{name}, all day {occ.strftime('%A')}"
    clock = occ.strftime("%I:%M").lstrip("0")
    period = occ.strftime("%p").lower()
    text = f"{name} at {clock} {period}"
    if with_day:
        now_date = datetime.now().date()
        day = occ.date()
        if day == now_date:
            day_word = "today"
        elif day == now_date + timedelta(days=1):
            day_word = "tomorrow"
        else:
            day_word = occ.strftime("%A")
        text += f" {day_word}"
    loc = event.get("location")
    if loc:
        text += f", in {loc}"
    return text


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _scope_window(scope: str, now: datetime):
    """Resolve a spoken day/date ("tomorrow", "friday", "december 5", "the 25th",
    "next week") into (start, end, label), or None when no day is mentioned."""
    scope = (scope or "").strip().lower().rstrip(".")
    if not scope:
        return None

    if scope == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), "Today"
    if scope == "tomorrow":
        d = (now + timedelta(days=1)).date()
        start = datetime(d.year, d.month, d.day)
        return start, start + timedelta(days=1), "Tomorrow"
    if "next week" in scope:
        days_to_monday = (0 - now.weekday() + 7) % 7 or 7
        d = (now + timedelta(days=days_to_monday)).date()
        start = datetime(d.year, d.month, d.day)
        return start, start + timedelta(days=7), "Next week"

    m = re.search(
        r"\b(?:next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        scope,
    )
    if m:
        wd = m.group(1)
        days_ahead = (_WEEKDAYS[wd] - now.weekday() + 7) % 7
        d = (now + timedelta(days=days_ahead)).date()
        start = datetime(d.year, d.month, d.day)
        return start, start + timedelta(days=1), wd.capitalize()

    # "december 5", "5 december", "december 5th"
    for pat in (
        r"([a-z]+)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?$",
        r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)$",
    ):
        mt = re.match(pat, scope)
        if not mt:
            continue
        a, b = mt.groups()
        if a in _MONTHS and b.isdigit():
            month, day = _MONTHS[a], int(b)
        elif b in _MONTHS and a.isdigit():
            month, day = _MONTHS[b], int(a)
        else:
            continue
        try:
            start = datetime(now.year, month, day)
        except ValueError:
            start = None
        if start is None or (start.date() < now.date()):
            try:
                start = datetime(now.year + 1, month, day)
            except ValueError:
                return None
        label = f"{start.strftime('%B')} {start.day}"
        return start, start + timedelta(days=1), label

    # "the 25th" — next occurrence of that day-of-month
    mo = re.match(r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)$", scope)
    if mo:
        day = int(mo.group(1))
        try:
            start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            start = None
        if start is None or start.date() <= now.date():
            month = now.month + 1 if now.month < 12 else 1
            year = now.year if now.month < 12 else now.year + 1
            try:
                start = datetime(year, month, day)
            except ValueError:
                return None
        return start, start + timedelta(days=1), f"the {start.day}"

    return None


def _calendar_urls(config: dict) -> list[str]:
    """Support one or more calendars: 'ical_urls' (list) or legacy 'ical_url'."""
    urls = []
    raw = config.get("ical_urls") or config.get("ical_url") or ""
    if isinstance(raw, str):
        urls = [raw]
    elif isinstance(raw, list):
        urls = [u for u in raw if isinstance(u, str)]
    return [u.strip() for u in urls if u.strip() and not u.lower().startswith("paste")]


def _fetch_events(urls: list[str]) -> list[dict]:
    """Fetch and merge events from several calendars, de-duplicated by UID."""
    seen = set()
    merged = []
    for url in urls:
        ical = _fetch_ical(url)
        if ical is None:
            continue
        for event in _parse_events(ical):
            uid = event.get("uid")
            if uid and uid in seen:
                continue
            if uid:
                seen.add(uid)
            merged.append(event)
    return merged


def get_calendar(scope: str = "") -> str:
    config = _load_calendar_config()
    urls = _calendar_urls(config)
    if not urls:
        return (
            "I don't have your calendar link yet. "
            "Paste your secret iCal address into beth_calendar.json."
        )

    events = _fetch_events(urls)
    if not events:
        return (
            "I could reach your calendar, but I didn't find any events in it. "
            "If your classes are on a different Google Calendar, add its "
            "secret iCal address to beth_calendar.json."
        )

    try:
        lookahead = int(config.get("lookahead_days", 14) or 14)
    except (TypeError, ValueError):
        lookahead = 14
    try:
        limit = min(int(config.get("limit", 5) or 5), 20)
    except (TypeError, ValueError):
        limit = 5

    scope = (scope or "").strip()
    window = _scope_window(scope, datetime.now())
    if window:
        start, end, label = window
        limit = max(limit, 12)

        upcoming = []
        for event in events:
            for occ in _expand_occurrences(event, start, end):
                upcoming.append((occ, event))
        upcoming.sort(key=lambda x: x[0])

        if not upcoming:
            return f"You don't have anything on {label}."
        text = f"{label}: " + ". ".join(
            _describe_occurrence(o, e, with_day=False)
            for o, e in upcoming[:limit]
        ) + "."
        if len(upcoming) > limit:
            text += f" And {len(upcoming) - limit} more after that."
        return text

    start = datetime.now()
    end = start + timedelta(days=lookahead)

    upcoming = []
    for event in events:
        for occ in _expand_occurrences(event, start, end):
            upcoming.append((occ, event))
    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        return f"You don't have any upcoming events in the next {lookahead} days."

    total = len(upcoming)
    text = (
        f"You have {total} upcoming event{'s' if total != 1 else ''} "
        f"in the next {lookahead} days. "
    )
    text += ". ".join(_describe_occurrence(o, e) for o, e in upcoming[:limit]) + "."
    if total > limit:
        text += f" And {total - limit} more after that."
    return text


CHROME_CANDIDATES = [
    os.path.expandvars(
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    ),
    os.path.expandvars(
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    ),
    os.path.expandvars(
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe"
    ),
]


def find_chrome_path() -> str | None:

    for path in CHROME_CANDIDATES:
        if path and os.path.isfile(path):
            return path

    try:
        result = subprocess.run(
            ["where", "chrome.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                path = line.strip()
                if path and os.path.isfile(path):
                    return path
    except Exception as e:
        print(f"Chrome search error: {e}")

    return None


def open_chrome_profile(profile_identifier: str) -> str:
    """
    Open a specific Chrome profile by name or number.
    """
    profile_identifier = str(profile_identifier or "").strip()
    if not profile_identifier:
        return "I need to know which Chrome profile to open."

    profile, profiles = _chrome_profile_by_name_or_number(profile_identifier)

    if profile is None:
        if not profiles:
            return "I couldn't find your Chrome profiles."
        return f"I couldn't find Chrome profile '{profile_identifier}'."

    chrome_path = find_chrome_path()
    if not chrome_path:
        return "I couldn't find Chrome installed."

    try:
        # Activate window if it's already open
        hwnd = _find_chrome_window_for_profile(profile["directory"])
        if hwnd:
            _activate_window(hwnd)
            time.sleep(0.15)

        # Launch the profile
        subprocess.Popen(
            [
                chrome_path,
                f'--profile-directory={profile["directory"]}',
            ],
            shell=False,
        )
        return f"Opening Chrome profile: {profile['name']}."
    except Exception as e:
        return f"I couldn't open Chrome profile {profile_identifier}. {e}"

CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Google",
    "Chrome",
    "User Data",
)


def _chrome_profiles():
    """Return Chrome profiles in Chrome's displayed profile order."""
    local_state = os.path.join(CHROME_USER_DATA, "Local State")

    try:
        with open(local_state, "r", encoding="utf-8") as f:
            data = json.load(f)

        info_cache = data.get("profile", {}).get("info_cache", {})
        profiles = []

        for directory, info in info_cache.items():
            if not isinstance(info, dict):
                continue

            profiles.append({
                "directory": directory,
                "name": str(info.get("name", directory)),
            })

        return profiles
    except Exception as e:
        print(f"[Beth Chrome] Could not read Chrome profiles: {e}")
        return []


def _chrome_profile_by_name_or_number(identifier: str):
    profiles = _chrome_profiles()

    # Try to parse as a number first (index)
    try:
        idx = int(identifier)
        if 1 <= idx <= len(profiles):
            return profiles[idx - 1], profiles
    except ValueError:
        pass

    # Search by name (case-insensitive)
    for profile in profiles:
        if identifier.lower() in profile["name"].lower():
            return profile, profiles

    return None, profiles


def _window_process_id(hwnd):
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(
        wintypes.HWND(hwnd),
        ctypes.byref(pid)
    )
    return int(pid.value)


def _is_real_visible_window(hwnd):
    user32 = ctypes.windll.user32
    return bool(
        user32.IsWindowVisible(hwnd)
        and user32.IsWindow(hwnd)
    )


def _find_chrome_window_for_profile(profile_directory: str):
    """
    Find a visible top-level Chrome window whose Chrome process command line
    contains the requested --profile-directory.
    """
    user32 = ctypes.windll.user32
    windows = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    def callback(hwnd, lparam):
        if _is_real_visible_window(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)

    for hwnd in windows:
        pid = _window_process_id(hwnd)

        try:
            proc = psutil.Process(pid)
            if proc.name().lower() != "chrome.exe":
                continue

            cmdline = " ".join(proc.cmdline()).lower()
            wanted = f"--profile-directory={profile_directory}".lower()

            if wanted in cmdline:
                return hwnd

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    return None


def _activate_window(hwnd) -> bool:
    """Bring a Windows window to the foreground."""
    if not hwnd:
        return False

    user32 = ctypes.windll.user32

    SW_RESTORE = 9
    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)

    current_thread = user32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(
        wintypes.HWND(hwnd), None
    )

    attached = False

    if target_thread and target_thread != current_thread:
        attached = bool(
            user32.AttachThreadInput(
                current_thread,
                target_thread,
                True,
            )
        )

    try:
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        return True
    finally:
        if attached:
            user32.AttachThreadInput(
                current_thread,
                target_thread,
                False,
            )


def search_chrome_profile(profile_identifier: str, query: str) -> str:
    """
    Search in the requested Chrome profile.

    If its window exists, activate it first.
    If it doesn't, launch that exact profile.
    The URL is passed directly to chrome.exe, so Edge is never involved.
    """
    query = str(query or "").strip()

    if not query:
        return "I need a search query."

    profile, profiles = _chrome_profile_by_name_or_number(profile_identifier)

    if profile is None:
        if not profiles:
            return "I couldn't find your Chrome profiles."

        return (
            f"I couldn't find Chrome profile '{profile_identifier}'. "
            f"You have {len(profiles)} Chrome profile"
            f"{'' if len(profiles) == 1 else 's'}."
        )

    chrome_path = find_chrome_path()

    if not chrome_path:
        return "I couldn't find Chrome installed."

    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}"

    # First try to locate the already-open window for this profile.
    hwnd = _find_chrome_window_for_profile(
        profile["directory"]
    )

    if hwnd:
        _activate_window(hwnd)
        time.sleep(0.15)

    try:
        # Calling the exact chrome.exe with --profile-directory ensures
        # the URL is handled by Chrome, not the default browser/Edge.
        subprocess.Popen(
            [
                chrome_path,
                f'--profile-directory={profile["directory"]}',
                url,
            ],
            shell=False,
        )

        return (
            f"Searching Google for {query} in your "
            f"Chrome profile: {profile['name']}."
        )

    except Exception as e:
        return f"I couldn't search in Chrome profile {profile_identifier}. {e}"


# ---------------------------------------------------------------------
# APP COMMANDS
# ---------------------------------------------------------------------

APP_COMMANDS = {

    "chrome": "chrome",

    "notepad": "notepad",

    "calculator": "calc",
    "calc": "calc",

    "spotify": "spotify",

    "file explorer": "explorer",
    "explorer": "explorer",

    "paint": "mspaint",

    "word": "winword",

    "excel": "excel",

    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
}


APP_PROCESS_NAMES = {

    "chrome": "chrome.exe",

    "notepad": "notepad.exe",

    "calculator": "CalculatorApp.exe",
    "calc": "CalculatorApp.exe",

    "spotify": "Spotify.exe",

    "paint": "mspaint.exe",

    "word": "WINWORD.EXE",

    "excel": "EXCEL.EXE",

    "vscode": "Code.exe",
    "vs code": "Code.exe",
    "visual studio code": "Code.exe",
}


# ---------------------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------------------

def normalize_target(target: str) -> str:

    target = str(target or "").lower().strip()

    replacements = {
        "google chrome": "chrome",
        "chrome browser": "chrome",

        "windows calculator": "calculator",

        "file explorer": "file explorer",

        "visual studio code": "vscode",
        "visual studio": "vscode",
    }

    return replacements.get(target, target)


# ---------------------------------------------------------------------
# WEBSITE SHORTCUTS
# ---------------------------------------------------------------------

def check_url_shortcuts(text: str) -> str | None:

    text_clean = text.lower().strip()

    # Longer phrases first.
    shortcuts = sorted(
        URL_SHORTCUTS.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    )

    for word, url in shortcuts:

        if word in text_clean:

            try:
                webbrowser.open(url)

                return f"Opening {word}."

            except Exception as e:

                print(f"Shortcut error: {e}")

                return f"I couldn't open {word}."

    return None


# ---------------------------------------------------------------------
# MICROSOFT EDGE (DEPRECATED)
# ---------------------------------------------------------------------
# Edge functionality removed per user request.



# ---------------------------------------------------------------------
# WEBSITE
# ---------------------------------------------------------------------

def perform_web_search(query: str) -> str:
    query = query.strip()
    if not query:
        return "I need a search query."

    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded_query}"

    try:
        chrome_path = find_chrome_path()
        if chrome_path:
            subprocess.Popen([chrome_path, url], shell=False)
            return f"Searching Google for {query} in Chrome."
        else:
            webbrowser.open(url)
            return f"Searching Google for {query}."
    except Exception as e:
        return f"I couldn't perform the web search. {e}"

def open_website(target: str) -> str:

    target = target.strip()

    if not target:

        return "I need a website to open."

    if not target.startswith(
        ("http://", "https://")
    ):

        target = f"https://{target}"

    try:

        chrome_path = find_chrome_path()
        if chrome_path:
            subprocess.Popen(
                [chrome_path, target],
                shell=False,
            )
            return "Opening the website in Chrome."

        webbrowser.open(target)

        return "Opening the website."

    except Exception as e:

        return f"I couldn't open that website. {e}"


# ---------------------------------------------------------------------
# OPEN APP
# ---------------------------------------------------------------------

def open_app(target: str) -> str:

    target = normalize_target(target)

    if not target:

        return "I need to know which app to open."

    # Chrome needs a direct executable path.
    if target == "chrome":

        chrome_path = find_chrome_path()

        if not chrome_path:

            return "I couldn't find Chrome installed."

        try:

            subprocess.Popen(
                [chrome_path],
                shell=False,
            )

            return "Opening Chrome."

        except Exception as e:

            return f"I couldn't open Chrome. {e}"

    # Notepad
    if target == "notepad":

        try:

            subprocess.Popen(
                [
                    "explorer.exe",
                    r"shell:AppsFolder\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
                ]
            )

            return "Opening Notepad."

        except Exception:

            try:

                subprocess.Popen(
                    ["notepad.exe"]
                )

                return "Opening Notepad."

            except Exception as e:

                return f"I couldn't open Notepad. {e}"

    command = APP_COMMANDS.get(target)

    if command is None:

        return f"I don't know how to open {target} yet."

    try:

        subprocess.Popen(
            ["cmd", "/c", "start", "", command],
            shell=False,
        )

        return f"Opening {target}."

    except Exception as e:

        return f"I couldn't open {target}. {e}"


# ---------------------------------------------------------------------
# CLOSE APP
# ---------------------------------------------------------------------

def close_app(target: str) -> str:

    target = normalize_target(target)

    process_name = APP_PROCESS_NAMES.get(target)

    if process_name is None:

        return (
            f"I don't know how to close {target} yet."
        )

    closed_any = False

    for proc in psutil.process_iter(["name"]):

        try:

            name = proc.info["name"]

            if (
                name
                and name.lower()
                == process_name.lower()
            ):

                proc.terminate()
                closed_any = True

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
        ):

            continue

    if closed_any:

        return f"Closed {target}."

    return f"{target} doesn't seem to be running."


# ---------------------------------------------------------------------
# LOCAL FILE SEARCH
# ---------------------------------------------------------------------

def search_local_files(query: str) -> str:
    query = query.lower().strip()

    if not query:
        return "I need a file name to search for."

    matches = []

    for search_dir in SEARCH_DIRECTORIES:
        if not os.path.exists(search_dir):
            continue
        try:
            for root, dirs, files in os.walk(search_dir):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if query in file.lower():
                        full_path = os.path.join(root, file)
                        try:
                            # Get file size and modified time for better sorting
                            stats = os.stat(full_path)
                            matches.append({
                                "name": file,
                                "path": full_path,
                                "mtime": stats.st_mtime,
                                "size": stats.st_size
                            })
                        except (OSError, PermissionError):
                            continue

                if len(matches) >= 10: # Collect a few more to sort by date
                    break
        except Exception as e:
            print(f"Error searching in {search_dir}: {e}")

    if not matches:
        return f"I couldn't find any files matching {query}."

    # Sort matches by modified time (newest first)
    matches.sort(key=lambda x: x["mtime"], reverse=True)

    # Limit to top 3 for the voice response
    top_matches = matches[:3]

    if len(top_matches) == 1:
        file = top_matches[0]["name"]
        path = top_matches[0]["path"]
        return f"I found {file} at {path}."

    names = ", ".join(item["name"] for item in top_matches)
    return f"I found {len(top_matches)} matching files, most recently: {names}."


# =====================================================================
# TIMER SYSTEM
# =====================================================================

def _now() -> float:

    return time.time()


def _format_duration(seconds: int) -> str:

    seconds = max(0, int(seconds))

    if seconds < 60:

        return (
            f"{seconds} second"
            f"{'' if seconds == 1 else 's'}"
        )

    minutes, remaining = divmod(
        seconds,
        60,
    )

    if minutes < 60:

        text = (
            f"{minutes} minute"
            f"{'' if minutes == 1 else 's'}"
        )

        if remaining:

            text += (
                f" and {remaining} second"
                f"{'' if remaining == 1 else 's'}"
            )

        return text

    hours, minutes = divmod(
        minutes,
        60,
    )

    text = (
        f"{hours} hour"
        f"{'' if hours == 1 else 's'}"
    )

    if minutes:

        text += (
            f" and {minutes} minute"
            f"{'' if minutes == 1 else 's'}"
        )

    return text


def _load_timer_file() -> list:

    if not os.path.exists(TIMER_FILE):

        return []

    try:

        with open(
            TIMER_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

            if isinstance(data, list):

                return data

    except Exception as e:

        print(
            f"WARNING: Could not load timer file: {e}"
        )

    return []


def _save_timer_file(data: list):

    temp_file = TIMER_FILE + ".tmp"

    try:

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                indent=2,
            )

        os.replace(
            temp_file,
            TIMER_FILE,
        )

    except Exception as e:

        print(
            f"WARNING: Could not save timers: {e}"
        )


def _generate_timer_id() -> str:

    return str(
        int(time.time() * 1000)
    )


def _remove_timer_from_disk(timer_id: str):

    with timer_lock:

        timers = _load_timer_file()

        timers = [
            timer
            for timer in timers
            if timer.get("id") != timer_id
        ]

        _save_timer_file(timers)


def _fire_timer(timer_id: str):
    print(f"[Beth Timer] Firing timer: {timer_id}")

    with timer_lock:

        timer = active_timers.pop(
            timer_id,
            None,
        )

    if not timer:
        return

    _remove_timer_from_disk(timer_id)

    label = timer.get(
        "label",
        "",
    )

    if label:

        _speak_timer_message(f"Your {label} timer is up.")

    else:

        _speak_timer_message("Your timer is up.")


def _fire_pre_alert(timer_id: str):
    print(f"[Beth Timer] Firing pre-alert: {timer_id}")
    with timer_lock:
        timer = active_timers.get(timer_id)

    if not timer:
        return

    label = timer.get("label", "timer")
    message = timer.get("message", "")

    if message:
        _speak_timer_message(f"Just a heads up, your reminder to {message} is coming up in one minute.")
    else:
        _speak_timer_message(f"Just a heads up, your {label} timer is almost up.")

def _fire_reminder(timer_id: str):
    print(f"[Beth Timer] Firing reminder: {timer_id}")

    with timer_lock:

        timer = active_timers.pop(
            timer_id,
            None,
        )

    if not timer:
        return

    _remove_timer_from_disk(timer_id)

    message = timer.get(
        "message",
        "your reminder",
    )

    _speak_timer_message(f"Reminder: {message}")


def _schedule_timer(timer: dict):

    timer_id = timer["id"]

    remaining = (
        timer["fire_at"] - _now()
    )

    # If Beth was restarted after the timer should
    # have fired, fire it shortly after startup.
    remaining = max(
        0.5,
        remaining,
    )

    if timer.get("type") == "timer":

        callback = _fire_timer

    else:

        callback = _fire_reminder

    thread = threading.Timer(
        remaining,
        callback,
        args=(timer_id,),
    )

    thread.daemon = True

    with timer_lock:

        active_timers[timer_id] = timer

    thread.start()


def _restore_timers():

    timers = _load_timer_file()

    if not timers:
        return

    restored = []

    now = _now()

    for timer in timers:

        try:

            fire_at = float(
                timer["fire_at"]
            )

            timer["fire_at"] = fire_at

            # Future timers are restored normally.
            if fire_at > now:

                restored.append(timer)
                _schedule_timer(timer)

            else:

                # Overdue timers are fired after Beth starts.
                restored.append(timer)

                _schedule_timer(timer)

        except Exception as e:

            print(
                f"WARNING: Invalid saved timer: {e}"
            )

    print(
        f"Restored {len(restored)} timer/reminder(s)."
    )


def _create_timer(
    timer_type: str,
    target: str,
    duration_seconds: int,
) -> str:

    if duration_seconds <= 0:

        return "I need a valid duration."

    if duration_seconds > 7 * 24 * 60 * 60:

        return (
            "For now, timers and reminders "
            "can be up to seven days."
        )

    timer_id = _generate_timer_id()

    now = _now()

    timer = {
        "id": timer_id,
        "type": timer_type,
        "label": (
            target
            if timer_type == "timer"
            else ""
        ),
        "message": (
            target
            if timer_type == "reminder"
            else ""
        ),
        "created_at": now,
        "fire_at": now + duration_seconds,
    }

    with timer_lock:

        timers = _load_timer_file()

        timers.append(timer)

        _save_timer_file(timers)

    _schedule_timer(timer)

    # Proactive Alert: Schedule a pre-alert 60 seconds before expiry if duration is > 60s
    if duration_seconds > 60:
        pre_alert_delay = duration_seconds - 60
        pre_alert_thread = threading.Timer(
            pre_alert_delay,
            _fire_pre_alert,
            args=(timer_id,),
        )
        pre_alert_thread.daemon = True
        pre_alert_thread.start()

    duration_text = _format_duration(
        duration_seconds
    )

    if timer_type == "timer":

        if target:

            return (
                f"Timer set for {duration_text} "
                f"for {target}."
            )

        return (
            f"Timer set for {duration_text}."
        )

    return (
        f"Okay, I'll remind you in "
        f"{duration_text} to {target}."
    )


def set_timer(
    target: str,
    duration_seconds: int,
) -> str:

    return _create_timer(
        "timer",
        target.strip(),
        duration_seconds,
    )


def set_reminder(
    target: str,
    duration_seconds: int,
) -> str:

    target = target.strip()

    if not target:

        return (
            "I need to know what you want "
            "to be reminded about."
        )

    return _create_timer(
        "reminder",
        target,
        duration_seconds,
    )


# ---------------------------------------------------------------------
# TIMER LIST
# ---------------------------------------------------------------------

def list_timers() -> str:

    timers = _load_timer_file()

    if not timers:

        return "You don't have any active timers or reminders."

    now = _now()

    active = []

    for timer in timers:

        remaining = int(
            timer["fire_at"] - now
        )

        if remaining < 0:
            remaining = 0

        active.append(
            (
                timer,
                remaining,
            )
        )

    if not active:

        return "You don't have any active timers or reminders."

    responses = []

    for timer, remaining in active:

        duration = _format_duration(
            remaining
        )

        if timer["type"] == "timer":

            label = timer.get(
                "label",
                "",
            )

            if label:

                responses.append(
                    f"{label}, in {duration}"
                )

            else:

                responses.append(
                    f"timer, in {duration}"
                )

        else:

            message = timer.get(
                "message",
                "",
            )

            responses.append(
                f"reminder to {message}, "
                f"in {duration}"
            )

    return (
        "You have "
        + str(len(responses))
        + " active item"
        + ("" if len(responses) == 1 else "s")
        + ": "
        + "; ".join(responses)
        + "."
    )


# ---------------------------------------------------------------------
# CANCEL TIMER / REMINDER
# ---------------------------------------------------------------------

def cancel_timer(target: str) -> str:

    target = target.lower().strip()

    with timer_lock:

        timers = _load_timer_file()

        if not timers:

            return (
                "You don't have any active "
                "timers or reminders."
            )

        matching = []

        for timer in timers:

            label = (
                timer.get("label", "")
                or timer.get("message", "")
            ).lower()

            if not target:

                matching.append(timer)

            elif target in label:

                matching.append(timer)

    if not matching:

        return (
            f"I couldn't find a timer or reminder "
            f"matching {target}."
        )

    # If no target was given and there is exactly
    # one item, cancel it.
    if not target and len(matching) > 1:

        return (
            "You have multiple timers or reminders. "
            "Tell me which one you want to cancel."
        )

    timer = matching[0]

    timer_id = timer["id"]

    with timer_lock:

        active_timers.pop(
            timer_id,
            None,
        )

        timers = _load_timer_file()

        timers = [
            t
            for t in timers
            if t.get("id") != timer_id
        ]

        _save_timer_file(timers)

    label = (
        timer.get("label")
        or timer.get("message")
        or "timer"
    )

    return f"Cancelled {label}."


def _press_key(hex_code):
    """Simulate a keyboard press and release."""
    ctypes.windll.user32.keybd_event(hex_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(hex_code, 0, ctypes.windll.user32.KEYEVENTF_KEYUP, 0)

def volume_up() -> str:
    """Increase system volume."""
    _press_key(0xAF)
    return "Turning the volume up."

def volume_down() -> str:
    """Decrease system volume."""
    _press_key(0xAE)
    return "Turning the volume down."

def volume_mute() -> str:
    """Mute or unmute system audio."""
    _press_key(0xAD)
    return "Toggling mute."

def _load_macros() -> dict:
    if not os.path.exists(MACRO_FILE):
        # Default macros
        return {
            "work": [
                {"action": "open_app", "target": "chrome"},
                {"action": "open_app", "target": "vscode"},
                {"action": "open_app", "target": "spotify"},
            ],
            "relax": [
                {"action": "open_app", "target": "chrome"},
                {"action": "open_site", "target": "youtube.com"},
            ],
            "study": [
                {"action": "open_site", "target": "https://coach.mastersunion.org/"},
            ],
        }
    try:
        with open(MACRO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[Beth Macro] Load error: {e}")
    return {}

def _save_macros(data: dict):
    try:
        with open(MACRO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Beth Macro] Save error: {e}")

def run_macro(target: str) -> str:
    """Execute a predefined set of actions."""
    target = target.strip().lower().replace(" mode", "")
    macros = _load_macros()

    if target not in macros:
        return f"I don't have a macro defined for {target} mode yet."

    steps = macros[target]
    # We reuse execute_steps to handle the list of actions
    return execute_steps(steps, raw_text=f"Running {target} macro")

def _load_subjects() -> dict:
    if not os.path.exists(SUBJECTS_FILE):
        return {}
    try:
        with open(SUBJECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[Beth Subjects] Load error: {e}")
    return {}

def study_subject(subject: str) -> str:
    """Open the LMS and the book for a specific subject."""
    subject = subject.strip().lower()
    if not subject:
        return "Which subject would you like to study?"

    # 1. Always open the LMS
    lms_url = "https://coach.mastersunion.org/"
    webbrowser.open(lms_url)

    # 2. Look up the subject book/resource
    subjects = _load_subjects()
    book_url = subjects.get(subject)

    if book_url:
        webbrowser.open(book_url)
        return f"I've opened the LMS and your {subject} resources. Happy studying!"
    else:
        return f"I've opened the LMS for you, but I don't have a specific book for {subject} yet."

# ---------------------------------------------------------------------
# ACTION EXECUTION
# ---------------------------------------------------------------------

def get_clock(kind: str = "time") -> str:
    now = datetime.now()
    if kind == "date":
        return f"Today is {now.strftime('%A, %B')} {now.day}, {now.year}."
    hour = now.hour % 12 or 12
    minute = now.minute
    period = now.strftime("%p").lower()
    return f"It's {hour}:{minute:02d} {period}."


def calculate(target: str) -> str:
    expr = (target or "").strip()
    if not expr or len(expr) > 80 or not re.fullmatch(r"[0-9a-zA-Z+\-*/().%^ ]+", expr):
        return "I can only do simple math. Try something like what is forty two times seven."
    try:
        cleaned = expr.replace("^", "**")
        if not re.match(r"^[0-9a-zA-Z+\-*/().** ]+$", cleaned):
            return "I can only do simple math. Try something like what is forty two times seven."
        val = eval(cleaned, {"__builtins__": {}},
                   {"sqrt": math.sqrt, "abs": abs, "round": round,
                    "min": min, "max": max, "pi": math.pi, "math": math})
    except Exception:
        return "I couldn't compute that. Try something like what is forty two times seven."
    if isinstance(val, bool):
        return "That's true." if val else "That's false."
    if isinstance(val, (int, float)):
        return f"That's {val:g}."
    return f"That's {val}."


_NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aether_notes.txt")


def save_note(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text or len(text) > 200:
        return "I couldn't write that note. Keep it to a sentence or two."
    with open(_NOTES_PATH, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    return "Noted."


def read_notes() -> str:
    try:
        with open(_NOTES_PATH, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        lines = []
    if not lines:
        return "You don't have any notes yet. Say note that, and something to save."
    notes = lines[-10:]
    return "Your notes: " + "  ".join(notes) + "."


def get_battery() -> str:
    try:
        bat = psutil.sensors_battery()
    except (AttributeError, NotImplementedError):
        return "I can't read the battery on this machine."
    if bat is None:
        return "I can't read the battery on this machine."
    pct = int(bat.percent)
    if bat.power_plugged:
        return f"Battery is at {pct} percent and charging."
    text = f"Battery is at {pct} percent, running on battery"
    if bat.secondsleft and bat.secondsleft > 60:
        text += f", about {int(bat.secondsleft / 60)} minutes left"
    return text + "."


def execute_action(
    step,
    raw_text: str = "",
    history: list = None,
) -> str:
    """
    Execute ONE step.

    Also accepts a list for backward compatibility with
    Phase 4's older call pattern.
    """

    # Backward compatibility:
    # if Phase 4 gives us a list, execute the list.
    if isinstance(step, list):

        return execute_steps(
            step,
            raw_text=raw_text,
        )

    if not isinstance(step, dict):
        return generate_chat_response(
            raw_text,
            history=history
        )

    action = step.get(
        "action",
        "unknown",
    )

    target = str(
        step.get(
            "target",
            "",
        )
        or ""
    ).strip()

    value = str(
        step.get(
            "value",
            "",
        )
        or ""
    ).strip()

    try:

        duration_seconds = int(
            step.get(
                "duration_seconds",
                0,
            )
            or 0
        )

    except (
        TypeError,
        ValueError,
    ):

        duration_seconds = 0

    if action == "open_app":

        return open_app(target)

    if action == "close_app":

        return close_app(target)

    if action == "close_current_app":
        return close_current_app()

    if action == "identify_app":
        return identify_app()

    if action == "open_site":

        return open_website(target)

    if action == "search_files":

        return search_local_files(target)

    if action == "web_search":

        return perform_web_search(target)

    if action == "chrome_profile_search":
        profile_id = step.get("profile_identifier", "")
        if not profile_id:
            # Fallback: if the model put the profile in the target, we can't easily
            # separate the query. But we'll try to use a default or ask.
            return "I know you want to search, but I'm not sure which Chrome profile to use."

        return search_chrome_profile(
            profile_id,
            target,
        )

    if action == "open_chrome_profile":
        return open_chrome_profile(target)

    if action == "change_voice":
        return change_voice(target)

    if action == "add_todo":
        return add_todo(target)

    if action == "list_todo":
        return list_todo()

    if action == "remove_todo":
        return remove_todo(target)

    if action == "get_weather":
        return get_weather(target)

    if action == "get_news":
        return get_news(target)

    if action == "get_calendar":
        return get_calendar(target)

    if action == "system_health":
        return get_system_health()

    if action == "load_document":
        return load_document(target)

    if action == "ask_document":
        return ask_document(target)

    if action == "volume_up":
        return volume_up()

    if action == "volume_down":
        return volume_down()

    if action == "volume_mute":
        return volume_mute()

    if action == "run_macro":
        return run_macro(target)

    if action == "study_subject":
        return study_subject(target)

    if action == "remember_fact":
        return remember_fact(target, value)

    if action == "recall_fact":
        return recall_fact(target)

    if action == "set_timer":

        return set_timer(
            target,
            duration_seconds,
        )

    if action == "set_reminder":

        return set_reminder(
            target,
            duration_seconds,
        )

    if action == "list_timers":

        return list_timers()

    if action == "cancel_timer":

        return cancel_timer(target)

    if action == "get_clock":
        return get_clock(target or "time")

    if action == "calculate":
        return calculate(target)

    if action == "save_note":
        return save_note(target)

    if action == "read_notes":
        return read_notes()

    if action == "get_battery":
        return get_battery()

    if action == "chat":
        return generate_chat_response(
            target
            if target
            else raw_text,
            history=history
        )

    if action == "unknown":
        if target == "timeout_error":
            return "My brain is taking a bit too long to think. Could you please repeat that?"
        if target == "connection_error":
            return "I'm having trouble connecting to my brain. Is Ollama running?"
        if target == "general_error":
            return "Something went wrong while I was thinking. Try again?"
        return "I'm not sure how to do that yet."


# ---------------------------------------------------------------------
# MULTI-STEP EXECUTION
# ---------------------------------------------------------------------

def execute_steps(
    steps: list[dict],
    raw_text: str = "",
    history: list = None,
) -> str:
    """
    Execute multiple actions sequentially.

    Example:
    [
        {"action": "open_app", "target": "chrome"},
        {"action": "web_search", "target": "AI news"},
        {"action": "open_app", "target": "spotify"}
    ]

    Each step is executed in order.
    """

    if not isinstance(steps, list):

        return execute_action(
            steps,
            raw_text=raw_text,
        )

    if not steps:

        return (
            "I couldn't determine what "
            "you wanted me to do."
        )

    # Maximum safety limit.
    steps = steps[:MAX_STEPS]

    # Check shortcuts ONLY when the command is
    # clearly just a shortcut.
    shortcut_response = None

    raw_lower = raw_text.lower().strip()

    if len(steps) == 1:

        shortcut_response = (
            check_url_shortcuts(raw_text)
        )

    if shortcut_response:

        return shortcut_response

    messages = []

    for index, step in enumerate(
        steps,
        start=1,
    ):

        print(
            f"[Beth] Executing step "
            f"{index}/{len(steps)}: {step}"
        )

        try:

            result = execute_action(
                step,
                raw_text=raw_text,
                history=history,
            )

            if result:

                messages.append(result)

        except Exception as e:

            print(
                f"ERROR in step {index}: {e}"
            )

            messages.append(
                f"Step {index} failed."
            )

        # Small delay between actions.
        # This prevents Windows apps from being
        # launched on top of each other too aggressively.
        if index < len(steps):

            time.sleep(0.25)

    if not messages:

        return "I couldn't complete that."

    if len(messages) == 1:

        return messages[0]

    # Natural voice response.
    return " ".join(messages)


# ---------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------

_restore_timers()


# ---------------------------------------------------------------------
# DIRECT TEST MODE
# ---------------------------------------------------------------------

def main():

    print("=" * 60)
    print("BETH — Phase 3 v3")
    print("=" * 60)
    print("Type a command or 'quit'.\n")

    while True:

        text = input("You: ").strip()

        if text.lower() == "quit":

            break

        from phase2_brain import parse_intent

        steps = parse_intent(text)

        print(
            "\nSteps:"
        )

        for i, step in enumerate(
            steps,
            1,
        ):

            print(
                f"  {i}. {step}"
            )

        print()

        result = execute_steps(
            steps,
            raw_text=text,
        )

        print(
            f"Beth: {result}\n"
        )


def _calendar_self_check():
    sample = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART;VALUE=DATE:20260922\r\n"
        "SUMMARY:Car insurance renewal\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART;TZID=Asia/Kolkata:20260921T090000\r\n"
        "DTEND;TZID=Asia/Kolkata:20260921T093000\r\n"
        "RRULE:FREQ=DAILY;COUNT=3\r\n"
        "SUMMARY:Daily standup\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = _parse_events(sample)
    assert len(events) == 2, events
    assert events[0]["all_day"] is True
    daily = _expand_occurrences(
        events[1],
        datetime(2026, 9, 21, 0, 0),
        datetime(2026, 9, 24, 0, 0),
    )
    assert [o.day for o in daily] == [21, 22, 23], daily
    print("calendar parser self-check OK")


def _features_self_check():
    from phase2_brain import _absolute_time, _deterministic_calc, _deterministic_clock, _deterministic_notes

    clock = _deterministic_clock("what's the date")
    assert clock and clock[0]["target"] == "date", clock
    assert _deterministic_clock("what time is it")[0]["target"] == "time"

    calc = _deterministic_calc("what is 15% of 200")
    assert calc, calc
    assert calculate(calc[0]["target"]) == "That's 30."
    assert calculate("sqrt(144)") == "That's 12."
    assert calculate("2 + 2") == "That's 4."
    assert calculate("import os") != "That's 4."  # sandboxed

    notes = _deterministic_notes("note that the assignment is due friday")
    assert notes and notes[0]["target"] == "the assignment is due friday", notes

    global _NOTES_PATH
    orig_notes = _NOTES_PATH
    _NOTES_PATH = os.path.join(os.path.dirname(_NOTES_PATH), "_selfcheck_notes.txt")
    try:
        save_note("self-check note")
        assert read_notes().startswith("Your notes:")
    finally:
        _NOTES_PATH = orig_notes

    abs_t = _absolute_time("remind me at 10 pm")
    assert abs_t and 0 < abs_t[0] < 24 * 3600, abs_t
    print("features self-check OK")


if __name__ == "__main__":
    if sys.argv[1:2] == ["selfcheck"]:
        _calendar_self_check()
        _features_self_check()
    else:
        main()
