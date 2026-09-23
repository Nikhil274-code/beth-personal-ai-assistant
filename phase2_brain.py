"""
BETH — Phase 2 v3.1: Reliable intent parser
--------------------------------------------
- Returns a LIST of steps every time.
- Timers/reminders are parsed deterministically instead of relying on the LLM.
- Supports multi-step commands through Ollama.
- Accepts Ollama responses shaped as {"steps":[...]} OR a single {"action":...}.
- Keeps chat generation local through Ollama.
"""

import json
import os
import re
import requests
from datetime import datetime, timedelta

ASSISTANT_NAME = "Beth"
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_URL = "http://localhost:11434/api/generate"
MAX_STEPS = 8

SUPPORTED_ACTIONS = [
    "open_app",
    "close_app",
    "close_current_app",
    "identify_app",
    "open_site",
    "web_search",
    "chrome_profile_search",
    "open_chrome_profile",
    "search_files",
    "set_timer",
    "set_reminder",
    "list_timers",
    "cancel_timer",
    "change_voice",
    "add_todo",
    "list_todo",
    "remove_todo",
    "get_weather",
    "get_news",
    "remember_fact",
    "recall_fact",
    "system_health",
    "load_document",
    "ask_document",
    "volume_up",
    "volume_down",
    "volume_mute",
    "run_macro",
    "study_subject",
    "get_calendar",
    "get_clock",
    "calculate",
    "save_note",
    "read_notes",
    "get_battery",
    "chat",
    "unknown",
]

# Common spoken number words. This lets "ten seconds" work without asking
# the 3B model to do arithmetic.
NUMBER_WORDS = {
    "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60,
}

def _number_value(value: str):
    value = value.strip().lower()
    if value.isdigit():
        return int(value)
    if value in NUMBER_WORDS:
        return NUMBER_WORDS[value]
    # simple "twenty five", etc.
    parts = value.split()
    if len(parts) == 2 and parts[0] in NUMBER_WORDS and parts[1] in NUMBER_WORDS:
        return NUMBER_WORDS[parts[0]] + NUMBER_WORDS[parts[1]]
    return None

def _duration_seconds(text: str):
    """
    Extract a duration from phrases such as:
      10 seconds, ten seconds, 2 minutes, one hour,
      in 30 seconds, for 5 minutes.
    """
    t = text.lower().strip()

    # Find the unit word, then read backwards up to two tokens as the number
    # ("twenty five minutes", "in ten minutes"). Backwards from the unit avoids
    # absorbing unrelated words ahead of it ("remind me in ten minutes").
    munit = re.search(r"\b(seconds?|secs?|mins?|minutes?|hours?|days?)\b", t)
    if not munit:
        return None

    pre = t[: munit.start()].rstrip()
    tokens = pre.split()[-2:]

    number = None
    raw_number = " ".join(tokens)
    if raw_number.replace(".", "", 1).isdigit():
        number = float(raw_number)
    if number is None:
        number = _number_value(raw_number)
    if number is None:
        for cand in tokens:
            number = _number_value(cand)
            if number is not None:
                break
    if number is None:
        return None

    unit = munit.group(1)
    if unit.startswith("second") or unit.startswith("sec"):
        multiplier = 1
    elif unit.startswith("minute") or unit.startswith("min"):
        multiplier = 60
    elif unit.startswith("hour") or unit.startswith("hr"):
        multiplier = 3600
    else:
        multiplier = 86400

    return max(1, int(number * multiplier))


