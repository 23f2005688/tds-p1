import json
import time
import os
import subprocess
import threading
from dotenv import load_dotenv

import requests
from flask import Flask

from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

# ---------------- Environment ----------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AIPIPE_TOKEN = os.getenv("AIPIPE_TOKEN")
LOG_URL = "https://raw.githubusercontent.com/23f2005688/tds-p1/refs/heads/main/run.jsonl"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g., "23f2005688/tds-p1"

LOG_FILE = "run.jsonl"
MAX_LOG_ENTRIES = 10  # Keeps only the recent logs to prevent infinite growth
DRY_RUN = False

# In-memory conversation history
conversation_history = {}  # {chat_id: [{"role":..., "content":...}, ...]}

client = OpenAI(base_url="https://aipipe.org/openai/v1", api_key=AIPIPE_TOKEN)

# ---------------- Flask Server for Render Health Check ----------------
flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return "Bot is active and running!", 200

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

# ---------------- Git & Logging ----------------
log_lock = threading.Lock()
git_lock = threading.Lock()

push_queue_event = threading.Event()
push_thread_stop = threading.Event()


def configure_git():
    """
    Configure git remote and identity safely.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return

    remote_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    # Ensure we are in a git repo
    subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=False, capture_output=True)

    # Check if 'origin' exists; if not, add it; else set it
    check_origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True
    )
    if check_origin.returncode != 0:
        subprocess.run(["git", "remote", "add", "origin", remote_url], check=False)
    else:
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=False)

    subprocess.run(["git", "config", "user.email", "bot@example.com"], check=False)
    subprocess.run(["git", "config", "user.name", "bot"], check=False)


def ensure_log_file_exists():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            pass


def log_event(event: dict):
    """
    Append a JSON line to run.jsonl safely and trim to MAX_LOG_ENTRIES.
    """
    event["timestamp"] = time.time()
    with log_lock:
        # Read existing entries if any
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        logs.append(line.strip())
        
        # Append new event
        logs.append(json.dumps(event, ensure_ascii=False))
        
        # Keep only the most recent N entries
        if len(logs) > MAX_LOG_ENTRIES:
            logs = logs[-MAX_LOG_ENTRIES:]
            
        # Write back trimmed logs
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            for log in logs:
                f.write(log + "\n")


def git_sync_and_push():
    """
    Non-destructive push:
    - fetch/pull with rebase disabled to avoid losing local changes
    - commit only if there are changes
    - push
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return

    with git_lock:
        subprocess.run(["git", "status"], check=False, capture_output=True, text=True)
        subprocess.run(["git", "fetch", "origin", "main"], check=False, capture_output=True, text=True)
        subprocess.run(
            ["git", "pull", "--no-rebase", "origin", "main"],
            check=False,
            capture_output=True,
            text=True
        )
        subprocess.run(["git", "add", LOG_FILE], check=False, capture_output=True, text=True)
        
        subprocess.run(
            ["git", "commit", "-m", "log update"],
            check=False,
            capture_output=True,
            text=True
        )

        push_res = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            check=False,
            capture_output=True,
            text=True
        )

        if push_res.returncode != 0:
            print("[Git Push Error]", push_res.stderr)


def push_worker():
    time.sleep(2)
    while not push_thread_stop.is_set():
        push_queue_event.wait(timeout=5)
        if push_thread_stop.is_set():
            break
        push_queue_event.clear()
        time.sleep(2)
        try:
            ensure_log_file_exists()
            if os.path.getsize(LOG_FILE) > 0:
                git_sync_and_push()
        except Exception as e:
            print("[Push Worker Exception]", e)


# ---------------- Model Prompting ----------------
BASE_SYSTEM_PROMPT = (
    "You are a careful data analyst. The user's LAST message asks a data-analysis question. "
    "Work out the real answer (use any public data you know, general world knowledge, "
    "and arithmetic on numbers given in the message), shaped exactly as the question specifies.\n"
    "Reply with ONLY a JSON object with exactly two top-level keys:\n"
    "\"answer\" (containing your answer in the exact shape requested) and \"log_url\" "
    "(leave this as the string \"PLACEHOLDER\").\n"
    "No explanation, no markdown code blocks, no backticks — just the raw JSON."
)

JSON_RETRY_PROMPT = (
    "Your previous output failed JSON parsing because it included markdown fences or invalid syntax. "
    "Return ONLY valid raw JSON with exactly two top-level keys: "
    "\"answer\" and \"log_url\". "
    "Set \"log_url\" to \"PLACEHOLDER\". "
    "Do NOT wrap the JSON in markdown code blocks like ```json ... ```. No other text."
)


def extract_json_object(text: str):
    """
    Best-effort extraction of a JSON object from text, stripping markdown if present.
    """
    if not text:
        return None
    
    cleaned = text.strip()
    # Strip markdown code blocks if the model included them anyway
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Remove first line (e.g. ```json) and last line (```)
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1]).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = cleaned[start:end + 1].strip()
    return candidate


def safe_parse_reply(reply_text: str):
    """
    Parse model reply into dict; return dict or None.
    """
    try:
        return json.loads(reply_text)
    except json.JSONDecodeError:
        candidate = extract_json_object(reply_text)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None
        return None


# ---------------- Telegram Handler ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text

    log_event({"type": "incoming", "chat_id": chat_id, "text": user_text})

    history = conversation_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history_tail = history[-6:]

    if DRY_RUN:
        reply_text = '{"answer": {"state": "Assam"}, "log_url": "PLACEHOLDER"}'
    else:
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": BASE_SYSTEM_PROMPT}] + history_tail,
            )
            reply_text = response.choices[0].message.content.strip()
        except Exception as e:
            log_event({"type": "error", "chat_id": chat_id, "error": str(e)})
            await update.message.reply_text(json.dumps({"error": "internal_error", "log_url": LOG_URL}))
            return

        parsed = safe_parse_reply(reply_text)
        needs_retry = (
            parsed is None
            or not isinstance(parsed, dict)
            or "answer" not in parsed
            or "log_url" not in parsed
        )

        if needs_retry:
            log_event({
                "type": "json_parse_failed",
                "chat_id": chat_id,
                "raw_model_reply": reply_text
            })

            try:
                retry_response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": BASE_SYSTEM_PROMPT},
                        {"role": "user", "content": history_tail[-1]["content"]},
                        {"role": "system", "content": JSON_RETRY_PROMPT},
                    ],
                )
                reply_text = retry_response.choices[0].message.content.strip()
                parsed = safe_parse_reply(reply_text)
            except Exception as e:
                log_event({"type": "retry_error", "chat_id": chat_id, "error": str(e)})

        if parsed is None:
            parsed = {"answer": None, "log_url": "PLACEHOLDER"}

    final_parsed = {
        "answer": parsed.get("answer"),
        "log_url": LOG_URL,
    }

    final_reply = json.dumps(final_parsed)

    log_event({"type": "outgoing", "chat_id": chat_id, "text": final_reply})
    push_queue_event.set()

    await update.message.reply_text(final_reply)


# ---------------- Application Entrypoint ----------------
if __name__ == "__main__":
    configure_git()
    ensure_log_file_exists()
    port = int(os.environ.get("PORT", 10000))
    
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=push_worker, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    flask_app.run(host="0.0.0.0", port=port)
    print("Bot is running... (Ctrl+C to stop)")
    app.run_polling(drop_pending_updates=True)