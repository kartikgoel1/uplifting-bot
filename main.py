import logging
import json
import random
import datetime
import os
import uuid
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler, Application

# --- 0. TIMEZONE CONFIGURATION (IST) ---
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def get_ist_time():
    """Returns the current time in IST."""
    return datetime.datetime.now(IST)

# --- 1. DUMMY WEB SERVER (KEEPS RENDER ALIVE) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    
    # This handles UptimeRobot pings to prevent "501" errors
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# --- 2. CONFIGURATION ---
QUOTES = {
    "alain_meaning": [
        "“Work is one of the ways in which we can dignify our suffering.” — Alain de Botton",
        "“Anxiety is the handmaiden of contemporary ambition.” — Alain de Botton",
        "“It is not that we are not good enough, but that we are judging ourselves by a standard that is impossible.” — Alain de Botton"
    ],
    "maker_creativity": [
        "“The way to do great work is to love what you do.” — Steve Jobs",
        "“Make something people want.” — Paul Graham",
        "“Amateurs sit and wait for inspiration, the rest of us just get up and go to work.” — Stephen King"
    ],
    "stoic_resilience": [
        "“We suffer more often in imagination than in reality.” — Seneca",
        "“The impediment to action advances action. What stands in the way becomes the way.” — Marcus Aurelius",
        "“Do not seek for things to happen the way you want them to; rather, wish that what happens happen the way it happens.” — Epictetus"
    ],
    "mindful_learning": [
        "“The present moment is filled with joy and happiness. If you are attentive, you will see it.” — Thich Nhat Hanh",
        "“Awareness is the greatest agent for change.” — Eckhart Tolle",
        "“Don’t worry about the future. Just be here now.” — Diana Winston"
    ],
    "general_encourage": [
        "“The secret of getting ahead is getting started.” — Mark Twain",
        "“Small progress is still progress.”",
        "“You don’t have to see the whole staircase, just take the first step.” — Martin Luther King Jr."
    ]
}

GOALS_CONFIG = [
    {"id": "work_prod", "text": "1 hr Product Knowledge/Integration", "days": [0,1,2,3,4], "persona": "alain_meaning", "hour_start": 9, "hour_end": 12},
    {"id": "work_build", "text": "Build Product / Tech Blogs", "days": [5,6], "persona": "maker_creativity", "hour_start": 10, "hour_end": 14},
    {"id": "work_dsa", "text": "1 DSA Question", "days": [0,1,2,3,4,5,6], "persona": "stoic_resilience", "hour_start": 14, "hour_end": 17},
    {"id": "work_german", "text": "German Lesson", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 18, "hour_end": 20},
    {"id": "pers_meditate", "text": "Meditate", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 7, "hour_end": 9},
    {"id": "pers_water", "text": "Drink 3L Water", "days": [0,1,2,3,4,5,6], "persona": "general_encourage", "hour_start": 10, "hour_end": 20},
]

