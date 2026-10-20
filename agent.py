"""Break-time bot: a LangChain agent that posts lunch & tea reminders to Google Chat."""
import argparse
import csv
import io
import logging
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

import persona  # noqa: E402  (reads env after load_dotenv)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("break-bot")

WEBHOOK_URL = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")
# LLM: OpenAI or any OpenAI-compatible provider. A Groq key (gsk_...) is detected automatically.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "") or os.getenv("GROQ_API_KEY", "")
IS_GROQ = OPENAI_API_KEY.startswith("gsk_")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1" if IS_GROQ else "") or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b" if IS_GROQ else "gpt-4o-mini")


def make_llm(**kwargs):
    """Chat model for the configured provider (OpenAI by default, Groq if a gsk_ key is set)."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=OPENAI_MODEL, api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL, **kwargs)
LUNCH_TIMES = os.getenv("LUNCH_TIMES", "12:30")
TEA_TIMES = os.getenv("TEA_TIMES", "15:30")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Colombo")
SCHEDULE_DAYS = os.getenv("SCHEDULE_DAYS", "mon-fri")
VIBE = os.getenv("VIBE", "casual, dry and witty like a dev teammate, generous with food emojis")
TEAM_NAME = os.getenv("TEAM_NAME", "Agent Manager Team")
# Static messages are only used if explicitly enabled; by default every message is AI-generated.
# Daily menu sheet (columns: date, lunch, tea). Accepts a Google Sheets share link or a CSV URL.
MENU_SHEET_URL = os.getenv("MENU_SHEET_URL", "")

# Commute reminder: a separate group, posted only by the scheduler (never from /chat).
COMMUTE_WEBHOOK_URL = os.getenv("COMMUTE_WEBHOOK_URL", "")
COMMUTE_TIMES = os.getenv("COMMUTE_TIMES", "13:30")
COMMUTE_MESSAGE = os.getenv("COMMUTE_MESSAGE", "🚗 Don't forget to book the commute!")

# Morning menu preview to the main group (fixed format, straight from the sheet).
# Morning hello and Monday juice reminder are off by default (team feedback); set a time to re-enable.
MORNING_TIMES = os.getenv("MORNING_TIMES", "off")
# Friday "don't rush home" nudge and Monday morning juice reminder (main group).
FRIDAY_WINDDOWN_TIMES = os.getenv("FRIDAY_WINDDOWN_TIMES", "16:30")
JUICE_TIMES = os.getenv("JUICE_TIMES", "off")

# Weather check for the commute reminder (Open-Meteo, free, no key). Defaults to Colombo.
WEATHER_ENABLED = os.getenv("WEATHER_ENABLED", "true").lower() == "true"
WEATHER_LAT = float(os.getenv("WEATHER_LAT", "6.9271"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "79.8612"))
WEATHER_FROM_HOUR = int(os.getenv("WEATHER_FROM_HOUR", "16"))  # evening commute window start
WEATHER_TO_HOUR = int(os.getenv("WEATHER_TO_HOUR", "20"))      # window end (exclusive)
RAIN_THRESHOLD = int(os.getenv("RAIN_THRESHOLD", "50"))        # % chance that counts as "rain expected"

# Never send scheduled break messages on Saturday/Sunday, regardless of SCHEDULE_DAYS.
SKIP_WEEKENDS = os.getenv("SKIP_WEEKENDS", "true").lower() == "true"
ALLOW_STATIC_FALLBACK = os.getenv("ALLOW_STATIC_FALLBACK", "false").lower() == "true"

# GIFs attached to break messages. Override with comma-separated URLs in LUNCH_GIF_URLS / TEA_GIF_URLS,
# or set GIPHY_API_KEY to pull a fresh random GIF from Giphy each time.
SEND_GIF = os.getenv("SEND_GIF", "false").lower() == "true"
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")
GIF_SEARCH = {"lunch": os.getenv("LUNCH_GIF_QUERY", "hungry"), "tea": os.getenv("TEA_GIF_QUERY", "tea time")}
DEFAULT_GIFS = {
    "lunch": ["https://lh5.googleusercontent.com/proxy/kCYn1T8dfpT-DahTs9cFIr5lUzu6iifCpAbzUGNzXJPmFMNpB0PLSYMtbxANU6W94fMqM2p59hDVWG1lbMce3rI23qP0HHHtUilHH3t6Om0"],
    "tea": ["https://lh5.googleusercontent.com/proxy/l6RaNrQUsZEBdiHkoVvvs-N7kpGawtmajVpKOey-1x4YEhmeqepxJLxy4soxH0YxZ7Li04TEVaHAuhI6SZ7cWN5JBE8oyr-xE2fUJw"],
}
GIFS = {k: [u.strip() for u in os.getenv(f"{k.upper()}_GIF_URLS", "").split(",") if u.strip()] or v
        for k, v in DEFAULT_GIFS.items()}

FALLBACKS = {
    "lunch": [
        "🍛 It's lunch o'clock, {team}! Close those tabs, grab your plate, and refuel. The bugs will still be here in an hour. 😄",
        "🥗 Lunch break alert! {team}, your brain has requested snacks. Request approved. ✅",
    ],
    "tea": [
        "☕ Tea time, {team}! Step away from the keyboard and sip something warm. 🫖",
        "🍪 3:30 vibes: tea + biscuits + 10 minutes of not thinking about deadlines. Go go go, {team}!",
    ],
}


def _menu_csv_url(url: str) -> str:
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "output=csv" not in url and "format=csv" not in url:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
                + (f"&gid={gid.group(1)}" if gid else ""))
    return url


def _parse_date(value: str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    return None


_menu_cache = {"at": 0.0, "rows": None}
MENU_CACHE_SECONDS = 120


def _menu_rows():
    """Download the sheet once (cached briefly, retried on timeouts) and return its rows."""
    if _menu_cache["rows"] is not None and time.time() - _menu_cache["at"] < MENU_CACHE_SECONDS:
        return _menu_cache["rows"]
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(_menu_csv_url(MENU_SHEET_URL), timeout=(10, 30))
            r.raise_for_status()
            r.encoding = "utf-8"
            rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                    for row in csv.DictReader(io.StringIO(r.text))]
            _menu_cache.update(at=time.time(), rows=rows)
            return rows
        except Exception as e:
            last_error = e
            log.warning("Menu sheet attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    log.error("Could not read menu sheet after 3 attempts: %s", last_error)
    return None


def get_today_menu(kind: str):
    """Return today's item for a column (breakfast/lunch/tea), or None if unset/unavailable."""
    if not MENU_SHEET_URL:
        return None
    rows = _menu_rows()
    if not rows:
        return None
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    for row in rows:
        if _parse_date(row.get("date", "")) == today:
            return row.get(kind) or None
    return None


