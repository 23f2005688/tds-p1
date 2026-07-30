import json
import time
import os
import subprocess
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")
LOG_URL = "https://raw.githubusercontent.com/23f2005688/tds-p1/refs/heads/main/run.jsonl"  
# -------------------------------------------

client = OpenAI(base_url="https://aipipe.org/openai/v1", api_key=AIPIPE_TOKEN)
LOG_FILE = "run.jsonl"

conversation_history = {}

# throttle git pushes so we're not committing on every single message
_events_since_push = 0
PUSH_EVERY_N_EVENTS = 4

def log_event(event: dict):
    event["timestamp"] = time.time()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def push_log():
    global _events_since_push
    _events_since_push += 1
    if _events_since_push < PUSH_EVERY_N_EVENTS:
        return
    _events_since_push = 0
    try:
        subprocess.run(["git", "add", LOG_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "log update"], check=True)
        subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError:
        pass  # nothing new to commit, or push failed — don't crash the bot
DRY_RUN = False  # set to False only for your final real test / submission
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text
    log_event({"type": "incoming", "chat_id": chat_id, "text": user_text})

    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})

    system_prompt = (
        "You are a careful data analyst. The user's LAST message asks a data-analysis "
        "question. Work out the real answer (use any public data you know, e.g. MOSPI "
        "statistics, general world knowledge, or arithmetic on numbers given in the "
        "message), shaped exactly as the question specifies. "
        "Reply with ONLY a JSON object with exactly two top-level keys: "
        "\"answer\" (containing your answer in the exact shape requested — this may "
        "itself be a nested object, a string, a number, etc., depending on what the "
        "question asks for) and \"log_url\" (leave this as the string \"PLACEHOLDER\" "
        "— it will be replaced automatically). "
        "No explanation, no markdown, no code fences — just the raw JSON, nothing else."
    )

    if DRY_RUN:
        reply_text = '{"answer": {"state": "Assam"}, "log_url": "PLACEHOLDER"}'
    else:
        try:
            response = client.chat.completions.create(
                model="gpt-5-mini",
                messages=[{"role": "system", "content": system_prompt}] + history[-6:],
            )
            reply_text = response.choices[0].message.content.strip()
        except Exception as e:
            log_event({"type": "error", "chat_id": chat_id, "error": str(e)})
            await update.message.reply_text(json.dumps({"error": "internal_error", "log_url": LOG_URL}))
            return

    history.append({"role": "assistant", "content": reply_text})

    try:
        parsed = json.loads(reply_text)
    except json.JSONDecodeError:
        try:
            start, end = reply_text.find("{"), reply_text.rfind("}")
            parsed = json.loads(reply_text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            log_event({"type": "parse_error", "chat_id": chat_id, "raw": reply_text})
            parsed = {"answer": None}

    # Always force the correct log_url — never trust the model's placeholder,
    # and always ensure only the two required top-level keys exist.
    final_parsed = {
        "answer": parsed.get("answer"),
        "log_url": LOG_URL,
    }
    final_reply = json.dumps(final_parsed)

    log_event({"type": "outgoing", "chat_id": chat_id, "text": final_reply})
    await update.message.reply_text(final_reply)

    push_log()
app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
print("Bot is running... (Ctrl+C to stop)")
app.run_polling()