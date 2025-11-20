#!/usr/bin/env python3
import os
import time
import requests
import subprocess
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# =========================
# Configuration
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))
UPDATER_SCRIPT = "/app/docker_auto_update.py"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Telegram bot token or chat ID not set in .env")


# =========================
# Logging
# =========================
os.makedirs("/var/log/docker-updater", exist_ok=True)
logger = logging.getLogger("UpdaterListener")
logger.setLevel(logging.INFO)
fh = logging.FileHandler("/var/log/docker-updater/docker-auto-update.log")
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
logger.addHandler(fh)

# =========================
# Telegram API helpers
# =========================
def escape_md(text: str) -> str:
    """Escape text for MarkdownV2 formatting"""
    escape_chars = r"_*[]()~`>#+-=|{}.!\\"
    for c in escape_chars:
        text = text.replace(c, f"\\{c}")
    return text

def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2"
        })
        if r.status_code != 200:
            logger.warning(f"Failed to send message: {r.text}")
    except Exception as e:
        logger.warning(f"Exception sending message: {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 10}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        logger.warning(f"Failed to fetch updates: {e}")
        return []

# =========================
# Main loop
# =========================
def main():
    last_update_id = None
    logger.info("Updater listener started")
    while True:
        updates = get_updates(offset=last_update_id + 1 if last_update_id else None)
        for u in updates:
            last_update_id = u["update_id"]
            msg = u.get("message", {})
            text = msg.get("text", "").strip()
            if msg.get("chat", {}).get("id") != int(TELEGRAM_CHAT_ID):
                continue  # ignore messages from other chats

            if text.lower() == "/update_now":
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info("Received manual update command")
                send_message(f"⏳ *Manual Update Triggered*\n🕒 Time: `{escape_md(now)}`\nRunning Docker Auto-Updater…")
                try:
                    # Run updater script
                    result = subprocess.run(
                        ["docker", "exec", "Docker-Update", "python3", "/app/docker_auto_update.py"],
                        capture_output=True,
                        text=True,
                        timeout=600  # 10 minutes max
                    )
                    if result.returncode == 0:
                        send_message(f"✅ *Updater Finished Successfully*\n🕒 Time: `{escape_md(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`")
                        logger.info("Updater finished successfully")
                    else:
                        err = escape_md(result.stderr or "Unknown error")
                        send_message(f"⚠️ *Updater Failed*\n🕒 Time: `{escape_md(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`\nDetails:\n`{err}`")
                        logger.error(f"Updater failed: {result.stderr}")
                except Exception as e:
                    err = escape_md(str(e))
                    send_message(f"⚠️ *Exception Running Updater*\nDetails:\n`{err}`")
                    logger.error(f"Exception: {e}")
            else:
                logger.info(f"Ignored message: {text}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
