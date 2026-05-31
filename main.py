import logging
import json
import random
import datetime
import os
import uuid
import time
import re
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import pymongo
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters, Application
)

# --- TIMEZONE ---
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.datetime.now(IST)

# --- WEB SERVER (keeps Render alive) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        pass  # suppress noisy logs

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# --- QUOTES (fallback if Gemini is unavailable) ---
def load_quotes():
    try:
        with open("quotes.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not load quotes.json: {e}")
        return {"general_encourage": ["“Keep going.”", "“You got this.”"]}

QUOTES = load_quotes()

# --- CONFIG ---
MY_CHAT_ID = 2071012504
DAILY_CAPACITY = 15

DEFAULT_GOALS = [
    {"id": "work_prod",    "text": "1 hr Product Knowledge/Integration", "days": [0,1,2,3,4], "persona": "alain_meaning",     "hour_start": 9,  "hour_end": 12},
    {"id": "work_build",   "text": "Build Product / Tech Blogs",         "days": [5,6],       "persona": "maker_creativity",  "hour_start": 10, "hour_end": 14},
    {"id": "work_dsa",     "text": "1 DSA Question",                     "days": [0,1,2,3,4,5,6], "persona": "stoic_resilience","hour_start": 14, "hour_end": 17},
    {"id": "work_german",  "text": "German Lesson",                      "days": [0,1,2,3,4,5,6], "persona": "mindful_learning","hour_start": 18, "hour_end": 20},
    {"id": "pers_meditate","text": "Meditate",                           "days": [0,1,2,3,4,5,6], "persona": "mindful_learning","hour_start": 7,  "hour_end": 9},
    {"id": "pers_water",   "text": "Drink 3L Water",                     "days": [0,1,2,3,4,5,6], "persona": "general_encourage","hour_start": 10, "hour_end": 20},
]

# --- DATABASE ---

def get_db():
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("WARNING: MONGO_URI not set.")
        return None
    client = pymongo.MongoClient(mongo_uri)
    return client["uplifting_bot_db"]

def get_col():
    db = get_db()
    return db["user_state_v2"] if db is not None else None

def load_state():
    today_str = str(get_ist_time().date())
    default = {
        "date": today_str,
        "active_tasks": [],
        "backlog": [],
        "completed_ids": [],
        "awaiting_morning": False,
        "awaiting_evening": False,
        "morning_intention": None,
    }

    col = get_col()
    if col is None:
        return default

    data = col.find_one({"_id": "current_user"})
    if not data:
        return default

    if data.get("date") != today_str:
        # Day rollover: carry incomplete tasks forward
        old_pool = data.get("active_tasks", []) + data.get("backlog", [])
        completed_ids = data.get("completed_ids", [])
        pool = [t for t in old_pool if t["id"] not in completed_ids]
        pool.sort(key=lambda x: (not x.get("is_urgent", False), x.get("created_at", time.time())))

        new_state = {
            "date": today_str,
            "active_tasks": pool[:DAILY_CAPACITY],
            "backlog": pool[DAILY_CAPACITY:],
            "completed_ids": [],
            "awaiting_morning": False,
            "awaiting_evening": False,
            "morning_intention": None,
        }
        save_state(new_state)
        return new_state

    return data

def save_state(state):
    col = get_col()
    if col is None:
        return
    state["_id"] = "current_user"
    col.replace_one({"_id": "current_user"}, state, upsert=True)

def load_goals():
    col = get_col()
    if col is None:
        return DEFAULT_GOALS
    data = col.find_one({"_id": "user_goals"})
    if not data:
        col.insert_one({"_id": "user_goals", "goals": DEFAULT_GOALS})
        return DEFAULT_GOALS
    return data.get("goals", [])

def save_goals(goals):
    col = get_col()
    if col is None:
        return
    col.replace_one({"_id": "user_goals"}, {"_id": "user_goals", "goals": goals}, upsert=True)

