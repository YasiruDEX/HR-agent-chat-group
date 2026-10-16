"""Chat Agent entrypoint: POST /chat on port 8000, plus the lunch/tea scheduler in the background."""
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

import agent

log = logging.getLogger("break-bot")
_sessions: Dict[str, List[Any]] = {}
MAX_HISTORY = 20


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    context: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    response: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched = agent.schedule(background=True) if agent.WEBHOOK_URL else None
    if not sched:
        log.warning("GOOGLE_CHAT_WEBHOOK_URL not set; scheduler disabled")
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="Break-time Agent", lifespan=lifespan)


def schedule_summary() -> str:
    return (f"Lunch at {agent.LUNCH_TIMES}, tea at {agent.TEA_TIMES} "
            f"({agent.SCHEDULE_DAYS}, {agent.TIMEZONE}). Vibe: {agent.VIBE}.")


def chat_with_agent(message: str, session_id: str, context: Optional[Dict[str, Any]]) -> str:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    @tool
    def get_schedule() -> str:
        """Get the configured lunch/tea times, days, timezone and vibe."""
        return schedule_summary()

    @tool
    def trigger_break_message(kind: str) -> str:
        """Immediately generate and post a break message to the team. kind must be 'lunch' or 'tea'."""
        if kind not in ("lunch", "tea"):
            return "kind must be 'lunch' or 'tea'"
        agent.run_break(kind)
        return f"{kind} message posted"

    @tool
    def send_custom_message(message: str) -> str:
        """Post a custom message (exact text) to the team's Google Chat space."""
        return agent.post_to_google_chat(message)

    @tool
    def send_announcement(message: str, title: str = "Announcement", important: bool = False,
                          mention_all: bool = False) -> str:
        """Post an important announcement to the team as a highlighted card.
        message: the body (Google Chat supports <b>bold</b>, <i>italic</i>, <br> in cards).
        title: short headline. important: true for urgent/critical notices.
        mention_all: true to @mention everyone (only if the user asks or it's urgent)."""
        return agent.post_announcement(message, title, important, mention_all)

    tools = [get_schedule, trigger_break_message, send_custom_message, send_announcement]
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are the break-time bot for the '{team}' Google Chat group. You announce lunch and tea "
         "breaks with a {vibe} vibe. Help the user: answer questions about the schedule, trigger "
         "lunch/tea messages, or post custom messages and announcements to the group when asked.\n"
         "- If the user gives exact text in quotes or says 'send exactly', post it verbatim.\n"
         "- If they give a brief (e.g. 'tell everyone the standup moved to 3pm'), write a clear, concise "
         "message for them: professional but friendly, at most 1 emoji, no hype.\n"
         "- Important/urgent notices, deadlines, outages, schedule changes: use send_announcement. "
         "Casual messages: send_custom_message.\n"
         "- Only post to the group when the user clearly asks. After posting, tell the user exactly what was sent.\n"
         "Extra context: {context}"),
        ("placeholder", "{history}"),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])
    llm = ChatOpenAI(model=agent.OPENAI_MODEL, api_key=agent.OPENAI_API_KEY)
    executor = AgentExecutor(agent=create_tool_calling_agent(llm, tools, prompt), tools=tools)
    history = _sessions.setdefault(session_id, [])
    out = executor.invoke({"input": message, "history": history, "team": agent.TEAM_NAME,
                           "vibe": agent.VIBE, "context": context or {}})["output"]
    history.extend([HumanMessage(message), AIMessage(out)])
    del history[:-MAX_HISTORY]
    return out


def chat_without_llm(message: str) -> str:
    """Keyword fallback when no OpenAI key is configured."""
    text = message.lower()
    for kind in ("lunch", "tea"):
        if kind in text and any(w in text for w in ("send", "trigger", "post", "now")):
            agent.run_break(kind)
            return f"Posted a {kind} message to the group ✅"
    for prefix, important in (("urgent:", True), ("announce:", False)):
        if text.startswith(prefix):
            agent.post_announcement(message[len(prefix):].strip(), important=important)
            return "Posted the announcement to the group ✅"
    if text.startswith("say "):
        agent.post_to_google_chat(message[4:])
        return "Posted your message to the group ✅"
    return ("OPENAI_API_KEY isn't set, so I'm in basic mode. Try 'send lunch now', "
            "'send tea now', 'say <message>', 'announce: <message>' or 'urgent: <message>'. " + schedule_summary())


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        if agent.OPENAI_API_KEY:
            reply = chat_with_agent(req.message, req.session_id or "default", req.context)
        else:
            reply = chat_without_llm(req.message)
    except Exception as e:
        log.exception("chat failed")
        reply = f"Sorry, something went wrong: {e}"
    return ChatResponse(response=reply)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
