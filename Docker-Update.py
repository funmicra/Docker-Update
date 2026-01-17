#!/usr/bin/env python3

import time
import logging
import os
import subprocess
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime
from dotenv import load_dotenv
import argparse
from pathlib import Path

# =========================
# Environment setup
# =========================
load_dotenv()
HOSTNAME = os.getenv("HOST_MACHINE", "unknown-host")
STACKS_BASE_DIR = Path(os.getenv("STACKS_BASE_DIR", "/opt/infra/stacks"))

# =========================
# CLI arguments
# =========================
parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--run-once", action="store_true")
args = parser.parse_args()

DRY_RUN = args.dry_run
RUN_ONCE = args.run_once

# =========================
# Configuration
# =========================
def to_bool(v):
    return str(v).lower() in ("1", "true", "yes", "on")

CFG = {
    "check_interval": int(os.getenv("CHECK_INTERVAL", 3600)),
    "allowlist": [s.strip() for s in os.getenv("STACKS_ALLOWLIST", "").split(",") if s.strip()],
    "denylist": [s.strip() for s in os.getenv("STACKS_DENYLIST", "").split(",") if s.strip()],
    "notifications": {
        "enabled": to_bool(os.getenv("TELEGRAM", "false")),
        "token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    },
}

# =========================
# Logging
# =========================
LOG_DIR = "/app/logs" if os.path.exists("/app") else "./logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("ComposeAutoUpdate")
logger.setLevel(logging.INFO)

fmt = logging.Formatter(
    "%(asctime)s - %(levelname)s - [%(hostname)s] - %(message)s"
)

class HostFilter(logging.Filter):
    def filter(self, record):
        record.hostname = HOSTNAME
        return True

logger.addFilter(HostFilter())

console = logging.StreamHandler()
console.setFormatter(fmt)
logger.addHandler(console)

file_handler = RotatingFileHandler(
    f"{LOG_DIR}/Docker-Compose-Update.log", maxBytes=5_000_000, backupCount=5
)
file_handler.setFormatter(fmt)
logger.addHandler(file_handler)

# =========================
# Telegram
# =========================
def notify(stack=None, event="info", extra=None):
    if not CFG["notifications"]["enabled"]:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"🏠 Host: `{HOSTNAME}`\n"

    if event == "dry_run":
        msg += "🧪 *DRY RUN MODE*\n"
    elif event == "update":
        msg += f"🟢 *Updated*\n📦 Stack: `{stack}`\n"
    elif event == "error":
        msg += f"⚠️ *Error*\n📦 Stack: `{stack}`\n`{extra}`\n"
    elif event == "cleanup":
        msg += f"🧹 *Cleanup*\nReclaimed `{extra:.2f} MB`\n"

    msg += f"\n🕒 {ts}"

    try:
        requests.post(
            f"https://api.telegram.org/bot{CFG['notifications']['token']}/sendMessage",
            data={
                "chat_id": CFG["notifications"]["chat_id"],
                "text": msg,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram failed: {e}")

# =========================
# Stack discovery
# =========================
def discover_stacks():
    stacks = []
    for d in STACKS_BASE_DIR.iterdir():
        if d.is_dir() and (d / "docker-compose.yaml").exists():
            stacks.append(d)
    return stacks

def stack_allowed(name):
    if CFG["allowlist"] and name not in CFG["allowlist"]:
        return False
    if name in CFG["denylist"]:
        return False
    return True

# =========================
# Stack update logic
# =========================
def update_stack(stack_path: Path):
    name = stack_path.name
    logger.info(f"📦 Processing stack: {name}")

    if DRY_RUN:
        logger.info(f"[DRY-RUN] docker compose pull ({name})")
        logger.info(f"[DRY-RUN] docker compose up -d ({name})")
        return

    subprocess.run(
        ["docker", "compose", "pull"],
        cwd=stack_path,
        check=True,
    )

    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=stack_path,
        check=True,
    )

    notify(name, "update")

# =========================
# Cleanup
# =========================
def cleanup_images():
    result = subprocess.run(
        ["docker", "image", "prune", "-f"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info("🧹 Image prune complete")

# =========================
# Main loop
# =========================
def main():
    if DRY_RUN:
        notify(event="dry_run")

    while True:
        for stack in discover_stacks():
            name = stack.name
            if not stack_allowed(name):
                logger.info(f"Skipping stack {name}")
                continue

            try:
                update_stack(stack)
            except Exception as e:
                logger.error(f"{name} failed: {e}")
                notify(name, "error", str(e))

        cleanup_images()

        if RUN_ONCE:
            logger.info("Run-once mode enabled. Exiting.")
            return

        logger.info(f"💤 Sleeping {CFG['check_interval']} seconds")
        time.sleep(CFG["check_interval"])

if __name__ == "__main__":
    main()