FOOD_EMOJIS = set("🍛🍚🍗🍖🥩🐟🍤🦐🥗🥦🥕🌽🍆🥔🍞🥯🥐🥖🥪🍔🍟🍕🌭🌮🌯🥙🍜🍝🍲🍱🍣🍥🥟🍩🍪🧁🍰🎂🥧🍫🍬🍮🍯🥛☕🫖🍵🧃🥤🍹🍊🍋🍌🍉🍇🍓🥭🍍🥥🥚🍳🧀🥓🫓🫔🥘")
# Menu keywords -> emojis that may represent them.
MENU_EMOJIS = [
    (("rice", "curry", "dhal", "parippu", "biryani", "kottu"), "🍛🍚"), (("chicken",), "🍗"),
    (("fish", "maakaral", "tuna"), "🐟"), (("prawn", "shrimp"), "🍤"),
    (("bun", "bread", "roll", "patt", "pastr", "toast"), "🍞🥐"),
    (("veg", "salad", "beetroot", "beans", "karawila", "cabbage", "carrot"), "🥗🥦"),
    (("cake",), "🍰"), (("biscuit", "cookie"), "🍪"), (("egg",), "🥚"),
    (("fruit", "pineapple", "banana", "papaya", "mango"), "🍍🍌🥭"), (("noodle",), "🍜"),
    (("pizza",), "🍕"), (("burger",), "🍔"), (("sandwich",), "🥪"),
]


