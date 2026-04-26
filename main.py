import logging
import json
import random
import datetime
import os
import uuid
import time
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
import pymongo 

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, Application

# --- 0. TIMEZONE CONFIGURATION (IST) ---
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.datetime.now(IST)

# --- 1. DUMMY WEB SERVER ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# --- 2. CONFIGURATION ---

def load_quotes():
    try:
        with open("quotes.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Error loading quotes.json: {e}")
        return {
            "general_encourage": ["“Keep going.”", "“You got this.”"]
        }

QUOTES = load_quotes()

# Renamed to DEFAULT_GOALS. We only use this ONCE to seed the database.
DEFAULT_GOALS = [
    {"id": "work_prod", "text": "1 hr Product Knowledge/Integration", "days": [0,1,2,3,4], "persona": "alain_meaning", "hour_start": 9, "hour_end": 12},
    {"id": "work_build", "text": "Build Product / Tech Blogs", "days": [5,6], "persona": "maker_creativity", "hour_start": 10, "hour_end": 14},
    {"id": "work_dsa", "text": "1 DSA Question", "days": [0,1,2,3,4,5,6], "persona": "stoic_resilience", "hour_start": 14, "hour_end": 17},
    {"id": "work_german", "text": "German Lesson", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 18, "hour_end": 20},
    {"id": "pers_meditate", "text": "Meditate", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 7, "hour_end": 9},
    {"id": "pers_water", "text": "Drink 3L Water", "days": [0,1,2,3,4,5,6], "persona": "general_encourage", "hour_start": 10, "hour_end": 20},
]

# --- 3. DATABASE MANAGEMENT ---
DAILY_CAPACITY = 15

def get_db():
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("⚠️ NO DATABASE FOUND.")
        return None
    client = pymongo.MongoClient(mongo_uri)
    return client["uplifting_bot_db"]["user_state_v2"]

def load_state():
    today_str = str(get_ist_time().date())
    default_state = {
        "date": today_str, 
        "active_tasks": [], 
        "backlog": [],      
        "completed_ids": [], 
        "last_nudge_timestamp": 0
    }
    
    collection = get_db()
    if collection is None: return default_state

    data = collection.find_one({"_id": "current_user"})
    if not data: return default_state
    
    if data.get("date") != today_str:
        old_active = data.get("active_tasks", []) + data.get("dynamic_tasks", [])
        old_backlog = data.get("backlog", [])
        completed_ids = data.get("completed_ids", [])
        
        pool = []
        for task in old_active + old_backlog:
            if task["id"] not in completed_ids:
                task["valid_from_hour"] = 0
                pool.append(task)
        
        pool.sort(key=lambda x: (not x.get("is_urgent", False), x.get("created_at", time.time())))
        
        new_active = pool[:DAILY_CAPACITY]
        new_backlog = pool[DAILY_CAPACITY:]
        last_nudge = data.get("last_nudge_timestamp", 0)
        
        new_state = {
            "date": today_str,
            "active_tasks": new_active,
            "backlog": new_backlog,
            "completed_ids": [], 
            "last_nudge_timestamp": last_nudge
        }
        save_state(new_state)
        return new_state
        
    return data

def save_state(state):
    collection = get_db()
    if collection is None: return
    state["_id"] = "current_user"
    collection.replace_one({"_id": "current_user"}, state, upsert=True)

# --- NEW: GOALS DATABASE MANAGEMENT ---
def load_goals():
    collection = get_db()
    if collection is None: return DEFAULT_GOALS
    
    data = collection.find_one({"_id": "user_goals"})
    if not data:
        # Seed the database with defaults on first run
        default_data = {"_id": "user_goals", "goals": DEFAULT_GOALS}
        collection.insert_one(default_data)
        return DEFAULT_GOALS
    return data.get("goals", [])

def save_goals(goals_list):
    collection = get_db()
    if collection is None: return
    collection.replace_one({"_id": "user_goals"}, {"_id": "user_goals", "goals": goals_list}, upsert=True)

# --- 4. BOT LOGIC ---

async def add_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = " ".join(context.args)
    if not raw_text:
        msg = "⚙️ **How to add a daily goal:**\n"
        msg += "Basic: `/addgoal Read 10 Pages`\n"
        msg += "With Time: `/addgoal Meditate | 7-9`\n"
        msg += "With Days (0=Mon, 6=Sun): `/addgoal German | 18-20 | 0,1,2,3,4`"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    parts = [p.strip() for p in raw_text.split("|")]
    text = parts[0]
    
    # Defaults
    start_hr, end_hr = 8, 22
    days = [0,1,2,3,4,5,6]
    
    # Parse Time if provided
    if len(parts) > 1:
        try:
            times = parts[1].split("-")
            start_hr = int(times[0])
            end_hr = int(times[1])
        except:
            pass # fallback to defaults if they typed it wrong
            
    # Parse Days if provided
    if len(parts) > 2:
        try:
            days = [int(d.strip()) for d in parts[2].split(",")]
        except:
            pass

    new_goal = {
        "id": f"goal_{random.randint(1000,9999)}",
        "text": text,
        "days": days,
        "persona": "general_encourage",
        "hour_start": start_hr,
        "hour_end": end_hr
    }
    
    goals = load_goals()
    goals.append(new_goal)
    save_goals(goals)
    
    await update.message.reply_text(f"🎯 **New Daily Goal Added!**\n{text} (Between {start_hr}:00 and {end_hr}:00)", parse_mode="Markdown")

