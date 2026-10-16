# 🍛 Break-time Agent

A LangChain + OpenAI agent that keeps the **Agent Manager Team** fed, caffeinated and on time. It posts AI-written lunch and tea reminders, a morning menu preview and a weather-aware commute reminder to Google Chat. It also exposes a chat endpoint for sending announcements on demand.

## What it does

| Time (Mon–Fri) | Message | Group |
|---|---|---|
| **09:30** | 🍽️ Today's lunch & tea menu, read from the menu sheet | Team group |
| **12:30** | 🍛 AI-written lunch reminder, the day's menu and a GIF | Team group |
| **13:30** | 🚗 "Don't forget to book the commute!" plus a rain forecast for the evening | Commute group |
| **15:30** | ☕ AI-written tea reminder, the day's menu and a GIF | Team group |

- **Fresh every time:** the AI writes each lunch and tea message in a dry, witty dev-teammate tone, with emojis that match the actual food.
- **The menu is always exact.** The day's items are copied word for word from the Google Sheet onto their own bold line, never rewritten by the AI. If a cell is empty, the message is just generic.
- **Weather-aware commute:** checks the Open-Meteo forecast for 4–8 PM and warns early if rain is likely (no API key needed).
- **No weekend messages:** nothing scheduled is sent on Saturday or Sunday.
- **Separate groups:** the commute group is only posted to by the scheduler. The chat endpoint can't reach it.

## Chat endpoint

Standard chat interface: **`POST /chat`** on port **8000**.

```json
// Request
{ "message": "Announce to everyone with @all: release freeze starts tomorrow at 10am", "session_id": "s1", "context": {} }
// Response
{ "response": "I posted the announcement ..." }
```

Things you can ask:
- *"What's for lunch today?"* answers from the menu sheet.
- *"What's the schedule?"*
- *"Send the lunch message now"* or *"Send tea now"*.
- *"Tell everyone the standup moved to 3pm"* writes and posts a clear message for you.
- *"Announce with @all: ..."* posts a highlighted 📢 card; urgent notices get a 🚨 card.
- *Send exactly: "..."* posts your text word for word.

The agent only posts when you clearly ask, and its reply tells you exactly what was sent. `GET /health` returns `{"status":"ok"}`.

> ⚠️ Anyone who can reach `/chat` can post to the team group, so keep the endpoint private.

## Daily menu sheet

Google Sheet with the columns **`date | day | lunch | tea`** (dates as `YYYY-MM-DD`; `day` is optional and ignored). Fill in each day's row in the morning, or the whole week at once. Share the sheet as **"Anyone with the link can view"** and set `MENU_SHEET_URL` to its normal share link.

## Configuration (environment variables)

**Required**

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key |
| `GOOGLE_CHAT_WEBHOOK_URL` | Incoming webhook of the team group |

**Schedule & tone**

| Variable | Default | Description |
|---|---|---|
| `LUNCH_TIMES` | `12:30` | Comma-separated; `12:30`, `12.30` and `12:30 PM` all work |
| `TEA_TIMES` | `15:30` | |
| `MENU_PREVIEW_TIMES` | `09:30` | Leave empty to turn the preview off |
| `TIMEZONE` | `Asia/Colombo` | |
| `SCHEDULE_DAYS` | `mon-fri` | Days for the lunch and tea messages |
| `SKIP_WEEKENDS` | `true` | Blocks every scheduled message on Saturday and Sunday |
| `TEAM_NAME` | `Agent Manager Team` | |
| `VIBE` | `casual, dry and witty like a dev teammate, generous with food emojis` | Tone of the AI messages |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `ALLOW_STATIC_FALLBACK` | `false` | Send a fixed backup message if AI generation fails |

**Menu, GIFs & commute**

| Variable | Default | Description |
|---|---|---|
| `MENU_SHEET_URL` | — | Google Sheet share link (or a CSV link) |
| `SEND_GIF` | `true` | Attach a GIF to lunch and tea messages |
| `LUNCH_GIF_URLS` / `TEA_GIF_URLS` | built-in | Your own GIF links, comma-separated |
| `GIPHY_API_KEY` | — | Optional. Uses a random Giphy GIF each time instead of the fixed ones |
| `LUNCH_GIF_QUERY` / `TEA_GIF_QUERY` | `hungry` / `tea time` | Giphy search words (only used with the key) |
| `COMMUTE_WEBHOOK_URL` | — | Commute group webhook; leave empty to turn the commute reminder off |
| `COMMUTE_TIMES` | `13:30` | |
| `COMMUTE_MESSAGE` | `🚗 Don't forget to book the commute!` | |
| `WEATHER_ENABLED` | `true` | |
| `WEATHER_LAT` / `WEATHER_LON` | `6.9271` / `79.8612` | Colombo |
| `WEATHER_FROM_HOUR` / `WEATHER_TO_HOUR` | `16` / `20` | Evening commute window |
| `RAIN_THRESHOLD` | `50` | % chance of rain that triggers the warning |

## Build & run

- **Runtime:** Python 3.11 · **Start command:** `python main.py` · **Agent type:** Chat Agent (port 8000)
- Dependencies install automatically from `requirements.txt`.
- Run **one copy only**: each copy runs its own scheduler, so two copies would send every message twice.

Manual triggers (handy for testing):

```bash
python agent.py --now lunch     # or: tea | menu | commute
```

## Project structure

```
main.py           # FastAPI /chat endpoint + starts the scheduler
agent.py          # scheduler, AI message generation, menu sheet, weather, Google Chat posting
requirements.txt
.env.example      # all settings with defaults
```