def allowed_emojis(kind: str, item) -> list:
    """Emojis the message may use: generic lunch/tea ones plus ones matching today's menu."""
    allowed = ["🍽️"] if kind == "lunch" else ["☕"]
    text = (item or "").lower()
    for words, emojis in MENU_EMOJIS:
        if any(w in text for w in words):
            allowed += list(emojis)
    return allowed


def strip_offmenu_emojis(text: str, allowed) -> str:
    """Remove food emojis that don't represent today's menu (guards against invented foods)."""
    out = "".join(ch for ch in text if ch not in FOOD_EMOJIS or ch in allowed)
    return re.sub(r"  +", " ", out).strip()


def with_menu(text: str, kind: str, item) -> str:
    """Append today's menu verbatim from the sheet so it's always shown exactly and clearly."""
    if item:
        icon = "🍽️" if kind == "lunch" else "☕"
        text = f"{text}\n\n{icon} *Today's {kind}:* {item}"
    return persona.sign(text)


def clean(text: str) -> str:
    """Remove em/en dashes from outgoing messages (house style)."""
    text = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1 to \2", text)           # 4–8 -> 4 to 8
    text = re.sub(r"(AM|PM)\s*[–—]\s*", r"\1 to ", text)                # 4 PM–8 PM -> 4 PM to 8 PM
    text = re.sub(r"\s*[—–]\s*", ", ", text)                             # any other dash -> comma
    return re.sub(r"(^|\n), ", r"\1", text)                             # no leading commas


def pick_gif(kind: str):
    """Return a GIF URL for this break, or None if GIFs are disabled."""
    if not SEND_GIF:
        return None
    if GIPHY_API_KEY:
        try:
            r = requests.get("https://api.giphy.com/v1/gifs/random", timeout=10, params={
                "api_key": GIPHY_API_KEY, "tag": GIF_SEARCH[kind], "rating": "g"})
            r.raise_for_status()
            return r.json()["data"]["images"]["downsized"]["url"]
        except Exception:
            log.exception("Giphy lookup failed; using built-in GIFs")
    return random.choice(GIFS[kind])


def post_to_google_chat(text: str, gif_url=None) -> str:
    """Send a text message (optionally with a GIF) to the configured Google Chat space."""
    payload = {"text": clean(text)}
    if gif_url:
        payload["cardsV2"] = [{"cardId": "gif", "card": {"sections": [
            {"widgets": [{"image": {"imageUrl": gif_url, "altText": "GIF"}}]}]}}]
    resp = requests.post(
        WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=15,
    )
    resp.raise_for_status()
    return f"Posted (HTTP {resp.status_code})"


def post_announcement(message: str, title: str = "Announcement", important: bool = False,
                      mention_all: bool = False) -> str:
    """Post a highlighted announcement card (optionally @all) to Google Chat."""
    header = clean(("🚨 " if important else "📢 ") + title)
    message = clean(message)
    payload = {
        "text": "<users/all>" if mention_all else "",
        "cardsV2": [{
            "cardId": "announcement",
            "card": {
                "header": {"title": header, "subtitle": TEAM_NAME},
                "sections": [{"widgets": [{"textParagraph": {"text": message}}]}],
            },
        }],
    }
    if not payload["text"]:
        del payload["text"]
    resp = requests.post(WEBHOOK_URL, json=payload,
                         headers={"Content-Type": "application/json; charset=UTF-8"}, timeout=15)
    resp.raise_for_status()
    return f"Announcement posted (HTTP {resp.status_code})"


