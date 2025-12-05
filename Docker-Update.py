#!/usr/bin/env python3
import docker
import time
import logging
import sys
import os
import subprocess
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime
from dotenv import load_dotenv
import argparse
import socket
from docker.errors import NotFound, APIError

load_dotenv()

HOSTNAME = os.getenv("HOST_MACHINE", "unknown-host")

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
    "check_interval": (
    int(os.getenv("CHECK_INTERVAL").strip())
    if os.getenv("CHECK_INTERVAL") and os.getenv("CHECK_INTERVAL").strip().isdigit()
    else 3600
    ),
    "skip_containers": [
        c.strip() for c in os.getenv("SKIP_CONTAINERS", "").split(",") if c.strip()
    ],
    "notifications": {
        "enabled": to_bool(os.getenv("TELEGRAM", "false")),
        "type": "telegram",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID")
    },
    "logging": {
        "path": os.getenv("LOG_PATH") or "/var/log/Docker-Update.log",
        "max_bytes": 10485760,
        "backup_count": 5
    }
}

# =========================
# Logging setup
# =========================
# Smart logging path selection
if os.path.exists("/app"):
    LOG_DIR = "/app/logs"
else:
    LOG_DIR = os.path.join(os.getcwd(), "logs")

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

file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=5_000_000,
    backupCount=5
)
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
        return (
            f"{host_info}\n"
            f"🧪 *DRY RUN MODE*\n"
            f"🔍 No changes will be applied.\n"
            f"🕒 Time: {ts}"
        )

    if event_type == "update":
        return (
            f"{host_info}\n"
            f"🟢 *Update*\n"
            f"🐳 Container: `{container_name}`\n"
            f"New Image: `{image}`\n"
            f"🕒 Time: {ts}"
        )

    if event_type == "up_to_date":
        return (
            f"{host_info}\n"
            f"✅ *No Update Needed*\n"
            f"🐳 Container: `{container_name}`\n"
            f"🕒 Time: {ts}"
        )

    if event_type == "error":
        return (
            f"{host_info}\n"
            f"⚠️ *Error*\n"
            f"🐳 Container: `{container_name}`\n"
            f"Details: `{extra}`\n"
            f"🕒 Time: {ts}"
        )

    if event_type == "cleanup":
        return (
            f"{host_info}\n"
            f"🧹 *Cleanup*\n"
            f"Reclaimed space: `{extra:.2f} MB`\n"
            f"🕒 Time: {ts}"
        )

    return (
        f"{host_info}\n"
        f"ℹ️ *Notification*\n"
        f"🐳 Container: `{container_name}`\n"
        f"🕒 Time: {ts}"
    )
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
# Update container function
# =========================
last_check_time = {}

def update_container(container):
    global last_check_time

    name = container.name
    image_name = container.image.tags[0] if container.image.tags else None
    old_image_id = container.image.id
    new_image_id = None   # <-- critical: guarantee variable exists

    logger.info(f"🔍 Evaluating update window for {name} ({image_name})")

    try:
        if not image_name:
            raise RuntimeError("Container has no tagged image reference.")

        repo, tag = image_name.split(":")

        # --- Pull fresh image -------------------------------------------------
        logger.info(f"📦 Pulling new image for {name}: {image_name}")

        if DRY_RUN:
            new_image_id = "SIMULATED-ID"
            logger.info(f"(DRY_RUN) Simulated new image ID: {new_image_id}")
        else:
            try:
                # preemptive hygiene: remove old tag if orphaned
                try:
                    client.images.remove(image=image_name, force=True)
                except docker.errors.ImageNotFound:
                    pass

                new_image = client.images.pull(image_name)
                new_image_id = new_image.id

                # re-tag to avoid `<none>` images
                new_image.tag(repo, tag)

            except Exception as pull_error:
                raise RuntimeError(
                    f"Image pull failed for {image_name}: {pull_error}"
                )

        # --- Determine if update is even necessary -----------------------------
        if not DRY_RUN and new_image_id == old_image_id:
            logger.info(f"⏩ {name} is already current. No update required.")
            notify(name, "skipped")
            return

        # --- Stop, replace, restart container ---------------------------------
        logger.info(f"🔄 Rolling update for {name}")

        if not DRY_RUN:
            container.stop()
            client.containers.remove(container.id)

            # ensure clean slate for old leftovers
            client.images.prune(filters={"dangling": True})

            # deploy updated container
            client.containers.run(
                image_name,
                name=name,
                detach=True,
                ports=container.attrs["NetworkSettings"]["Ports"],
                environment=container.attrs["Config"]["Env"],
                volumes=container.attrs["Mounts"],
                restart_policy=container.attrs["HostConfig"]["RestartPolicy"],
            )

        logger.info(f"✅ Update complete for {name}")
        notify(name, "updated", extra=f"new image ID: {new_image_id}")

    except Exception as e:
        # consistent error flow: no assumptions about variable existence
        logger.error(f"❌ Update failed for {name}: {e}")
        notify(name, "error", extra=str(e))

    last_check_time = datetime.now()


 

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
        containers = client.containers.list()
        for c in containers:
            update_container(c)

        cleanup_unused_images()


        if RUN_ONCE:
            logger.info("Run-once mode: exiting after single cycle.")
            return
        
        while True:
            logger.info(f"💤 Sleeping {CFG['check_interval']} seconds…")
            time.sleep(CFG["check_interval"])

            containers = client.containers.list()
            for c in containers:
                update_container(c)

            cleanup_unused_images()
    
    except KeyboardInterrupt:
        logger.info("Exiting Docker auto-update script.")

if __name__ == "__main__":
    if DRY_RUN:
        notify(event_type="dry_run")  # global dry-run banner
    main()