# --- 3. STATE MANAGEMENT ---
STATE_FILE = "user_state.json"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if data.get("date") != str(get_ist_time().date()):
                return {"date": str(get_ist_time().date()), "completed": [], "dynamic_tasks": []}
            return data
    except FileNotFoundError:
        return {"date": str(get_ist_time().date()), "completed": [], "dynamic_tasks": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# --- 4. BOT LOGIC ---

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_text = " ".join(context.args)
    if not task_text:
        await update.message.reply_text("Please describe the task. Example: `/add Clean room`")
        return

    state = load_state()
    new_task = {
        "id": f"dyn_{random.randint(1000,9999)}",
        "text": task_text,
        "persona": "general_encourage",
        "type": "dynamic"
    }
    state["dynamic_tasks"].append(new_task)
    save_state(state)
    await update.message.reply_text(f"✍️ Added: '{task_text}'. I'll keep it in mind.")

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    completed_ids = state["completed"]
    now = get_ist_time()
    current_weekday = now.weekday()
    
    message = f"📋 **Your Agenda for Today ({now.strftime('%A')})**\n\n"
    
    # Recurring Goals
    message += "*Recurring:*\n"
    has_recurring = False
    for goal in GOALS_CONFIG:
        if current_weekday in goal["days"]:
            has_recurring = True
            status = "✅" if goal["id"] in completed_ids else "⬜️"
            message += f"{status} {goal['text']}\n"
            
    if not has_recurring:
        message += "_No recurring goals for today._\n"
        
    # Dynamic Tasks
    message += "\n*One-Offs:*\n"
    if not state["dynamic_tasks"]:
        message += "_No extra tasks added._\n"
    else:
        for task in state["dynamic_tasks"]:
            status = "✅" if task["id"] in completed_ids else "⬜️"
            message += f"{status} {task['text']}\n"

    await update.message.reply_text(message, parse_mode="Markdown")

async def done_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    completed_ids = state["completed"]
    now = get_ist_time()
    current_weekday = now.weekday()
    
    keyboard = []
    
    # Add Pending Recurring Goals
    for goal in GOALS_CONFIG:
        if current_weekday in goal["days"]:
            if goal["id"] not in completed_ids:
                btn = InlineKeyboardButton(f"✅ {goal['text']}", callback_data=f"done_{goal['id']}")
                keyboard.append([btn])
                
    # Add Pending Dynamic Tasks
    for task in state["dynamic_tasks"]:
        if task["id"] not in completed_ids:
            btn = InlineKeyboardButton(f"✅ {task['text']}", callback_data=f"done_{task['id']}")
            keyboard.append([btn])
            
    if not keyboard:
        await update.message.reply_text("🎉 You have no pending tasks for today!")
    else:
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select a task to mark as complete:", reply_markup=reply_markup)

async def check_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    ist_now = get_ist_time()
    await update.message.reply_text(
        f"🕒 **Internal Clock Check:**\n"
        f"🌍 UTC: {utc_now.strftime('%H:%M')}\n"
        f"🇮🇳 IST: {ist_now.strftime('%H:%M')}"
    )

async def send_nudge(context: ContextTypes.DEFAULT_TYPE, chat_id, task):
    persona = task.get("persona", "general_encourage")
    quote = random.choice(QUOTES.get(persona, QUOTES["general_encourage"]))
    
    message = f"💡 *A thought for you:*\n_{quote}_\n\n👉 **Task:** {task['text']}"
    keyboard = [[InlineKeyboardButton("✅ I Did It", callback_data=f"done_{task['id']}")]]
    await context.bot.send_message(chat_id=chat_id, text=message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def check_schedule(context: ContextTypes.DEFAULT_TYPE):
    # Retrieve the chat_id from the job context
    chat_id = context.job.chat_id
    
    now = get_ist_time()
    current_hour = now.hour
    current_weekday = now.weekday()
    
    state = load_state()
    completed_ids = state["completed"]
    
    candidates = []
    
    for goal in GOALS_CONFIG:
        if goal["id"] not in completed_ids:
            if current_weekday in goal["days"]:
                if goal["hour_start"] <= current_hour < goal["hour_end"]:
                    candidates.append(goal)
    
    for task in state["dynamic_tasks"]:
        if task["id"] not in completed_ids:
            candidates.append(task)
            
    if candidates and random.random() < 0.10: 
        chosen_task = random.choice(candidates)
        await send_nudge(context, chat_id, chosen_task)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data.startswith("done_"):
        task_id = data.split("_")[1]
        state = load_state()
        if task_id not in state["completed"]:
            state["completed"].append(task_id)
            save_state(state)
        await query.edit_message_text(text=f"✅ **Well done.** Task marked complete.\n\n_Resting state updated._", parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is the manual start command
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Hello! I am online (IST).\nType `/list` to see your day.")
    # Remove old jobs to avoid duplicates if user types /start multiple times
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
    context.job_queue.run_repeating(check_schedule, interval=60, first=10, chat_id=chat_id, name=str(chat_id))

# --- 5. THE AUTO-START (AMNESIA FIX) ---
async def post_init(application: Application):
    # ==========================================
    # ⚠️ INPUT REQUIRED: PUT YOUR CHAT ID HERE
    # Get this from @userinfobot
    MY_CHAT_ID = 2071012504  
    # ==========================================
    
    print(f"🤖 Bot restarting. Auto-starting timer for ID: {MY_CHAT_ID}")
    
    try:
        await application.bot.send_message(chat_id=MY_CHAT_ID, text="🤖 **System Restarted.** I am back online and watching the clock.")
        # Start the timer automatically without needing /start
        application.job_queue.run_repeating(check_schedule, interval=60, first=10, chat_id=MY_CHAT_ID, name=str(MY_CHAT_ID))
    except Exception as e:
        print(f"Failed to auto-start: {e}")

# --- 6. EXECUTION ---
if __name__ == '__main__':
    # Startup Fingerprint
    INSTANCE_ID = str(uuid.uuid4())[:8]
    print(f"🤖 BOT STARTING. Instance ID: {INSTANCE_ID}")

    # Start Web Server for UptimeRobot
    Thread(target=start_server, daemon=True).start()

    TOKEN = os.getenv("TELEGRAM_TOKEN")
    
    if not TOKEN:
        print("CRITICAL ERROR: TELEGRAM_TOKEN not found.")
    else:
        # We add post_init here to handle the auto-start
        application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("add", add_task))
        application.add_handler(CommandHandler("list", list_tasks)) 
        application.add_handler(CommandHandler("done", done_menu)) 
        application.add_handler(CommandHandler("time", check_time))
        application.add_handler(CallbackQueryHandler(button_handler))
        
        application.run_polling()