def generate_with_agent(kind: str) -> None:
    """Let a LangChain tool-calling agent compose and send the message."""
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    item = get_today_menu(kind)
    log.info("Today's %s menu: %s", kind, item or "(not set)")

    sent = []

    @tool
    def send_google_chat_message(message: str) -> str:
        """Send the final message to the team's Google Chat space."""
        if sent:  # never post twice for one reminder
            return "Already sent. Do not send again; you are done."
        sent.append(True)
        message = strip_offmenu_emojis(message, allowed_emojis(kind, item))
        result = post_to_google_chat(with_menu(message, kind, item), pick_gif(kind))
        log.info("%s message: %s", kind.capitalize(), result)
        persona.remember(kind, message)
        return result

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "{persona}\n\n"
         "Task: write ONE {kind}-time reminder for the '{team}' Google Chat group: ONE very short sentence, "
         "max 10 words, in character. Shorter is better. "
         "Vibe: {vibe}. Today is {today}.\n"
         "ANGLE (mandatory, commit to it fully; it's the whole joke): {angle}.\n"
         "Style rules:\n"
         "- FOOD RULE: never name any food or drink that is not on today's menu. If no menu is given below, "
         "don't name any specific food at all; just say lunch or tea.\n"
         "- Use exactly one emoji at the start: {emoji_hint}. No other emojis.\n"
         "- You don't need to address the team by name. If you do, use '{team}' exactly; no nicknames.\n"
         "- No hype words (epic, champions, brilliance, crush it, let's go, devour) and no exclamation spam.\n"
         "- Don't sign the message or mention your own name.\n"
         "- Never use em dashes (—) or en dashes (–); use commas or full stops instead.\n"
         "- Don't open with 'Attention' or 'It's lunchtime'; vary your openings. No filler like 'delightful' or 'bound to'.\n"
         "- Pick a different dish to mention than your recent messages did, or none at all.\n"
         "Examples of the right tone:\n"
         "- 🍽️ Lunch is live. Bugs can wait an hour.\n"
         "- ☕ Tea time. That PR can wait.\n"
         "{menu_note}"
         "Write something new in that spirit; don't copy the examples. "
         "Call send_google_chat_message exactly once with the final message."),
        ("human", "It's {kind} time now ({now}). Compose and send the message."),
        ("placeholder", "{agent_scratchpad}"),
    ])
    llm = make_llm(temperature=1.0)
    tools = [send_google_chat_message]
    executor = AgentExecutor(agent=create_tool_calling_agent(llm, tools, prompt), tools=tools,
                             max_iterations=3)
    now = datetime.now(ZoneInfo(TIMEZONE))
    menu_note = (
        f"Today's {kind} menu: {item}. The exact menu is automatically added on its own line below your "
        "message, so do NOT list the menu or rename/paraphrase dishes. You may mention ONE dish by its exact "
        "name from the menu to tease it.\n" if item else "No menu today: don't name any food.\n")
    executor.invoke({
        "menu_note": menu_note,
        "persona": persona.persona_brief(kind, now, item, weather_now()),
        "emoji_hint": " or ".join(allowed_emojis(kind, item)),
        "team": TEAM_NAME, "kind": kind, "vibe": VIBE, "angle": random.choice(persona.ANGLES),
        "today": now.strftime("%A, %d %B"), "now": now.strftime("%I:%M %p"),
    })


def run_break(kind: str) -> None:
    log.info("Triggering %s message", kind)
    if OPENAI_API_KEY:
        try:
            generate_with_agent(kind)
            return
        except Exception:
            log.exception("AI generation failed")
    else:
        log.error("OPENAI_API_KEY not set; cannot generate %s message", kind)
    if not ALLOW_STATIC_FALLBACK:
        raise RuntimeError(f"Could not generate {kind} message (set OPENAI_API_KEY)")
    log.warning("Sending static fallback message")
    text = random.choice(FALLBACKS[kind]).format(team=TEAM_NAME)
    log.info(post_to_google_chat(with_menu(text, kind, get_today_menu(kind)), pick_gif(kind)))


