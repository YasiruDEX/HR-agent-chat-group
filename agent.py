"""Break-time bot: a LangChain agent that posts lunch & tea reminders to Google Chat."""
import argparse
import logging
import os
import random
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

    @tool
    def send_google_chat_message(message: str) -> str:
        """Send the final message to the team's Google Chat space."""
        return post_to_google_chat(message, pick_gif(kind))

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
         "- 🍛🍜 It's lunch o'clock, Agent Manager Team! Close those tabs, grab your plate, and refuel. The bugs will still be here in an hour. 🍕😄\n"
         "- ☕🍪 Tea time, Agent Manager Team. That PR isn't going to review itself, but it can definitely wait 15 minutes. 🫖\n"
         "Write something new in that spirit; don't copy the examples. "
         "Call send_google_chat_message exactly once with the final message."),
        ("human", "It's {kind} time now ({now}). Compose and send the message."),
        ("placeholder", "{agent_scratchpad}"),
    ])
    llm = ChatOpenAI(model=OPENAI_MODEL, api_key=OPENAI_API_KEY, temperature=1.0)
    tools = [send_google_chat_message]
    executor = AgentExecutor(agent=create_tool_calling_agent(llm, tools, prompt), tools=tools)
    now = datetime.now(ZoneInfo(TIMEZONE))
    executor.invoke({
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
    log.info(post_to_google_chat(random.choice(FALLBACKS[kind]).format(team=TEAM_NAME), pick_gif(kind)))


def schedule(background: bool = False):
    sched = (BackgroundScheduler if background else BlockingScheduler)(timezone=TIMEZONE)
    for kind, times in (("lunch", LUNCH_TIMES), ("tea", TEA_TIMES)):
        for t in filter(None, (x.strip() for x in times.split(","))):
            hour, minute = t.split(":")
            sched.add_job(run_break, CronTrigger(day_of_week=SCHEDULE_DAYS, hour=int(hour),
                                                 minute=int(minute), timezone=TIMEZONE),
                          args=[kind], id=f"{kind}-{t}", misfire_grace_time=300)
            log.info("Scheduled %s at %s (%s, %s)", kind, t, SCHEDULE_DAYS, TIMEZONE)
    sched.start()
    return sched


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", choices=["lunch", "tea"], help="send one message immediately and exit")
    args = parser.parse_args()
    if not WEBHOOK_URL:
        raise SystemExit("GOOGLE_CHAT_WEBHOOK_URL is not set")
    run_break(args.now) if args.now else schedule()
