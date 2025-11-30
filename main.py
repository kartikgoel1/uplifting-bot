import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# --- DUMMY WEB SERVER TO KEEP RENDER ALIVE ---
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

# Start the web server in a background thread
Thread(target=start_server, daemon=True).start()
import logging
import json
import random
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURATION: THE BRAIN ---

# 1. The Philosophers (The Vibe Engine)
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

# 2. The Goals (Static Configuration)
# Note: We use 0=Mon, 6=Sun for weekdays
GOALS_CONFIG = [
    {"id": "work_prod", "text": "1 hr Product Knowledge/Integration", "days": [0,1,2,3,4], "persona": "alain_meaning", "hour_start": 9, "hour_end": 12},
    {"id": "work_build", "text": "Build Product / Tech Blogs", "days": [5,6], "persona": "maker_creativity", "hour_start": 10, "hour_end": 14},
    {"id": "work_dsa", "text": "1 DSA Question", "days": [0,1,2,3,4,5,6], "persona": "stoic_resilience", "hour_start": 14, "hour_end": 17},
    {"id": "work_german", "text": "German Lesson", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 18, "hour_end": 20},
    {"id": "pers_meditate", "text": "Meditate", "days": [0,1,2,3,4,5,6], "persona": "mindful_learning", "hour_start": 7, "hour_end": 9},
    {"id": "pers_water", "text": "Drink 3L Water", "days": [0,1,2,3,4,5,6], "persona": "general_encourage", "hour_start": 10, "hour_end": 20},
]

# --- STATE MANAGEMENT ---
# We use a simple JSON file to remember what you finished today.
STATE_FILE = "user_state.json"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            # If the date in file is not today, reset the state
            if data.get("date") != str(datetime.date.today()):
                return {"date": str(datetime.date.today()), "completed": [], "dynamic_tasks": []}
            return data
    except FileNotFoundError:
        return {"date": str(datetime.date.today()), "completed": [], "dynamic_tasks": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# --- BOT LOGIC ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_first_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Hello {user_first_name}. I am online.\n\n"
        "I will gently nudge you about your goals.\n"
        "To add a one-off task (like 'Iron clothes'), type:\n"
        "`/add Iron clothes`\n\n"
        "To test a nudge right now, type `/test`."
    )
    # Store chat_id to send proactive messages later
    context.job_queue.run_repeating(check_schedule, interval=60, first=10, chat_id=update.effective_chat.id)

async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_text = " ".join(context.args)
    if not task_text:
        await update.message.reply_text("Please describe the task. Example: `/add Iron clothes`")
        return

    state = load_state()
    # Create a simple dynamic task object
    new_task = {
        "id": f"dyn_{random.randint(1000,9999)}",
        "text": task_text,
        "persona": "general_encourage",
        "type": "dynamic"
    }
    state["dynamic_tasks"].append(new_task)
    save_state(state)
    await update.message.reply_text(f"✍️ Added: '{task_text}'. I'll keep it in mind.")

async def send_nudge(context: ContextTypes.DEFAULT_TYPE, chat_id, task):
    # Pick a random quote based on the task's persona
    persona = task.get("persona", "general_encourage")
    quote = random.choice(QUOTES.get(persona, QUOTES["general_encourage"]))
    
    message = f"💡 *A thought for you:*\n_{quote}_\n\n👉 **Task:** {task['text']}"
    
    keyboard = [
        [InlineKeyboardButton("✅ I Did It", callback_data=f"done_{task['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(chat_id=chat_id, text=message, reply_markup=reply_markup, parse_mode="Markdown")

async def check_schedule(context: ContextTypes.DEFAULT_TYPE):
    """Runs every minute to check if we should send a nudge."""
    job = context.job
    chat_id = job.chat_id
    now = datetime.datetime.now()
    current_hour = now.hour
    current_weekday = now.weekday()
    
    state = load_state()
    completed_ids = state["completed"]
    
    # 1. Collect all candidates (Static + Dynamic)
    candidates = []
    
    # Check Static Goals
    for goal in GOALS_CONFIG:
        if goal["id"] not in completed_ids:
            if current_weekday in goal["days"]:
                if goal["hour_start"] <= current_hour < goal["hour_end"]:
                    candidates.append(goal)
    
    # Check Dynamic Goals
    for task in state["dynamic_tasks"]:
        if task["id"] not in completed_ids:
            candidates.append(task)
            
    # 2. Decision Logic: Don't spam. Just pick ONE random pending thing occasionally.
    # To prevent spamming every minute, we add a 10% chance to trigger IF there are candidates.
    # (Adjust this probability for more/less frequency)
    if candidates and random.random() < 0.10: 
        chosen_task = random.choice(candidates)
        await send_nudge(context, chat_id, chosen_task)

async def test_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force a nudge immediately for testing."""
    state = load_state()
    # Mock a task if everything is done
    mock_task = {"id": "test_task", "text": "Test Nudge (You are building this!)", "persona": "maker_creativity"}
    await send_nudge(context, update.effective_chat.id, mock_task)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # ACK the button click
    
    data = query.data
    if data.startswith("done_"):
        task_id = data.split("_")[1]
        
        # Update State
        state = load_state()
        if task_id not in state["completed"]:
            state["completed"].append(task_id)
            save_state(state)
            
        await query.edit_message_text(text=f"✅ **Well done.** Task marked complete.\n\n_Resting state updated._", parse_mode="Markdown")

# --- MAIN EXECUTION ---

if __name__ == '__main__':
    # REPLACE WITH YOUR ACTUAL TOKEN
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        print("Error: No token found. Make sure TELEGRAM_TOKEN is set in your environment.")
        exit(1)
    
    application = ApplicationBuilder().token(TOKEN).build()
    
    # Add Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_task))
    application.add_handler(CommandHandler("test", test_nudge))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Bot is running... Press Ctrl+C to stop.")
    application.run_polling()
