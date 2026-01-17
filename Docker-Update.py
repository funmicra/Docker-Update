#!/usr/bin/env python3
import docker
import time
import logging
import os
import subprocess
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime
from dotenv import load_dotenv
import argparse
import json
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
parser.add_argument("--dry-run", action="store_true", help="Run in simulation mode (no updates applied)")
parser.add_argument("--run-once", action="store_true", help="Run a single update cycle and exit")
args = parser.parse_args()
DRY_RUN = args.dry_run
RUN_ONCE = args.run_once

# =========================
# Configuration
# =========================
def to_bool(value):
    return str(value).lower() in ("1", "true", "yes", "y", "on")

CFG = {
    "check_interval": int(os.getenv("CHECK_INTERVAL") or 86400),
    "skip_containers": [c.strip() for c in os.getenv("SKIP_CONTAINERS", "").split(",") if c.strip()],
    "notifications": {
        "enabled": to_bool(os.getenv("TELEGRAM", "false")),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID")
    },
    "logging": {
        "path": os.getenv("LOG_PATH") or "/var/log/Docker-Update.log",
        "max_bytes": 10_485_760,
        "backup_count": 5
    }
}

# =========================
# Logging setup
# =========================
LOG_DIR = "/app/logs" if os.path.exists("/app") else os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "Docker-Update.log")

logger = logging.getLogger("AutoUpdate")
logger.setLevel(logging.INFO)

class HostnameFilter(logging.Filter):
    def filter(self, record):
        record.hostname = HOSTNAME
        return True

logger.addFilter(HostnameFilter())

console = logging.StreamHandler()
console.setLevel(logging.INFO)
file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5)
file_handler.setLevel(logging.INFO)

fmt = logging.Formatter('%(asctime)s - %(levelname)s - [%(hostname)s] - %(message)s')
console.setFormatter(fmt)
file_handler.setFormatter(fmt)
logger.addHandler(console)
logger.addHandler(file_handler)

# =========================
# Docker client
# =========================
client = docker.from_env()

# =========================
# Telegram notification
# =========================
def format_telegram_message(event_type, container_name=None, image=None, extra=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host_info = f"\n🏠 Host: `{HOSTNAME}`"

    if event_type == "dry_run":
        return f"{host_info}\n🧪 *DRY RUN MODE*\n🔍 No changes will be applied.\n🕒 Time: {ts}"
    if event_type == "update":
        return f"{host_info}\n🟢 *Update*\n🐳 Container: `{container_name}`\nNew Image: `{image}`\n🕒 Time: {ts}"
    if event_type == "up_to_date":
        return f"{host_info}\n✅ *No Update Needed*\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"
    if event_type == "error":
        return f"{host_info}\n⚠️ *Error*\n🐳 Container: `{container_name}`\nDetails: `{extra}`\n🕒 Time: {ts}"
    if event_type == "cleanup":
        return f"{host_info}\n🧹 *Cleanup*\nReclaimed space: `{extra:.2f} MB`\n🕒 Time: {ts}"
    if event_type == "info":
        return f"{host_info}\nℹ️ *Info*\n{extra}\n🕒 Time: {ts}"

    return f"{host_info}\nℹ️ *Notification*\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"

def notify(container_name=None, event_type="info", image=None, extra=None):
    msg = format_telegram_message(event_type, container_name, image, extra)
    logger.info(msg)
    if CFG["notifications"]["enabled"] and CFG["notifications"]["telegram_bot_token"]:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{CFG['notifications']['telegram_bot_token']}/sendMessage",
                data={
                    "chat_id": CFG["notifications"]["telegram_chat_id"],
                    "text": msg,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            if resp.status_code != 200:
                logger.warning(f"[Telegram] Failed to send: {resp.text}")
        except Exception as e:
            logger.warning(f"[Telegram] Exception: {e}")

# =========================
# Discover stacks
# =========================
def discover_stacks():
    if not STACKS_BASE_DIR.exists():
        logger.error(f"Stacks base directory does not exist: {STACKS_BASE_DIR}")
        return []

    stacks = []
    for d in STACKS_BASE_DIR.iterdir():
        if d.is_dir() and (d / "docker-compose.yaml").exists():
            stacks.append(d)
    return stacks

# =========================
# Update stack function
# =========================
def update_stack(stack_dir: Path):
    stack_name = stack_dir.name
    logger.info(f"Checking stack: {stack_name}")

    if DRY_RUN:
        logger.info(f"[DRY-RUN] Would pull and update stack {stack_name}")
        notify(stack_name, "dry_run")
        return

    try:
        # Pull images
        pull_cmd = ["docker", "compose", "pull"]
        result = subprocess.run(pull_cmd, cwd=stack_dir, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        logger.info(result.stdout)

        # Up containers
        up_cmd = ["docker", "compose", "up", "-d", "--no-deps"]
        result = subprocess.run(up_cmd, cwd=stack_dir, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        logger.info(result.stdout)

        notify(stack_name, "update", extra=f"Stack updated successfully: {stack_name}")

    except Exception as e:
        logger.error(f"Error updating stack {stack_name}: {e}")
        notify(stack_name, "error", extra=str(e))

# =========================
# Cleanup unused images
# =========================
def cleanup_unused_images():
    try:
        logger.info("🧹 Pruning unused images…")
        unused = client.images.prune(filters={"dangling": True})
        reclaimed = unused.get("SpaceReclaimed", 0)
        if reclaimed > 0:
            size_mb = reclaimed / (1024 * 1024)
            logger.info(f"Reclaimed {size_mb:.2f} MB from unused images.")
            notify("Docker Images", "cleanup", extra=size_mb)
    except Exception as e:
        logger.error(f"Failed pruning images: {e}")
        notify("Docker Images", "error", extra=str(e))

# =========================
# Main loop
# =========================
def main():
    try:
        while True:
            stacks = discover_stacks()
            if not stacks:
                logger.warning("No stacks found to update.")
            for stack_dir in stacks:
                update_stack(stack_dir)

            cleanup_unused_images()

            if RUN_ONCE:
                logger.info("Run-once mode: exiting after single cycle.")
                return

            logger.info(f"💤 Sleeping {CFG['check_interval']} seconds…")
            time.sleep(CFG["check_interval"])

    except KeyboardInterrupt:
        logger.info("Exiting Docker auto-update script.")

if __name__ == "__main__":
    main()