def load_history():
    col = get_col()
    if col is None:
        return []
    data = col.find_one({"_id": "history"})
    return data.get("entries", []) if data else []

def save_history_entry(entry):
    col = get_col()
    if col is None:
        return
    # Keep only the last 14 days
    col.update_one(
        {"_id": "history"},
        {"$push": {"entries": {"$each": [entry], "$slice": -14}}},
        upsert=True
    )

# --- GEMINI AI ---

def build_context_summary() -> str:
    history = load_history()
    state = load_state()
    goals = load_goals()
    today = get_ist_time()
    weekday = today.weekday()
    lines = []

    if history:
        lines.append("Recent days:")
        for entry in history[-7:]:
            rating = entry.get("evening_rating", "?")
            done = entry.get("goals_completed_count", 0)
            total = entry.get("goals_total", 0)
            note = entry.get("evening_note", "")
            note_short = (note[:80] + "...") if len(note) > 80 else note
            lines.append(f"  {entry.get('date','?')}: {done}/{total} goals, rated {rating}/5. \"{note_short}\"")

        # Detect streaks of missed goals
        goal_ids_to_check = [
            ("work_dsa",    "DSA"),
            ("work_german", "German"),
            ("pers_meditate","Meditation"),
        ]
        for goal_id, label in goal_ids_to_check:
            missed = 0
            for entry in reversed(history[-7:]):
                if goal_id not in entry.get("completed_ids", []):
                    missed += 1
                else:
                    break
            if missed >= 2:
                lines.append(f"Pattern: {label} missed {missed} days in a row.")

    morning_intention = state.get("morning_intention")
    if morning_intention:
        lines.append(f"Today's morning intention: \"{morning_intention}\"")

    completed_today = state.get("completed_ids", [])
    if completed_today:
        goal_map = {g["id"]: g["text"] for g in goals}
        done_names = [goal_map[cid] for cid in completed_today if cid in goal_map]
        if done_names:
            lines.append(f"Completed today: {', '.join(done_names)}")

    today_goals = [g for g in goals if weekday in g["days"]]
    pending = [g["text"] for g in today_goals if g["id"] not in completed_today]
    if pending:
        lines.append(f"Still pending today: {', '.join(pending)}")

    return "\n".join(lines) if lines else "No history yet — this is the beginning."