def _absolute_time(text: str):
    """Seconds from now until a spoken clock time: 'at 3 pm', 'at 15:30',
    'at 9 o'clock', 'at noon'. Returns (seconds, phrase) or None."""
    low = re.sub(r"o'?clock", "", text.lower(), flags=re.I)
    phrase = None
    hour = minute = None
    amp = None

    m = re.search(r"\bat\s+(noon|midnight)\b", low)
    if m:
        phrase = m.group(1)
        hour, minute = (12, 0) if phrase == "noon" else (0, 0)
    else:
        m = re.search(
            r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
            r"|\b(\d{1,2}):(\d{2})\s*(am|pm)\b"
            r"|\b(\d{1,2})\s*(am|pm)\b",
            low,
        )
        if m:
            if m.group(1) is not None:
                hour, minute, amp = int(m.group(1)), int(m.group(2) or 0), m.group(3)
                phrase = "at " + m.group(1) + ((":" + m.group(2)) if m.group(2) else "") + ((" " + amp) if amp else "")
            elif m.group(4) is not None:
                hour, minute, amp = int(m.group(4)), int(m.group(5)), m.group(6)
                phrase = "at " + m.group(4) + ":" + m.group(5) + ((" " + amp) if amp else "")
            elif m.group(7) is not None:
                hour, minute, amp = int(m.group(7)), 0, m.group(8)
                phrase = "at " + m.group(7) + " " + amp
    if hour is None:
        return None

    if amp == "pm" and hour < 12:
        hour += 12
    elif amp == "am" and hour == 12:
        hour = 0
    try:
        target = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if target <= datetime.now():
        target += timedelta(days=1)
    return int((target - datetime.now()).total_seconds()), phrase


def _clean_trailing_time(message: str, phrase: str) -> str:
    """Drop a 'at 3 pm' phrase from the end of a reminder message."""
    if not phrase or not message:
        return message
    tail = phrase[3:].strip() if phrase.startswith("at ") else phrase
    cleaned = re.sub(r"\s+at\s+" + re.escape(tail) + r"\s*$", "", message, flags=re.I)
    return cleaned.strip(" ,.")

def _clean_reminder_message(text: str) -> str:
    t = text.strip()
    # Everything after "to" is normally the reminder message.
    m = re.search(r"\bto\s+(.+)$", t, flags=re.I)
    if m:
        return m.group(1).strip(" .")
    # Also support "remind me about X in 10 minutes".
    m = re.search(r"\babout\s+(.+?)(?:\s+in\s+[\w.]+\s+(?:seconds?|minutes?|hours?|days?))?\s*$",
                  t, flags=re.I)
    if m:
        return m.group(1).strip(" .")
    return ""

