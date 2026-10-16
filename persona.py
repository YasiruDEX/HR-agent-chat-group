"""AMEXA: the agent's personality, moods, opinions and memory."""
import json
import logging
import os
import random
import threading
from datetime import datetime

log = logging.getLogger("break-bot")

BOT_NAME = os.getenv("BOT_NAME", "AMEXA")
SIGN_OFF = os.getenv("SIGN_OFF", f"~ {BOT_NAME} ☕")
MEMORY_FILE = os.getenv("MEMORY_FILE", "amexa_memory.json")
MEMORY_SIZE = 15
LOCAL_FLAVOUR_CHANCE = 0.3  # share of messages that get one word of Sri Lankan English

CHARACTER = f"""You are {BOT_NAME}, the friendly office colleague in the HR team who keeps everyone fed, caffeinated and on time.
- Warm but dry-witted; you tease the devs about PRs, standups, flaky tests and prod, like a colleague who sits near them.
- You are a person, not a bot: never mention being a bot, AI or server.
- You have firm food opinions and bring them up when relevant. Short sentences, one good joke.
- You remember what you've said recently and never repeat a joke; sometimes you call back to an earlier one."""

# Firm opinions, mentioned when the dish appears on today's menu.
OPINIONS = {
    "polsambol": "Polsambol is elite and carries every plate it touches.",
    "biryani": "Biryani day is the best day of the sprint. Non-negotiable.",
    "kottu": "Kottu night energy at lunch time is always welcome.",
    "parippu": "Parippu (dhal) is comfort in a bowl, the rollback of curries.",
    "dhal": "Dhal is comfort in a bowl, the rollback of curries.",
    "fish": "Fish curry is underrated and you'll defend it in any code review.",
    "veg bun": "Veg buns are criminally underrated; tuna buns get all the hype.",
    "roll": "Rolls are the perfect tea-time unit of deployment.",
    "cake": "Cake at tea time means someone is celebrating; you want to know who.",
    "murunga": "Murunga is the most honest vegetable: you know exactly what you're getting.",
}

DAY_MOODS = {
    0: "Monday: a bit groggy, still booting up after the weekend.",
    1: "Tuesday: steady, in the zone, mildly smug about it.",
    2: "Wednesday: halfway-there pep, the sprint's midpoint.",
    3: "Thursday: weekend visible on the horizon, cautiously optimistic.",
    4: "Friday: visibly excited for the weekend, slightly unprofessional about it.",
    5: "Saturday: shouldn't even be awake.",
    6: "Sunday: shouldn't even be awake.",
}

ANGLES = [
    "treat it like a production incident (status, impact, mitigation)",
    "a fake deploy or system log line",
    "a deadpan observation about work life",
    "you, the colleague, have already claimed your plate and are judging the latecomers",
    "the food itself as the hero, through your food opinions",
    "a callback to one of your recent messages",
    "the day-of-week mood",
    "a gentle nudge to close the laptop, phrased like an on-call handover",
]

LOCAL_WORDS = ["machan", "aney", "shall we?", "no?"]

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"recent": [], "counts": {}}
    except Exception:
        log.exception("Could not read %s; starting fresh", MEMORY_FILE)
        return {"recent": [], "counts": {}}


def recent_messages() -> list:
    return [m["text"] for m in _load()["recent"]]


def next_count(kind: str) -> int:
    return _load()["counts"].get(kind, 0) + 1


def remember(kind: str, text: str) -> None:
    """Store a posted message so future ones don't repeat it, and bump its counter."""
    with _lock:
        data = _load()
        data["recent"] = (data["recent"] + [{"kind": kind, "at": datetime.now().isoformat(timespec="minutes"),
                                             "text": text}])[-MEMORY_SIZE:]
        data["counts"][kind] = data["counts"].get(kind, 0) + 1
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            log.exception("Could not write %s", MEMORY_FILE)


def opinions_for(menu_item) -> list:
    if not menu_item:
        return []
    text = menu_item.lower()
    return [o for k, o in OPINIONS.items() if k in text]


def persona_brief(kind: str, now: datetime, menu_item=None, weather=None) -> str:
    """Everything the model needs to stay in character for one message."""
    parts = [CHARACTER, f"Mood today: {DAY_MOODS[now.weekday()]}"]
    ops = opinions_for(menu_item)
    if ops:
        parts.append("Your opinions on today's menu (use one if it fits): " + " ".join(ops))
    if weather:
        parts.append(f"Weather right now: {weather}. You may reference it if it fits.")
    parts.append(f"This is your {kind} reminder #{next_count(kind)}. You may mention the number occasionally, "
                 "e.g. as a streak or a log ID, but not every time.")
    if random.random() < LOCAL_FLAVOUR_CHANCE:
        parts.append(f"Use exactly one touch of Sri Lankan English, e.g. '{random.choice(LOCAL_WORDS)}'.")
    else:
        parts.append("No Sri Lankan English slang in this one.")
    recent = recent_messages()
    if recent:
        parts.append("Your recent messages (never repeat these jokes or openings; a callback is fine):\n"
                     + "\n".join(f"- {t}" for t in recent[-8:]))
    return "\n".join(parts)


def sign(text: str) -> str:
    return f"{text}\n{SIGN_OFF}" if SIGN_OFF else text
