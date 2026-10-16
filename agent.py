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
VIBE = os.getenv("VIBE", "casual, dry and witty like a dev teammate, light on emojis")
TEAM_NAME = os.getenv("TEAM_NAME", "Agent Manager Team")
# Static messages are only used if explicitly enabled; by default every message is AI-generated.
ALLOW_STATIC_FALLBACK = os.getenv("ALLOW_STATIC_FALLBACK", "false").lower() == "true"

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


def post_to_google_chat(text: str) -> str:
    """Send a text message to the configured Google Chat space."""
    resp = requests.post(
        WEBHOOK_URL,
        json={"text": text},
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=15,
    )
    resp.raise_for_status()
    return f"Posted (HTTP {resp.status_code})"


def generate_with_agent(kind: str) -> None:
    """Let a LangChain tool-calling agent compose and send the message."""
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    @tool
    def send_google_chat_message(message: str) -> str:
        """Send the final message to the team's Google Chat space."""
        return post_to_google_chat(message)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You write break reminders for the '{team}' Google Chat group. "
         "Write ONE {kind}-time message, 1-2 sentences, like a witty teammate would post it. "
         "Vibe: {vibe}. Angle for this one: {angle}. Today is {today}.\n"
         "Style rules:\n"
         "- At most 2 emojis: usually one at the start and optionally one at the end. Never more.\n"
         "- Address the team exactly as '{team}'. Don't invent nicknames.\n"
         "- No hype words (epic, champions, brilliance, crush it, let's go, devour) and no exclamation spam.\n"
         "- Dry, clever and grounded beats loud and cheesy.\n"
         "Examples of the right tone:\n"
         "- 🍛 It's lunch o'clock, Agent Manager Team! Close those tabs, grab your plate, and refuel. The bugs will still be here in an hour. 😄\n"
         "- ☕ Tea time, Agent Manager Team. That PR isn't going to review itself, but it can definitely wait 15 minutes.\n"
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
    log.info(post_to_google_chat(random.choice(FALLBACKS[kind]).format(team=TEAM_NAME)))


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