def scheduled_break(kind: str) -> None:
    """Scheduler entrypoint: skips weekends, then sends the break message."""
    if SKIP_WEEKENDS and datetime.now(ZoneInfo(TIMEZONE)).weekday() >= 5:
        log.info("Weekend; skipping scheduled %s message", kind)
        return
    run_break(kind)


def _is_weekend() -> bool:
    return SKIP_WEEKENDS and datetime.now(ZoneInfo(TIMEZONE)).weekday() >= 5


def _post(url: str, text: str) -> str:
    resp = requests.post(url, json={"text": clean(text)},
                         headers={"Content-Type": "application/json; charset=UTF-8"}, timeout=15)
    resp.raise_for_status()
    return f"Posted (HTTP {resp.status_code})"


MENU_LINES = [("breakfast", "🍳", "Breakfast"), ("lunch", "🍛", "Lunch"), ("tea", "☕", "Tea")]

# What AMEXA should write for each extra message (the menu itself is always added by code, verbatim).
TASKS = {
    "morning": ("the morning hello. Open by introducing yourself casually, like 'AMEXA here' plus a good morning "
                "to the team, then one short warm/witty line to start the day. Keep it to 1-2 sentences. "
                "Don't list the menu; it's added below your message automatically."),
    "friday": ("the Friday 4:30 PM nudge: remind the team, in a funny way, not to rush home yet. It's still "
               "work time, finish strong, close the laptop properly, push the commit, don't sprint to the "
               "parking lot. ONE very short sentence, max 10 words, playful, never preachy. "
               "Do not mention any food or drink at all."),
    "juice": ("the Monday morning juice reminder: tell the team to grab their morning juice glass before it's "
              "gone. 1-2 sentences, short and fun, use 1-2 juice/fruit emojis (🧃🍊🥭🍍🍉)."),
}
STATIC_TEXT = {
    "morning": "☀️ AMEXA here. Good morning, {team}!",
    "friday": "🏁 Easy there, {team}. It's only 4:30 on a Friday. Finish strong before you sprint home.",
    "juice": "🧃 Morning, {team}! Don't forget to grab your juice glass before it's gone. 🍊",
}


def compose(kind: str) -> str:
    """AMEXA writes a short in-character message for `kind` (morning/friday/juice)."""
    if OPENAI_API_KEY:
        try:
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI
            now = datetime.now(ZoneInfo(TIMEZONE))
            prompt = ChatPromptTemplate.from_messages([
                ("system", "{persona}\n\nTask: write {task}\nTeam name: '{team}' (optional; if used, exactly). Today is {today}. "
                           "Vibe: {vibe}. At most 1 emoji, never a food emoji. Never name any food. Keep it as short as possible. No hype words, no exclamation spam, never use em or en "
                           "dashes. Don't sign the message or mention your own name. "
                           "Reply with the message text only."),
                ("human", "Write it now ({now})."),
            ])
            chain = prompt | make_llm(temperature=1.0)
            text = chain.invoke({
                "persona": persona.persona_brief(kind, now), "task": TASKS[kind], "team": TEAM_NAME,
                "today": now.strftime("%A, %d %B"), "vibe": VIBE, "now": now.strftime("%I:%M %p"),
            }).content.strip().strip('"')
            text = strip_offmenu_emojis(text, [] if kind == "friday" else list("🧃🍊🥭🍍🍉"))
            persona.remember(kind, text)
            return text
        except Exception:
            log.exception("AI generation failed for %s", kind)
    # The morning menu must always go out; the others follow ALLOW_STATIC_FALLBACK.
    if kind != "morning" and not ALLOW_STATIC_FALLBACK:
        raise RuntimeError(f"Could not generate {kind} message (set OPENAI_API_KEY)")
    return STATIC_TEXT[kind].format(team=TEAM_NAME)