async def delgoal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goals = load_goals()
    keyboard = []
    for g in goals:
        btn = InlineKeyboardButton(f"🗑 {g['text']}", callback_data=f"delg_{g['id']}")
        keyboard.append([btn])
            
    if not keyboard:
        await update.message.reply_text("You have no active daily goals.")
    else:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select a Daily Goal to **Permanently Delete**:", reply_markup=reply_markup)


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = " ".join(context.args)
    if not raw_text:
        await update.message.reply_text("Example: `/add Pay bills urgent` or `/add Laundry evening`")
        return

    lower_text = raw_text.lower()
    start_hour = 0
    is_urgent = False
    clean_text = raw_text
    
    if "urgent" in lower_text:
        is_urgent = True
        import re
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
        "id": f"dyn_{random.randint(1000,9999)}",
        "text": clean_text,
        "persona": "general_encourage",
        "type": "dynamic",
        "valid_from_hour": start_hour,
        "is_urgent": is_urgent,
        "created_at": time.time()
    }
    
    active_count = len(state["active_tasks"])
    msg = ""
    
    if is_urgent:
        state["active_tasks"].append(new_task)
        msg = f"🚨 **Urgent Task Added.**\n'{clean_text}' is now on your active list (Priority)."
    elif active_count < DAILY_CAPACITY:
        state["active_tasks"].append(new_task)
        msg = f"✍️ Added: '{clean_text}' to today's list."
    else:
        state["backlog"].append(new_task)
        msg = f"📦 **List Full ({active_count}/{DAILY_CAPACITY}).**\nSaved '{clean_text}' to Backlog."

    if start_hour > 0:
        msg += f"\n⏳ Silent until {start_hour}:00."

    save_state(state)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    goals = load_goals() # Fetch goals from DB
    completed_ids = state.get("completed_ids", [])
    now = get_ist_time()
    current_weekday = now.weekday()
    
    recurring_pending = []
    recurring_done = []
    
    for goal in goals:
        if current_weekday in goal["days"]:
            if goal["id"] in completed_ids:
                recurring_done.append(goal['text'])
            else:
                recurring_pending.append(goal['text'])
    
    active_tasks = state["active_tasks"]
    dyn_pending = []
    dyn_done = []
    
    for task in active_tasks:
        if task["id"] in completed_ids:
            dyn_done.append(task['text'])
        else:
            txt = task['text']
            if task.get("is_urgent"): txt = "🔥 " + txt
            dyn_pending.append(txt)

    message = f"📅 **Daily Scoreboard ({now.strftime('%A')})**\n"
    message += f"Capacity: {len(active_tasks)}/{DAILY_CAPACITY}\n\n"
    
    message += f"🏆 **Completed**\n"
    for t in recurring_done + dyn_done: message += f"✅ ~{t}~\n"
    if not (recurring_done + dyn_done): message += "_No wins yet._\n"
        
    message += "\n🧱 **Remaining**\n"
    for t in recurring_pending: message += f"🔘 {t} (Goal)\n"
    for t in dyn_pending: message += f"⬜ {t}\n"
    if not (recurring_pending + dyn_pending): message += "_All clear!_\n"
        
    backlog_count = len(state.get("backlog", []))
    if backlog_count > 0:
        message += f"\n📦 _Backlog: {backlog_count} items waiting._"

    await update.message.reply_text(message, parse_mode="Markdown")

async def view_backlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    backlog = state.get("backlog", [])
    
    if not backlog:
        await update.message.reply_text("📦 **Backlog is empty.**", parse_mode="Markdown")
        return

    message = f"📦 **Backlog ({len(backlog)} items)**\n\n"
    for task in backlog:
        prefix = "🔥 " if task.get("is_urgent") else "💤 "
        message += f"{prefix} {task['text']}\n"
        
    await update.message.reply_text(message, parse_mode="Markdown")

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    completed_ids = state.get("completed_ids", [])
    
    keyboard = []
    for task in state["active_tasks"]:
        if task["id"] not in completed_ids:
            btn = InlineKeyboardButton(f"🗑 {task['text']}", callback_data=f"del_{task['id']}")
            keyboard.append([btn])
            
    if not keyboard:
        await update.message.reply_text("Nothing to delete from your active list.")
    else:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select a task to **Permanently Delete**:", reply_markup=reply_markup)

