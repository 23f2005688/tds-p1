import json
import time
import os
import subprocess
import requests
from threading import Thread
from dotenv import load_dotenv
from flask import Flask
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")
LOG_URL = "https://raw.githubusercontent.com/23f2005688/tds-p1/refs/heads/main/run.jsonl"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g., "23f2005688/tds-p1"

LOG_FILE = "run.jsonl"
conversation_history = {}

client = OpenAI(base_url="https://aipipe.org/openai/v1", api_key=AIPIPE_TOKEN)

# ---------------- Flask Server for Render Health Check ----------------
flask_app = Flask(__name__)

@flask_app.route("/health")
def health():
    return {"ok": True}

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

def self_ping():
    base_url = os.getenv("BASE_URL")
    if not base_url:
        return
    while True:
        time.sleep(600)  # every 10 minutes
        try:
            requests.get(f"{base_url}/health", timeout=10)
        except Exception:
            pass

# ---------------- Git & Logging Helpers ----------------
def configure_git():
    if GITHUB_TOKEN and GITHUB_REPO:
        remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
        # Check if 'origin' already exists
        check_origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True)
        
        if check_origin.returncode != 0:
            subprocess.run(["git", "remote", "add", "origin", remote_url], check=False)
        else:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=False)

        subprocess.run(["git", "config", "user.email", "bot@example.com"], check=False)
        subprocess.run(["git", "config", "user.name", "bot"], check=False)

def log_event(event: dict):
    event["timestamp"] = time.time()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def push_log():
    try:
        subprocess.run(["git", "add", LOG_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "log update"], check=False)
        # Explicitly specify origin main for automated environments
        push_res = subprocess.run(
            ["git", "push", "origin", "main"], 
            capture_output=True, 
            text=True
        )
        if push_res.returncode != 0:
            print(f"[Git Push Error]: {push_res.stderr}")
        else:
            print("[Git Push Success]")
    except Exception as e:
        print(f"[Git Exception]: {e}")

# ---------------- Telegram Handler ----------------
DRY_RUN = False 

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
                model="gpt-4o-mini",
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

    final_parsed = {
        "answer": parsed.get("answer"),
        "log_url": LOG_URL,
    }
    final_reply = json.dumps(final_parsed)

    log_event({"type": "outgoing", "chat_id": chat_id, "text": final_reply})
    await update.message.reply_text(final_reply)

    # Push to GitHub on every event
    push_log()

# ---------------- Application Entrypoint ----------------
if __name__ == "__main__":
    # 1. Configure Git BEFORE polling starts
    configure_git()

    # 2. Start web server threads
    Thread(target=run_flask, daemon=True).start()
    Thread(target=self_ping, daemon=True).start()

    # 3. Start Telegram Bot
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is running... (Ctrl+C to stop)")
    app.run_polling()