def menu_block():
    """Today's breakfast/lunch/tea, verbatim from the sheet, or None if nothing is set."""
    lines = [f"{icon} *{label}:* {item}" for col, icon, label in MENU_LINES if (item := get_today_menu(col))]
    return "\n".join([f"🍽️ *Today's menu*"] + lines) if lines else None


def morning_text() -> str:
    menu = menu_block()
    text = compose("morning")
    return persona.sign(f"{text}\n\n{menu}" if menu else text)


def scheduled_morning() -> None:
    if _is_weekend():
        return
    log.info("Morning message: %s", _post(WEBHOOK_URL, morning_text()))


def scheduled_simple(kind: str) -> None:
    """Friday wind-down / Monday juice: AI-written, signed, main group."""
    if _is_weekend():
        return
    log.info("%s message: %s", kind, _post(WEBHOOK_URL, persona.sign(compose(kind))))


def weather_now():
    """Short description of the next few hours' weather for the persona, or None."""
    if not WEATHER_ENABLED:
        return None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=(5, 8), params={
            "latitude": WEATHER_LAT, "longitude": WEATHER_LON, "timezone": TIMEZONE,
            "hourly": "precipitation_probability,temperature_2m", "forecast_days": 1})
        r.raise_for_status()
        h = r.json()["hourly"]
        hour = datetime.now(ZoneInfo(TIMEZONE)).hour
        rows = [(p or 0, t) for ts, p, t in zip(h["time"], h["precipitation_probability"], h["temperature_2m"])
                if hour <= int(ts[11:13]) < hour + 3]
        if not rows:
            return None
        rain, temp = max(p for p, _ in rows), rows[0][1]
        sky = "rainy" if rain >= RAIN_THRESHOLD else "dry"
        return f"{sky}, about {round(temp)}°C, {rain}% chance of rain in the next few hours"
    except Exception:
        log.warning("Weather lookup for persona failed", exc_info=True)
        return None


def _fmt_hour(h: int) -> str:
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def commute_weather_note():
    """One line about rain in the evening commute window, or None if weather is unavailable."""
    if not WEATHER_ENABLED:
        return None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=(5, 8), params={
            "latitude": WEATHER_LAT, "longitude": WEATHER_LON, "timezone": TIMEZONE,
            "hourly": "precipitation_probability,precipitation", "forecast_days": 1})
        r.raise_for_status()
        hourly = r.json()["hourly"]
        window = [(int(t[11:13]), p or 0, mm or 0) for t, p, mm in zip(
            hourly["time"], hourly["precipitation_probability"], hourly["precipitation"])
            if WEATHER_FROM_HOUR <= int(t[11:13]) < WEATHER_TO_HOUR]
    except Exception as e:
        log.warning("Weather lookup failed (%s); sending reminder without weather", type(e).__name__)
        return None
    if not window:
        return None
    wet = [(h, p) for h, p, _ in window if p >= RAIN_THRESHOLD]
    span = f"{_fmt_hour(WEATHER_FROM_HOUR)} to {_fmt_hour(WEATHER_TO_HOUR)}"
    if wet:
        start, peak = wet[0][0], max(p for _, p in wet)
        return f"🌧️ Rain expected from {_fmt_hour(start)} ({peak}% chance). Book the commute early!"
    peak = max(p for _, p, _ in window)
    return f"🌤️ Looks dry for the evening commute ({span}, max {peak}% chance of rain)."


def commute_message() -> str:
    note = commute_weather_note()
    return persona.sign(f"{COMMUTE_MESSAGE}\n{note}" if note else COMMUTE_MESSAGE)


def send_commute() -> None:
    text = commute_message()  # built once: one weather lookup
    log.info("Sending commute reminder: %r", text)
    log.info("Commute reminder: %s", _post(COMMUTE_WEBHOOK_URL, text))