def get_ai_response(prompt: str, context_summary: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Fallback to a random quote
        quote = random.choice(QUOTES.get("general_encourage", ["“Keep going.”"]))
        return quote

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        full_prompt = f"""You are a personal accountability coach for Kartik. Your style: direct, warm, specific. Never generic motivational-poster language.

Kartik's daily goals: DSA (1 problem), German lesson, Meditation, Product knowledge (weekdays), Build/write (weekends), Drink 3L water.

Context about recent days:
{context_summary}

Your task: {prompt}

Rules:
- 2-3 sentences max
- Be specific — reference his actual goals or recent patterns from the context
- If he's struggling, briefly acknowledge it and give ONE concrete small step
- Sound like a thoughtful human, not an app
- No filler phrases like "That's great!" or "Absolutely!"
"""
        response = model.generate_content(full_prompt)
        return response.text.strip()

    except Exception as e:
        print(f"Gemini error: {e}")
        quote = random.choice(QUOTES.get("general_encourage", ["“Keep going.”"]))
        return quote

# --- THREE DAILY TOUCHPOINTS ---

async def morning_checkin(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    goals = load_goals()
    today = get_ist_time()
    weekday = today.weekday()

    today_goals = [g["text"] for g in goals if weekday in g["days"]]
    goals_list = "\n".join(f"• {g}" for g in today_goals)

    msg = (
        f"Good morning. Here's what's on for today:\n\n{goals_list}\n\n"
        "*What's the one most important thing you'll do today?* Reply to this."
    )

    state = load_state()
    state["awaiting_morning"] = True
    state["awaiting_evening"] = False
    save_state(state)

    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def midday_checkin(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = load_state()

    # Skip if they've already completed something today
    if state.get("completed_ids"):
        return

    morning_intention = state.get("morning_intention")
    if morning_intention:
        msg = f"It's 1pm. You said this morning: _\"{morning_intention}\"_\n\nHow's it going?"
    else:
        msg = "It's 1pm. Nothing logged yet today — you haven't even told me what you're working on. Still here?"

    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def evening_checkin(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = load_state()
    goals = load_goals()
    today = get_ist_time()
    weekday = today.weekday()

    today_goals = [g for g in goals if weekday in g["days"]]
    completed_ids = state.get("completed_ids", [])
    completed_count = sum(1 for g in today_goals if g["id"] in completed_ids)
    total_count = len(today_goals)

    msg = (
        f"Day's done. You completed *{completed_count}/{total_count}* goals.\n\n"
        "Rate today 1–5 and tell me one thing about it."
    )

    state["awaiting_evening"] = True
    state["awaiting_morning"] = False
    save_state(state)

    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

# --- CONVERSATIONAL MESSAGE HANDLER ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    state = load_state()
    ctx = build_context_summary()

    if state.get("awaiting_morning"):
        state["morning_intention"] = user_text
        state["awaiting_morning"] = False
        save_state(state)

        ai = get_ai_response(
            f"Kartik's morning intention is: \"{user_text}\". "
            "Give a brief, specific response — one piece of encouragement and one thing to watch out for today based on his recent patterns.",
            ctx
        )
        await update.message.reply_text(ai)

    elif state.get("awaiting_evening"):
        numbers = re.findall(r'\b[1-5]\b', user_text)
        rating = int(numbers[0]) if numbers else None

        goals = load_goals()
        today = get_ist_time()
        weekday = today.weekday()
        today_goal_ids = [g["id"] for g in goals if weekday in g["days"]]
        completed_ids = state.get("completed_ids", [])
        completed_count = sum(1 for gid in today_goal_ids if gid in completed_ids)

        history_entry = {
            "date": str(today.date()),
            "evening_rating": rating,
            "evening_note": user_text[:200],
            "goals_completed_count": completed_count,
            "goals_total": len(today_goal_ids),
            "completed_ids": list(completed_ids),
        }
        save_history_entry(history_entry)

        state["awaiting_evening"] = False
        save_state(state)

        ai = get_ai_response(
            f"Kartik's evening reflection: \"{user_text}\". "
            f"He rated today {rating}/5 and completed {completed_count}/{len(today_goal_ids)} goals. "
            "Acknowledge what he said honestly and give one specific thought for tomorrow.",
            ctx
        )
        await update.message.reply_text(ai)

    else:
        # General conversation — Gemini responds as a coach
        ai = get_ai_response(
            f"Kartik says: \"{user_text}\". Respond as his accountability coach.",
            ctx
        )
        await update.message.reply_text(ai)

# --- COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _register_jobs(context.job_queue, chat_id)
    await update.message.reply_text(
        "I'm online.\n\n"
        "I'll check in at *8am*, *1pm*, and *9pm* IST every day.\n"
        "You can also just talk to me anytime.\n\n"
        "Commands: /list /add /done /addgoal /delgoal /backlog /delete",
        parse_mode="Markdown"
    )

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = " ".join(context.args)
    if not raw_text:
        await update.message.reply_text("Example: `/add Pay bills urgent` or `/add Laundry evening`", parse_mode="Markdown")
        return

    lower_text = raw_text.lower()
    start_hour = 0
    is_urgent = False
    clean_text = raw_text

    if "urgent" in lower_text:
        is_urgent = True
        clean_text = re.sub(r'(?i)\burgent\b', '', clean_text).strip()

    if clean_text.lower().endswith("morning"):
        start_hour = 9
        clean_text = clean_text[:-7].strip()
    elif clean_text.lower().endswith("afternoon"):
        start_hour = 14
        clean_text = clean_text[:-9].strip()
    elif clean_text.lower().endswith("evening"):
        start_hour = 18
        clean_text = clean_text[:-7].strip()

    state = load_state()
    new_task = {
        "id": f"dyn_{random.randint(1000, 9999)}",
        "text": clean_text,
        "persona": "general_encourage",
        "type": "dynamic",
        "valid_from_hour": start_hour,
        "is_urgent": is_urgent,
        "created_at": time.time()
    }

    if is_urgent:
        state["active_tasks"].append(new_task)
        msg = f"🚨 *Urgent task added:* '{clean_text}'"
    elif len(state["active_tasks"]) < DAILY_CAPACITY:
        state["active_tasks"].append(new_task)
        msg = f"Added: '{clean_text}'"
    else:
        state["backlog"].append(new_task)
        msg = f"List is full. '{clean_text}' added to backlog."

    if start_hour > 0:
        msg += f"\nSilent until {start_hour}:00."

    save_state(state)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    goals = load_goals()
    completed_ids = state.get("completed_ids", [])
    now = get_ist_time()
    weekday = now.weekday()

    recurring_done = [g["text"] for g in goals if weekday in g["days"] and g["id"] in completed_ids]
    recurring_pending = [g["text"] for g in goals if weekday in g["days"] and g["id"] not in completed_ids]

    dyn_done = [t["text"] for t in state["active_tasks"] if t["id"] in completed_ids]
    dyn_pending = [("🔥 " if t.get("is_urgent") else "") + t["text"]
                   for t in state["active_tasks"] if t["id"] not in completed_ids]

    msg = f"📅 *{now.strftime('%A')} — Scoreboard*\n\n"

    msg += "*Completed*\n"
    for t in recurring_done + dyn_done:
        msg += f"✅ ~{t}~\n"
    if not recurring_done and not dyn_done:
        msg += "_Nothing yet._\n"

    msg += "\n*Remaining*\n"
    for t in recurring_pending:
        msg += f"🔘 {t}\n"
    for t in dyn_pending:
        msg += f"⬜ {t}\n"
    if not recurring_pending and not dyn_pending:
        msg += "_All clear!_\n"

    backlog_count = len(state.get("backlog", []))
    if backlog_count > 0:
        msg += f"\n_Backlog: {backlog_count} items._"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def view_backlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    backlog = state.get("backlog", [])
    if not backlog:
        await update.message.reply_text("Backlog is empty.")
        return
    msg = f"*Backlog ({len(backlog)} items)*\n\n"
    for task in backlog:
        prefix = "🔥 " if task.get("is_urgent") else "• "
        msg += f"{prefix}{task['text']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    completed_ids = state.get("completed_ids", [])
    keyboard = [
        [InlineKeyboardButton(f"🗑 {t['text']}", callback_data=f"del_{t['id']}")]
        for t in state["active_tasks"] if t["id"] not in completed_ids
    ]
    if not keyboard:
        await update.message.reply_text("Nothing to delete from your active list.")
    else:
        await update.message.reply_text("Select a task to delete:", reply_markup=InlineKeyboardMarkup(keyboard))

async def done_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    goals = load_goals()
    completed_ids = state.get("completed_ids", [])
    now = get_ist_time()
    weekday = now.weekday()

    keyboard = []
    for goal in goals:
        if weekday in goal["days"] and goal["id"] not in completed_ids:
            keyboard.append([InlineKeyboardButton(f"✅ {goal['text']}", callback_data=f"done_{goal['id']}")])
    for task in state["active_tasks"]:
        if task["id"] not in completed_ids:
            keyboard.append([InlineKeyboardButton(f"✅ {task['text']}", callback_data=f"done_{task['id']}")])

    if not keyboard:
        await update.message.reply_text("🎉 No pending tasks for today!")
    else:
        await update.message.reply_text("Mark as complete:", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = " ".join(context.args)
    if not raw_text:
        msg = (
            "*How to add a daily goal:*\n"
            "Basic: `/addgoal Read 10 Pages`\n"
            "With time: `/addgoal Meditate | 7-9`\n"
            "With days (0=Mon, 6=Sun): `/addgoal German | 18-20 | 0,1,2,3,4`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    parts = [p.strip() for p in raw_text.split("|")]
    text = parts[0]
    start_hr, end_hr = 8, 22
    days = [0, 1, 2, 3, 4, 5, 6]

    if len(parts) > 1:
        try:
            times = parts[1].split("-")
            start_hr, end_hr = int(times[0]), int(times[1])
        except:
            pass

    if len(parts) > 2:
        try:
            days = [int(d.strip()) for d in parts[2].split(",")]
        except:
            pass

    new_goal = {
        "id": f"goal_{random.randint(1000, 9999)}",
        "text": text,
        "days": days,
        "persona": "general_encourage",
        "hour_start": start_hr,
        "hour_end": end_hr
    }
    goals = load_goals()
    goals.append(new_goal)
    save_goals(goals)
    await update.message.reply_text(f"🎯 *Goal added:* {text} ({start_hr}:00–{end_hr}:00)", parse_mode="Markdown")

async def delgoal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goals = load_goals()
    keyboard = [
        [InlineKeyboardButton(f"🗑 {g['text']}", callback_data=f"delg_{g['id']}")]
        for g in goals
    ]
    if not keyboard:
        await update.message.reply_text("No daily goals to delete.")
    else:
        await update.message.reply_text("Select a goal to permanently delete:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    state = load_state()

    if data.startswith("done_"):
        task_id = data[5:]
        if task_id not in state["completed_ids"]:
            state["completed_ids"].append(task_id)
            save_state(state)
        await query.edit_message_text("✅ *Done.* Marked complete.", parse_mode="Markdown")

    elif data.startswith("del_"):
        task_id = data[4:]
        state["active_tasks"] = [t for t in state["active_tasks"] if t["id"] != task_id]
        save_state(state)
        await query.edit_message_text("🗑 Task deleted.")

    elif data.startswith("delg_"):
        goal_id = data[5:]
        goals = load_goals()
        goals = [g for g in goals if g["id"] != goal_id]
        save_goals(goals)
        await query.edit_message_text("🗑 Goal permanently removed.")

# --- JOB REGISTRATION ---

def _register_jobs(job_queue, chat_id):
    for job in job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()

    job_queue.run_daily(
        morning_checkin,
        time=datetime.time(8, 0, 0, tzinfo=IST),
        chat_id=chat_id,
        name=str(chat_id)
    )
    job_queue.run_daily(
        midday_checkin,
        time=datetime.time(13, 0, 0, tzinfo=IST),
        chat_id=chat_id,
        name=str(chat_id)
    )
    job_queue.run_daily(
        evening_checkin,
        time=datetime.time(21, 0, 0, tzinfo=IST),
        chat_id=chat_id,
        name=str(chat_id)
    )

async def post_init(application: Application):
    try:
        await application.bot.send_message(
            chat_id=MY_CHAT_ID,
            text="🤖 *Uplifting Bot v3 is online.*\nCheck-ins at 8am, 1pm, 9pm IST. Talk to me anytime.",
            parse_mode="Markdown"
        )
        _register_jobs(application.job_queue, MY_CHAT_ID)
    except Exception as e:
        print(f"post_init error: {e}")

# --- MAIN ---

if __name__ == "__main__":
    print(f"🤖 Starting bot. Instance: {str(uuid.uuid4())[:8]}")
    Thread(target=start_server, daemon=True).start()

    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        print("CRITICAL: TELEGRAM_TOKEN not set.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_task))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("backlog", view_backlog))
    app.add_handler(CommandHandler("delete", delete_menu))
    app.add_handler(CommandHandler("done", done_menu))
    app.add_handler(CommandHandler("addgoal", add_goal))
    app.add_handler(CommandHandler("delgoal", delgoal_menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running.")
    app.run_polling()