def _deterministic_timer(text: str):
    """
    Handle timers/reminders without the LLM.
    Returns a list of steps or None if this is not a timer command.
    """
    t = text.strip()
    low = t.lower()

    reminder = (
        "remind" in low
        or "reminder" in low
    )

    timer = (
        "timer" in low
        or low.startswith("set a timer")
        or low.startswith("set timer")
    )

    if not reminder and not timer:
        return None

    # Prefer an absolute clock time ("at 3 pm"); fall back to a duration
    # phrase ("in 10 minutes").
    phrase = ""
    abs_t = _absolute_time(t)
    if abs_t is not None:
        duration, phrase = abs_t
    else:
        duration = _duration_seconds(t)
        if duration is None:
            return None

    if reminder:
        message = _clean_reminder_message(t)
        message = _clean_trailing_time(message, phrase)
        if not message:
            # "remind me in 10 minutes" is still a reminder request, but
            # Phase 3 will reject an empty message cleanly.
            message = ""
        return [{
            "action": "set_reminder",
            "target": message,
            "duration_seconds": duration,
        }]

    # For a plain timer, don't accidentally put "10 seconds" into target.
    # If the user said "timer for pasta for 10 minutes", keep "pasta".
    message = ""
    stripped = re.sub(
        r"\b(?:set\s+)?(?:a\s+)?timer\b", "", t, flags=re.I
    )
    stripped = re.sub(
        r"\b(?:in|for)\s+(\d+(?:\.\d+)?|[a-z]+(?:\s+[a-z]+)?)\s*"
        r"(?:seconds?|secs?|minutes?|mins?|hours?|days?)\b",
        "",
        stripped,
        flags=re.I,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip(" ,.")
    if stripped.lower() in {"", "for", "in"}:
        stripped = ""
    elif stripped.lower().startswith("for "):
        stripped = stripped[4:].strip()
    message = stripped

def _deterministic_weather(text: str):
    """
    Handle weather requests without the LLM.
    Returns a list of steps or None if this is not a weather command.
    """
    low = text.lower().strip().rstrip('?')
    if "weather" not in low and "temperature" not in low:
        return None

    # Try to extract city: "weather in London", "weather like today in Gurgaon"
    # Look for the location after "in", "at", or "for"
    m = re.search(r"\b(?:in|at|for)\s+([a-zA-Z\s,]+)$", low, flags=re.I)

    if m:
        city = m.group(1).strip()
    else:
        # Fallback: if the user says "London weather", capture the part before "weather"
        m_fallback = re.search(r"^([a-zA-Z\s,]+)\s+(?:weather|temperature)", low, flags=re.I)
        city = m_fallback.group(1).strip() if m_fallback else ""

    return [{
        "action": "get_weather",
        "target": city,
        "duration_seconds": 0,
    }]

def _deterministic_news(text: str):
    """
    Handle news requests without the LLM.
    Returns a list of steps or None if this is not a news command.
    """
    low = text.lower().strip().rstrip('?')
    if "news" not in low:
        return None

    # Don't steal commands that merely mention news ("open chrome and search
    # for AI news"). Only handle clear news requests.
    if re.match(r"^(?:get|show|tell|give|what|how|news|latest|today's|todays)\b", low) is None:
        return None

    # Topic: "news about/on/regarding X" -> X; otherwise strip request filler
    m = re.search(r"\bnews\s+(?:about|on|regarding)\s+([a-zA-Z\s,]+)$", low, flags=re.I)
    if m:
        topic = m.group(1).strip()
    else:
        filler = re.sub(r"^(?:(?:get|show|tell|give)\s+me\s+|whats\s+|what's\s+|the\s+|today's\s+|todays\s+|latest\s+)+", "", low)
        m_fallback = re.search(r"^([a-zA-Z\s,]+?)\s+news$", filler, flags=re.I)
        topic = m_fallback.group(1).strip() if m_fallback else ""

    return [{
        "action": "get_news",
        "target": topic,
        "duration_seconds": 0,
    }]

def _deterministic_macro(text: str):
    """
    Handle macro modes without the LLM.
    Returns a list of steps or None if this is not a macro command.
    """
    low = text.lower().strip()
    m = re.search(r"\b(work|relax|study|gaming|focus)\b\s*(?:mode)?", low)
    if not m:
        return None
    mode = m.group(1)
    return [{"action": "run_macro", "target": mode, "duration_seconds": 0}]

def _deterministic_volume(text: str):
    """Handle volume controls without the LLM."""
    low = text.lower().strip()
    if "volume" not in low and "mute" not in low:
        return None
    if any(x in low for x in ("up", "increase", "raise")):
        return [{"action": "volume_up", "target": "", "duration_seconds": 0}]
    if any(x in low for x in ("down", "decrease", "lower")):
        return [{"action": "volume_down", "target": "", "duration_seconds": 0}]
    if any(x in low for x in ("mute", "unmute", "silence")):
        return [{"action": "volume_mute", "target": "", "duration_seconds": 0}]
    return None

def _deterministic_study(text: str):
    """Handle subject study requests without the LLM."""
    low = text.lower().strip()
    if "study" not in low:
        return None
    m = re.search(r"study\s+(?:for\s+)?([a-zA-Z\s]+)", low)
    if m:
        return [{"action": "study_subject", "target": m.group(1).strip(), "duration_seconds": 0}]
    return None

def _calendar_scope(text: str) -> str:
    """Pull any mentioned day/date ("tomorrow", "friday", "december 5", "the
    25th", "next week") out of a calendar query. Empty string = no day named."""
    low = (text or "").lower()
    if re.search(r"\btomorrow\b", low):
        return "tomorrow"
    if re.search(r"\btoday\b", low):
        return "today"
    if "next week" in low:
        return "next week"

    m = re.search(r"\b(?:next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low)
    if m:
        return m.group(1)

    months = "january february march april may june july august september october november december".split()
    for pat in (
        r"\b(?:on\s+|the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(months) + r")\b",
        r"\b(" + "|".join(months) + r")\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b",
    ):
        m = re.search(pat, low)
        if m:
            a, b = m.groups()
            return f"{a} {b}" if a in months else f"{b} {a}"

    m = re.search(r"\b(?:on\s+|the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        return m.group(0).strip()

    return ""


def _deterministic_calendar(text: str):
    """Handle calendar/schedule requests without the LLM. The trigger phrases
    are configurable in beth_calendar.json (next to this file)."""
    low = text.lower().strip()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beth_calendar.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        triggers = cfg.get("triggers") or ["calendar", "schedule", "tomorrow", "today"]
    except Exception:
        triggers = ["calendar", "schedule", "tomorrow", "today"]

    scope = _calendar_scope(text)
    if not any(str(t).lower() in low for t in triggers) and not scope:
        return None

    return [{"action": "get_calendar", "target": scope, "duration_seconds": 0}]

def _deterministic_clock(text: str):
    """'what time is it' / 'what's the date' — instant, no LLM."""
    low = text.lower()
    if (
        re.search(r"\bwhat(?:'s|s|\s+is)?\s+the?\s*time\b", low)
        or "current time" in low
        or re.search(r"\bwhat\s+time\s+is\s+it\b", low)
    ):
        return [{"action": "get_clock", "target": "time", "duration_seconds": 0}]
    if (
        re.search(r"\bwhat(?:'s|s|\s+is)?\s+(?:the\s+)?date\b", low)
        or "current date" in low
        or re.search(r"\bwhat\s+day\s+is\s+it\b", low)
    ):
        return [{"action": "get_clock", "target": "date", "duration_seconds": 0}]
    return None


def _deterministic_calc(text: str):
    """Simple spoken arithmetic: 'what is 15% of 200', '42 times 7',
    'sqrt 144'. RESTRICTED eval in Phase 3 — never the LLM."""
    low = text.lower()
    if not re.search(r"\b(?:what(?:'s|s|\s+is)?|calculate|compute|evaluate|how much is)\b", low):
        return None
    if not re.search(r"\d", low):
        return None

    expr = text
    expr = re.sub(
        r"\b(?:hey beth,?\s+|please\s+)?(?:what(?:'s|s|\s+is)?|calculate|compute|evaluate|how much is)\s*",
        "",
        expr,
        flags=re.I,
    )
    expr = re.sub(r"^the\s+", "", expr, flags=re.I)
    expr = expr.replace("divided by", "/").replace("multiplied by", "*").replace("times", "*")
    expr = re.sub(r"\bplus\b", "+", expr, flags=re.I)
    expr = re.sub(r"\bminus\b", "-", expr, flags=re.I)
    expr = re.sub(r"\bsquare root of\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", expr, flags=re.I)
    expr = re.sub(r"\bsqrt\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", expr, flags=re.I)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s+of\s+(\d+(?:\.\d+)?)", r"(\1/100)*\2", expr)
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%$", r"(\1/100)", expr)
    expr = re.sub(r"[?!.,]+\s*$", "", expr).strip()
    if not expr or not re.match(r"^[0-9a-zA-Z+\-*/().%^ ]+$", expr):
        return None
    if len(expr) > 80:
        return None
    return [{"action": "calculate", "target": expr, "duration_seconds": 0}]


def _deterministic_notes(text: str):
    """'note that X', 'write this down X', 'read my notes'."""
    low = text.lower()
    if re.search(r"\b(?:read|show|what(?:'s| are| is)?\s+(?:in|on))\s+(?:my\s+)?notes\b", low):
        return [{"action": "read_notes", "target": "", "duration_seconds": 0}]

    m = re.search(
        r"\b(?:note|notes)\s+(?:that\s+|down\s+|this\s+|it\s+down\s*:?\s*)?(.+)$",
        text,
        flags=re.I,
    )
    if not m:
        m = re.search(r"\bwrite\s+this\s+down\s*:?\s*(.+)$", text, flags=re.I)
    if not m:
        return None
    msg = re.sub(r"\s+(?:down|in\s+my\s+notes|to\s+my\s+notes)\s*$", "", m.group(1), flags=re.I)
    msg = msg.strip(" .,")
    if not msg:
        return None
    return [{"action": "save_note", "target": msg, "duration_seconds": 0}]


def _deterministic_battery(text: str):
    """Battery questions — instant, local."""
    low = text.lower()
    if "battery" not in low:
        return None
    if re.search(r"\b(?:how'?s|how\s+is)\s+(?:my\s+)?battery\b", low) \
            or "battery level" in low \
            or re.search(r"\bbattery\s+(?:level|percentage|percent|left)\b", low):
        return [{"action": "get_battery", "target": "", "duration_seconds": 0}]
    return None

def _deterministic_apps(text: str):
    """Fast path for the most common commands (open/close apps, open sites,
    web search). These never touch the LLM — on this laptop that's the
    difference between ~2s and 30-60s. Compound commands fall through to the LLM."""
    t = text.strip().lower()
    t = re.sub(r"\b(please|would you|can you|could you|hey beth|beth)\b", " ", t)
    t = re.sub(r"\b(for me)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,")

    if not t:
        return None

    if re.search(r"\b(close|shut down|exit|quit)\b.*\b(this|that)\b", t) or \
       t in {"close it", "close that", "close this"}:
        return [{"action": "close_current_app", "target": "", "duration_seconds": 0}]

    m = re.match(r"^(open|start|launch|go to)\s+(.+)$", t)
    if m:
        raw = m.group(2).strip()
        if not raw or " and " in raw or "," in raw:
            return None  # compound -> LLM should split it
        if "chrome" in raw and re.search(r"first|second|third|fourth|fifth|profile", raw):
            return None  # "open my third chrome profile" etc. -> LLM
        target = re.sub(r"^(my|the)\s+", "", raw)
        if re.search(r"(?:https?://|\.\w{2,})", target):
            return [{"action": "open_site", "target": target, "duration_seconds": 0}]
        sites = {"youtube": "youtube.com", "github": "github.com",
                 "gmail": "mail.google.com", "maps": "maps.google.com"}
        if target in sites:
            return [{"action": "open_site", "target": sites[target], "duration_seconds": 0}]
        return [{"action": "open_app", "target": target, "duration_seconds": 0}]

    m = re.match(r"^(close|shut down|exit|quit|stop)\s+(.+)$", t)
    if m:
        target = m.group(2).strip()
        if " and " in target or "," in target:
            return None
        target = re.sub(r"^(my|the|this)\s+", "", target)
        return [{"action": "close_app", "target": target, "duration_seconds": 0}]

    if t.startswith("search"):
        rest = re.sub(r"^(?:for\s+|google\s+|the web\s+|on the web\s+)+", "", t[len("search"):].strip())
        if not rest:
            return None
        if re.match(r"^(my|this|local)", rest) and "file" in rest:
            return [{"action": "search_files", "target": rest, "duration_seconds": 0}]
        return [{"action": "web_search", "target": rest, "duration_seconds": 0}]

    return None

SYSTEM_PROMPT = f"""
You are the intent parser for a Windows voice assistant named {ASSISTANT_NAME}. Translate the user's spoken command into executable steps.

RESPONSE FORMAT:
Return ONLY one JSON object. No markdown. Shape: {{"steps": [{{"action":"...", "target":"...", "duration_seconds":0, "profile_identifier": "", "value": ""}}]}}.
steps is ALWAYS an array, even for one action. Keep it short.

ALLOWED ACTIONS:
{", ".join(SUPPORTED_ACTIONS)}

QUICK GUIDE:
open_app/close_app -> desktop apps by common name (target = app name)
close_current_app -> "close this window" | identify_app -> "what app is this"
open_site -> URL/domain mentioned | web_search -> query only
chrome_profile_search -> search in a NAMED profile (profile_identifier = name/number, target = query) | open_chrome_profile -> open that profile
search_files -> local files | set_timer/set_reminder -> duration_seconds as int | list_timers/cancel_timer
change_voice -> British/Indian/Male | add_todo/list_todo/remove_todo
get_weather -> city | get_news -> topic | system_health -> cpu/ram
remember_fact(target=key, value=fact) / recall_fact(target=key)
load_document -> read a file | ask_document -> question about a loaded one
volume_up/volume_down/volume_mute | run_macro -> mode name | study_subject -> subject
get_calendar -> calendar/schedule questions | chat -> general knowledge/conversation | unknown -> unclear/out of scope

RULES:
1. Preserve order of requests. Split compounds like "open chrome and search for X" into [open_app, web_search].
2. Never invent actions. JSON only, no filler.

EXAMPLES:
Input: "Open Spotify and play some music" -> {{"steps":[{{"action":"open_app","target":"spotify","duration_seconds":0}},{{"action":"chat","target":"play some music","duration_seconds":0}}]}}
Input: "Close this window" -> {{"steps":[{{"action":"close_current_app","target":"","duration_seconds":0}}]}}
Input: "Remember that my favorite color is blue" -> {{"steps":[{{"action":"remember_fact","target":"favorite color","value":"blue","duration_seconds":0}}]}}
Input: "Add buy milk to my todo list" -> {{"steps":[{{"action":"add_todo","target":"buy milk","duration_seconds":0}}]}}
Input: "What is the distance to the moon?" -> {{"steps":[{{"action":"chat","target":"What is the distance to the moon?","duration_seconds":0}}]}}
Input: "Open my nikhil chrome profile" -> {{"steps":[{{"action":"open_chrome_profile","target":"nikhil","duration_seconds":0}}]}}
"""

def _normalise_steps(data):
    """Convert all plausible Ollama JSON shapes into our one canonical shape."""
    if isinstance(data, dict) and isinstance(data.get("steps"), list):
        raw_steps = data["steps"]
    elif isinstance(data, dict) and "action" in data:
        raw_steps = [data]
    elif isinstance(data, list):
        raw_steps = data
    else:
        return [{"action": "unknown", "target": "", "duration_seconds": 0}]

    steps = []
    for item in raw_steps[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue

        action = str(item.get("action", "unknown")).strip().lower()
        if action not in SUPPORTED_ACTIONS:
            action = "unknown"

        target = str(item.get("target", "") or "").strip()

        try:
            duration = int(item.get("duration_seconds", 0) or 0)
        except (TypeError, ValueError):
            duration = 0

        # Capture profile_identifier if present (for Chrome profiles)
        profile_id = str(item.get("profile_identifier", "") or "").strip()

        steps.append({
            "action": action,
            "target": target,
            "duration_seconds": max(0, duration),
            "profile_identifier": profile_id
        })

    return steps or [{"action": "unknown", "target": "", "duration_seconds": 0}]


def _chrome_profile_search(text: str):
    """
    Detect a command that explicitly names a numbered Chrome profile,
    e.g. "search AI news in my third Chrome".
    """
    t = str(text or "").strip()
    low = t.lower()

    if "chrome" not in low:
        return None

    if not any(x in low for x in (
        "search", "look up", "google", "find"
    )):
        return None

    ordinal_map = {
        "first": 1, "1st": 1,
        "second": 2, "2nd": 2,
        "third": 3, "3rd": 3,
        "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5,
    }

    profile_index = None
    for word, number in ordinal_map.items():
        if re.search(r"\\b" + re.escape(word) + r"\\b", low):
            profile_index = number
            break

    if profile_index is None:
        return None

    query = t

    # Remove "in/on/using my third Chrome/profile".
    query = re.sub(
        r"\\b(?:in|on|using|with)\\s+(?:my\\s+)?"
        r"(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)"
        r"\\s+chrome(?:\\s+profile)?\\b",
        "",
        query,
        flags=re.I,
    )

    # Remove "go to/use/open my third Chrome".
    query = re.sub(
        r"\\b(?:go\\s+to|use|open)\\s+(?:my\\s+)?"
        r"(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)"
        r"\\s+chrome(?:\\s+profile)?\\s*(?:and)?",
        "",
        query,
        flags=re.I,
    )

    # Remove the search verb.
    query = re.sub(
        r"\\b(?:search|look\\s+up|google|find)\\s+(?:for\\s+)?",
        "",
        query,
        count=1,
        flags=re.I,
    )

    query = re.sub(r"\\s+", " ", query).strip(" ,.")

    if not query:
        return None

    return [{
        "action": "chrome_profile_search",
        "target": query,
        "duration_seconds": 0,
        "profile_index": profile_index,
    }]


def parse_intent(text: str, history: list = None) -> list[dict]:
    """Return a canonical list of executable steps."""
    text = str(text or "").strip()
    if not text:
        return [{"action": "unknown", "target": "", "duration_seconds": 0}]

    # Critical: timers/reminders do not depend on Llama 3B.
    timer_steps = _deterministic_timer(text)
    if timer_steps is not None:
        print(f"[Beth Brain] Deterministic timer parse: {timer_steps}")
        return timer_steps

    weather_steps = _deterministic_weather(text)
    if weather_steps is not None:
        print(f"[Beth Brain] Deterministic weather parse: {weather_steps}")
        return weather_steps

    news_steps = _deterministic_news(text)
    if news_steps is not None:
        print(f"[Beth Brain] Deterministic news parse: {news_steps}")
        return news_steps

    chrome_steps = _chrome_profile_search(text)
    if chrome_steps is not None:
        print(f"[Beth Brain] Chrome profile parse: {chrome_steps}")
        return chrome_steps

    macro_steps = _deterministic_macro(text)
    if macro_steps is not None:
        print(f"[Beth Brain] Deterministic macro parse: {macro_steps}")
        return macro_steps

    volume_steps = _deterministic_volume(text)
    if volume_steps is not None:
        print(f"[Beth Brain] Deterministic volume parse: {volume_steps}")
        return volume_steps

    clock_steps = _deterministic_clock(text)
    if clock_steps is not None:
        print(f"[Beth Brain] Deterministic clock parse: {clock_steps}")
        return clock_steps

    calc_steps = _deterministic_calc(text)
    if calc_steps is not None:
        print(f"[Beth Brain] Deterministic calc parse: {calc_steps}")
        return calc_steps

    notes_steps = _deterministic_notes(text)
    if notes_steps is not None:
        print(f"[Beth Brain] Deterministic notes parse: {notes_steps}")
        return notes_steps

    battery_steps = _deterministic_battery(text)
    if battery_steps is not None:
        print(f"[Beth Brain] Deterministic battery parse: {battery_steps}")
        return battery_steps

    study_steps = _deterministic_study(text)
    if study_steps is not None:
        print(f"[Beth Brain] Deterministic study parse: {study_steps}")
        return study_steps

    calendar_steps = _deterministic_calendar(text)
    if calendar_steps is not None:
        print(f"[Beth Brain] Deterministic calendar parse: {calendar_steps}")
        return calendar_steps

    app_steps = _deterministic_apps(text)
    if app_steps is not None:
        print(f"[Beth Brain] Deterministic app parse: {app_steps}")
        return app_steps

    # Conversation history is intentionally NOT included: it bloats every
    # prompt (costing 30s+ of CPU evaluation) and intent classification
    # doesn't need it.
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{SYSTEM_PROMPT}\nUser command: {text}\nJSON:",
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0,
        },
    }

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        raw_output = response.json().get("response", "{}")
    except requests.exceptions.ConnectionError:
        print("ERROR: Can't reach Ollama. Is it running? Try: ollama serve")
        return [{"action": "unknown", "target": "connection_error", "duration_seconds": 0}]
    except requests.exceptions.Timeout:
        print("ERROR: Ollama took too long to respond.")
        return [{"action": "unknown", "target": "timeout_error", "duration_seconds": 0}]
    except Exception as e:
        print(f"ERROR contacting Ollama: {e}")
        return [{"action": "unknown", "target": "general_error", "duration_seconds": 0}]

    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError:
        print(f"WARNING: Model returned invalid JSON: {raw_output!r}")
        return [{"action": "unknown", "target": "", "duration_seconds": 0}]

    steps = _normalise_steps(data)

    # Safety net: if the LLM itself classified a timer but failed to convert
    # the duration, re-run the deterministic parser.
    if any(
        s["action"] in {"set_timer", "set_reminder"}
        and s["duration_seconds"] <= 0
        for s in steps
    ):
        timer_steps = _deterministic_timer(text)
        if timer_steps is not None:
            return timer_steps

    print(f"[Beth Brain] Parsed steps: {steps}")
    return steps

def generate_chat_response(prompt_text: str, history: list = None) -> str:
    sys_instruction = (
        f"You are {ASSISTANT_NAME}, a helpful and friendly voice assistant. "
        "Keep responses concise, conversational, and clear for voice synthesis "
        "(1-3 sentences maximum)."
    )

    # Build history string (last 6 turns only — enough context, much faster)
    history_str = ""
    if history:
        history_str = "\n".join([f"{role}: {msg}" for role, msg in history[-6:]]) + "\n"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"{sys_instruction}\n\nRecent Conversation:\n{history_str}\nUser: {prompt_text}\n{ASSISTANT_NAME}:",
        "stream": False,
        "options": {"temperature": 0.7},
    }

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        answer = response.json().get("response", "").strip()
        return answer if answer else "I'm not sure how to respond to that."
    except Exception as e:
        print(f"ERROR generating chat response: {e}")
        return "I had trouble coming up with an answer."

def main():
    print(f"Testing Beth intent parser with model: {OLLAMA_MODEL}")
    print("Type a command (or 'quit' to exit)\n")

    while True:
        text = input("You: ").strip()
        if text.lower() == "quit":
            break

        steps = parse_intent(text)
        print("Steps:")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")
        print()

if __name__ == "__main__":
    main()