async def done_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    goals = load_goals()
    completed_ids = state.get("completed_ids", [])
    now = get_ist_time()
    current_weekday = now.weekday()
    
    keyboard = []
    
    for goal in goals:
        if current_weekday in goal["days"]:
            if goal["id"] not in completed_ids:
                btn = InlineKeyboardButton(f"✅ {goal['text']}", callback_data=f"done_{goal['id']}")
                keyboard.append([btn])
                
    for task in state["active_tasks"]:
        if task["id"] not in completed_ids:
            btn = InlineKeyboardButton(f"✅ {task['text']}", callback_data=f"done_{task['id']}")
            keyboard.append([btn])
            
    if not keyboard:
        await update.message.reply_text("🎉 You have no pending tasks for today!")
    else:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select a task to mark as complete:", reply_markup=reply_markup)

async def check_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ist_now = get_ist_time()
    await update.message.reply_text(f"🇮🇳 IST: {ist_now.strftime('%H:%M')}")

async def send_nudge(context: ContextTypes.DEFAULT_TYPE, chat_id, task):
    persona = task.get("persona", "general_encourage")
    quote = random.choice(QUOTES.get(persona, QUOTES["general_encourage"]))
    
    message = f"💡 *A thought for you:*\n_{quote}_\n\n👉 **Task:** {task['text']}"
    keyboard = [[InlineKeyboardButton("✅ I Did It", callback_data=f"done_{task['id']}")]]
    
    state = load_state()
    state["last_nudge_timestamp"] = get_ist_time().timestamp()
    save_state(state)
    
    await context.bot.send_message(chat_id=chat_id, text=message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def check_schedule(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    now = get_ist_time()
    current_hour = now.hour
    current_weekday = now.weekday()
    state = load_state()
    goals = load_goals()
    
    last_nudge = state.get("last_nudge_timestamp", 0)
    current_ts = now.timestamp()
    if (current_ts - last_nudge) < 3600:
        return 

    completed_ids = state.get("completed_ids", [])
    candidates = []
    
    for goal in goals:
        if goal["id"] not in completed_ids:
            if current_weekday in goal["days"]:
                if goal["hour_start"] <= current_hour < goal["hour_end"]:
                    candidates.append(goal)
    
    for task in state["active_tasks"]:
        if task["id"] not in completed_ids:
            start_time = task.get("valid_from_hour", 0)
            if current_hour >= start_time:
                candidates.append(task)
            
    if candidates: 
        chosen_task = random.choice(candidates)
        await send_nudge(context, chat_id, chosen_task)

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
        await query.edit_message_text(text=f"✅ **Well done.** Task marked complete.", parse_mode="Markdown")

    elif data.startswith("del_"):
        task_id = data[4:]
        state["active_tasks"] = [t for t in state["active_tasks"] if t["id"] != task_id]
        save_state(state)
        await query.edit_message_text(text=f"🗑 Task deleted.", parse_mode="Markdown")
        
    elif data.startswith("delg_"):
        # NEW: Delete a Daily Goal
        goal_id = data[5:]
        goals = load_goals()
        goals = [g for g in goals if g["id"] != goal_id]
        save_goals(goals)
        await query.edit_message_text(text=f"🗑 Daily Goal permanently removed.", parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Hello! I am online (IST).\nType `/list` to see your day.")
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
    context.job_queue.run_repeating(check_schedule, interval=60, first=10, chat_id=chat_id, name=str(chat_id))

async def post_init(application: Application):
    # ==========================================
    # ⚠️ INPUT REQUIRED: PUT YOUR CHAT ID HERE
    MY_CHAT_ID = 2071012504 
    # ==========================================
    try:
        await application.bot.send_message(chat_id=MY_CHAT_ID, text="🤖 **Smart Scheduler V2.1**\n(Goals migrated to Database!)")
        application.job_queue.run_repeating(check_schedule, interval=60, first=10, chat_id=MY_CHAT_ID, name=str(MY_CHAT_ID))
    except Exception as e:
        print(f"Failed to auto-start: {e}")

if __name__ == '__main__':
    INSTANCE_ID = str(uuid.uuid4())[:8]
    print(f"🤖 BOT STARTING. Instance ID: {INSTANCE_ID}")
    
    Thread(target=start_server, daemon=True).start()
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    
    if not TOKEN:
        print("CRITICAL ERROR: TELEGRAM_TOKEN not found.")
    else:
        application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        application.add_handler(CommandHandler("start", start))
        
        # Tasks Commands
        application.add_handler(CommandHandler("add", add_task))
        application.add_handler(CommandHandler("list", list_tasks)) 
        application.add_handler(CommandHandler("backlog", view_backlog)) 
        application.add_handler(CommandHandler("delete", delete_menu)) 
        
        # Goals Commands (NEW)
        application.add_handler(CommandHandler("addgoal", add_goal)) 
        application.add_handler(CommandHandler("delgoal", delgoal_menu)) 
        
        application.add_handler(CommandHandler("done", done_menu)) 
        application.add_handler(CommandHandler("time", check_time))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.run_polling()
