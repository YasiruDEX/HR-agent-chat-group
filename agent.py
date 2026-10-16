"""Break-time bot: a LangChain agent that posts lunch & tea reminders to Google Chat."""
import argparse
import csv
import io
import logging
import os
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("break-bot")

WEBHOOK_URL = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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
MENU_PREVIEW_TIMES = os.getenv("MENU_PREVIEW_TIMES", "09:30")

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
SEND_GIF = os.getenv("SEND_GIF", "true").lower() == "true"
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "")
GIF_SEARCH = {"lunch": os.getenv("LUNCH_GIF_QUERY", "hungry"), "tea": os.getenv("TEA_GIF_QUERY", "tea time")}
DEFAULT_GIFS = {
    "lunch": ["https://lh5.googleusercontent.com/proxy/kCYn1T8dfpT-DahTs9cFIr5lUzu6iifCpAbzUGNzXJPmFMNpB0PLSYMtbxANU6W94fMqM2p59hDVWG1lbMce3rI23qP0HHHtUilHH3t6Om0"],
    "tea": ["https://lh5.googleusercontent.com/proxy/l6RaNrQUsZEBdiHkoVvvs-N7kpGawtmajVpKOey-1x4YEhmeqepxJLxy4soxH0YxZ7Li04TEVaHAuhI6SZ7cWN5JBE8oyr-xE2fUJw"],
}
GIFS = {k: [u.strip() for u in os.getenv(f"{k.upper()}_GIF_URLS", "").split(",") if u.strip()] or v
        for k, v in DEFAULT_GIFS.items()}

# A random creative angle per message keeps the AI output fresh day to day.
ANGLES = [
    "a relatable dev joke (bugs, PRs, standups, deploys, meetings)",
    "a deadpan observation about work life",
    "a gentle nudge to step away from the screen",
    "a small playful jab at the endless Slack/Chat pings",
    "the food or drink itself as the hero",
    "a nod to what day of the week it is",
]

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


def get_today_menu(kind: str):
    """Return today's lunch/tea item from the menu sheet, or None if unset/unavailable."""
    if not MENU_SHEET_URL:
        return None
    try:
        r = requests.get(_menu_csv_url(MENU_SHEET_URL), timeout=15)
        r.raise_for_status()
        r.encoding = "utf-8"
        today = datetime.now(ZoneInfo(TIMEZONE)).date()
        for row in csv.DictReader(io.StringIO(r.text)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if _parse_date(row.get("date", "")) == today:
                return row.get(kind) or None
    except Exception:
        log.exception("Could not read menu sheet")
    return None


def with_menu(text: str, kind: str, item) -> str:
    """Append today's menu verbatim from the sheet so it's always shown exactly and clearly."""
    if not item:
        return text
    icon = "🍽️" if kind == "lunch" else "☕"
    return f"{text}\n\n{icon} *Today's {kind}:* {item}"


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
    payload = {"text": text}
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
    header = ("🚨 " if important else "📢 ") + title
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

    @tool
    def send_google_chat_message(message: str) -> str:
        """Send the final message to the team's Google Chat space."""
        return post_to_google_chat(with_menu(message, kind, item), pick_gif(kind))

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You write break reminders for the '{team}' Google Chat group. "
         "Write ONE {kind}-time message, 1-2 sentences, like a witty teammate would post it. "
         "Vibe: {vibe}. Angle for this one: {angle}. Today is {today}.\n"
         "Style rules:\n"
         "- Use 3-4 food/drink emojis that fit the moment (lunch: 🍛🍜🍕🍔🌮🥗🍱🍗🥪🍝🍩, tea: ☕🫖🍪🧁🍰🥐🍵), "
         "e.g. a couple at the start and one or two at the end. Only food/drink emojis plus at most one face like 😄. "
         "No bugs, rockets, stars, sparkles or party emojis.\n"
         "- Address the team exactly as '{team}'. Don't invent nicknames.\n"
         "- No hype words (epic, champions, brilliance, crush it, let's go, devour) and no exclamation spam.\n"
         "- Dry, clever and grounded beats loud and cheesy.\n"
         "Examples of the right tone:\n"
         "- 🍛🍚 It's lunch o'clock, Agent Manager Team! Close those tabs, grab your plate, and refuel. The bugs will still be here in an hour. 🍛😄\n"
         "- ☕🍪 Tea time, Agent Manager Team. That PR isn't going to review itself, but it can definitely wait 15 minutes. 🫖\n"
         "{menu_note}"
         "Write something new in that spirit; don't copy the examples. "
         "Call send_google_chat_message exactly once with the final message."),
        ("human", "It's {kind} time now ({now}). Compose and send the message."),
        ("placeholder", "{agent_scratchpad}"),
    ])
    llm = ChatOpenAI(model=OPENAI_MODEL, api_key=OPENAI_API_KEY, temperature=1.0)
    tools = [send_google_chat_message]
    executor = AgentExecutor(agent=create_tool_calling_agent(llm, tools, prompt), tools=tools)
    now = datetime.now(ZoneInfo(TIMEZONE))
    menu_note = (
        f"Today's {kind} menu: {item}. The exact menu is automatically added on its own line below your "
        "message, so do NOT list the menu or rename/paraphrase dishes. You may mention ONE dish by its exact "
        "name from the menu to tease it. Every food emoji must depict something actually on today's menu "
        "(rice/curry -> 🍛🍚, chicken -> 🍗, fish -> 🐟, buns/bread -> 🍞🥯, vegetables -> 🥗🥦); "
        "no 🍕 or 🍜 unless pizza or noodles are listed.\n" if item else "")
    executor.invoke({
        "menu_note": menu_note,
        "team": TEAM_NAME, "kind": kind, "vibe": VIBE, "angle": random.choice(ANGLES),
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
    resp = requests.post(url, json={"text": text},
                         headers={"Content-Type": "application/json; charset=UTF-8"}, timeout=15)
    resp.raise_for_status()
    return f"Posted (HTTP {resp.status_code})"


def menu_preview_text():
    """Today's menu as a clear fixed-format message, or None if the sheet has nothing for today."""
    lunch, tea = get_today_menu("lunch"), get_today_menu("tea")
    if not (lunch or tea):
        return None
    lines = [f"🍽️ *Today's menu, {TEAM_NAME}*", ""]
    if lunch:
        lines.append(f"🍛 *Lunch:* {lunch}")
    if tea:
        lines.append(f"☕ *Tea:* {tea}")
    lines += ["", "_Plan ahead before ordering outside._"]
    return "\n".join(lines)


def scheduled_menu_preview() -> None:
    if _is_weekend():
        return
    text = menu_preview_text()
    if not text:
        log.info("No menu in the sheet for today; skipping morning preview")
        return
    log.info("Menu preview: %s", _post(WEBHOOK_URL, text))


def _fmt_hour(h: int) -> str:
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def commute_weather_note():
    """One line about rain in the evening commute window, or None if weather is unavailable."""
    if not WEATHER_ENABLED:
        return None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": WEATHER_LAT, "longitude": WEATHER_LON, "timezone": TIMEZONE,
            "hourly": "precipitation_probability,precipitation", "forecast_days": 1})
        r.raise_for_status()
        hourly = r.json()["hourly"]
        window = [(int(t[11:13]), p or 0, mm or 0) for t, p, mm in zip(
            hourly["time"], hourly["precipitation_probability"], hourly["precipitation"])
            if WEATHER_FROM_HOUR <= int(t[11:13]) < WEATHER_TO_HOUR]
    except Exception:
        log.exception("Weather lookup failed; sending reminder without weather")
        return None
    if not window:
        return None
    wet = [(h, p) for h, p, _ in window if p >= RAIN_THRESHOLD]
    span = f"{_fmt_hour(WEATHER_FROM_HOUR)}–{_fmt_hour(WEATHER_TO_HOUR)}"
    if wet:
        start, peak = wet[0][0], max(p for _, p in wet)
        return f"🌧️ Rain expected from {_fmt_hour(start)} ({peak}% chance). Book the commute early!"
    peak = max(p for _, p, _ in window)
    return f"🌤️ Looks dry for the evening commute ({span}, max {peak}% chance of rain)."