def scheduled_commute() -> None:
    """Scheduler entrypoint for the commute reminder (separate webhook, weekdays only)."""
    if _is_weekend():
        log.info("Weekend; skipping commute reminder")
        return
    send_commute()


OFF_VALUES = {"off", "none", "disabled", "false", "no", "-"}


def time_list(value: str) -> list:
    """Split a comma-separated times setting; empty or 'off' disables it."""
    if (value or "").strip().lower() in OFF_VALUES:
        return []
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def parse_time(value: str):
    """Parse '12:30', '12.30', '1230', '12:30 PM', '3.30pm', '15' -> (hour, minute)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.\s]?(\d{2}))?\s*([ap]\.?m\.?)?\s*", value, re.I)
    if not m:
        raise ValueError(f"invalid time {value!r} (use HH:MM, e.g. 12:30 or 15:30)")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time {value!r} (use HH:MM, e.g. 12:30 or 15:30)")
    return hour, minute


def schedule(background: bool = False):
    sched = (BackgroundScheduler if background else BlockingScheduler)(timezone=TIMEZONE)
    for kind, times in (("lunch", LUNCH_TIMES), ("tea", TEA_TIMES)):
        for t in time_list(times):
            try:
                hour, minute = parse_time(t)
            except ValueError as e:
                log.error("Skipping %s schedule: %s", kind, e)
                continue
            sched.add_job(scheduled_break, CronTrigger(day_of_week=SCHEDULE_DAYS, hour=hour,
                                                 minute=minute, timezone=TIMEZONE),
                          args=[kind], id=f"{kind}-{t}", misfire_grace_time=300)
            log.info("Scheduled %s at %02d:%02d (%s, %s)", kind, hour, minute, SCHEDULE_DAYS, TIMEZONE)
    for func, times, days, args, label in (
            (scheduled_morning, MORNING_TIMES, "mon-fri", [], "morning message"),
            (scheduled_simple, FRIDAY_WINDDOWN_TIMES, "fri", ["friday"], "Friday wind-down"),
            (scheduled_simple, JUICE_TIMES, "mon", ["juice"], "Monday juice reminder")):
        for t in time_list(times):
            try:
                hour, minute = parse_time(t)
            except ValueError as e:
                log.error("Skipping %s schedule: %s", label, e)
                continue
            sched.add_job(func, CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=TIMEZONE),
                          args=args, id=f"{label}-{t}", misfire_grace_time=300)
            log.info("Scheduled %s at %02d:%02d (%s, %s)", label, hour, minute, days, TIMEZONE)
    if COMMUTE_WEBHOOK_URL:
        for t in time_list(COMMUTE_TIMES):
            try:
                hour, minute = parse_time(t)
            except ValueError as e:
                log.error("Skipping commute schedule: %s", e)
                continue
            sched.add_job(scheduled_commute, CronTrigger(day_of_week="mon-fri", hour=hour,
                                                         minute=minute, timezone=TIMEZONE),
                          id=f"commute-{t}", misfire_grace_time=300)
            log.info("Scheduled commute reminder at %02d:%02d (mon-fri, %s)", hour, minute, TIMEZONE)
    else:
        log.warning("COMMUTE_WEBHOOK_URL not set; commute reminder disabled")
    sched.start()
    return sched


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", choices=["lunch", "tea", "morning", "friday", "juice", "commute"], help="send one message immediately and exit")
    args = parser.parse_args()
    if not WEBHOOK_URL:
        raise SystemExit("GOOGLE_CHAT_WEBHOOK_URL is not set")
    if args.now == "morning":
        log.info(_post(WEBHOOK_URL, morning_text()))
    elif args.now in ("friday", "juice"):
        log.info(_post(WEBHOOK_URL, persona.sign(compose(args.now))))
    elif args.now == "commute":
        if not COMMUTE_WEBHOOK_URL:
            raise SystemExit("COMMUTE_WEBHOOK_URL is not set")
        send_commute()
    elif args.now:
        run_break(args.now)
    else:
        schedule()