def commute_message() -> str:
    note = commute_weather_note()
    return f"{COMMUTE_MESSAGE}\n{note}" if note else COMMUTE_MESSAGE


def scheduled_commute() -> None:
    """Scheduler entrypoint for the commute reminder (separate webhook, weekdays only)."""
    if _is_weekend():
        log.info("Weekend; skipping commute reminder")
        return
    log.info("Commute reminder: %s", _post(COMMUTE_WEBHOOK_URL, commute_message()))


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
        for t in filter(None, (x.strip() for x in times.split(","))):
            try:
                hour, minute = parse_time(t)
            except ValueError as e:
                log.error("Skipping %s schedule: %s", kind, e)
                continue
            sched.add_job(scheduled_break, CronTrigger(day_of_week=SCHEDULE_DAYS, hour=hour,
                                                 minute=minute, timezone=TIMEZONE),
                          args=[kind], id=f"{kind}-{t}", misfire_grace_time=300)
            log.info("Scheduled %s at %02d:%02d (%s, %s)", kind, hour, minute, SCHEDULE_DAYS, TIMEZONE)
    for t in filter(None, (x.strip() for x in MENU_PREVIEW_TIMES.split(","))):
        try:
            hour, minute = parse_time(t)
        except ValueError as e:
            log.error("Skipping menu preview schedule: %s", e)
            continue
        sched.add_job(scheduled_menu_preview, CronTrigger(day_of_week="mon-fri", hour=hour,
                                                          minute=minute, timezone=TIMEZONE),
                      id=f"menu-{t}", misfire_grace_time=300)
        log.info("Scheduled menu preview at %02d:%02d (mon-fri, %s)", hour, minute, TIMEZONE)
    if COMMUTE_WEBHOOK_URL:
        for t in filter(None, (x.strip() for x in COMMUTE_TIMES.split(","))):
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
    parser.add_argument("--now", choices=["lunch", "tea", "menu", "commute"], help="send one message immediately and exit")
    args = parser.parse_args()
    if not WEBHOOK_URL:
        raise SystemExit("GOOGLE_CHAT_WEBHOOK_URL is not set")
    if args.now == "menu":
        scheduled_menu_preview()
    elif args.now == "commute":
        if not COMMUTE_WEBHOOK_URL:
            raise SystemExit("COMMUTE_WEBHOOK_URL is not set")
        log.info("Sending: %r", commute_message())
        scheduled_commute()
    elif args.now:
        run_break(args.now)
    else:
        schedule()
