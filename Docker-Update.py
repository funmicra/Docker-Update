#!/usr/bin/env python3
import time
import logging
import os
import subprocess
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime
import argparse

# Attempt to import dotenv, ignore if not installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    print("python-dotenv not found, skipping .env loading")

import docker

# =========================
# Config & args
# =========================
HOSTNAME = os.getenv("HOST_MACHINE", "unknown-host")

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true", help="Run in simulation mode (no updates applied)")
parser.add_argument("--run-once", action="store_true", help="Run a single update cycle and exit")
args = parser.parse_args()

DRY_RUN = args.dry_run
RUN_ONCE = args.run_once

def to_bool(value):
    return str(value).lower() in ("1", "true", "yes", "y", "on")

CFG = {
    "check_interval": int(os.getenv("CHECK_INTERVAL", 3600)),
    "skip_containers": [c.strip() for c in os.getenv("SKIP_CONTAINERS", "").split(",") if c.strip()],
    "notifications": {
        "enabled": to_bool(os.getenv("TELEGRAM", "false")),
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID")
    },
    "logging": {
        "path": os.getenv("LOG_PATH") or "Docker-Update.log",
        "max_bytes": 5_000_000,
        "backup_count": 5
    }
}

# =========================
# Logging
# =========================
logger = logging.getLogger("DockerUpdater")
logger.setLevel(logging.INFO)

console = logging.StreamHandler()
console.setLevel(logging.INFO)

file_handler = RotatingFileHandler(CFG["logging"]["path"], maxBytes=CFG["logging"]["max_bytes"], backupCount=CFG["logging"]["backup_count"])
file_handler.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(hostname)s] - %(message)s')
console.setFormatter(formatter)
file_handler.setFormatter(formatter)

class HostnameFilter(logging.Filter):
    def filter(self, record):
        record.hostname = HOSTNAME
        return True

logger.addFilter(HostnameFilter())
logger.addHandler(console)
logger.addHandler(file_handler)

# =========================
# Docker client
# =========================
client = docker.from_env()

# =========================
# Notifications
# =========================
def notify(container_name=None, event_type="info", image=None, extra=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host_info = f"\n🏠 Host: `{HOSTNAME}`"
    if event_type == "update":
        msg = f"{host_info}\n🟢 *Update*\n🐳 Container: `{container_name}`\nNew Image: `{image}`\n🕒 Time: {ts}"
    elif event_type == "up_to_date":
        msg = f"{host_info}\n✅ *Up to date*\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"
    elif event_type == "error":
        msg = f"{host_info}\n⚠️ *Error*\n🐳 Container: `{container_name}`\nDetails: `{extra}`\n🕒 Time: {ts}"
    elif event_type == "cleanup":
        msg = f"{host_info}\n🧹 *Cleanup*\nReclaimed space: `{extra:.2f} MB`\n🕒 Time: {ts}"
    elif event_type == "dry_run":
        msg = f"{host_info}\n🧪 *DRY RUN*\n🔍 No changes applied\n🕒 Time: {ts}"
    else:
        msg = f"{host_info}\nℹ️ Notification\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"

    logger.info(msg)
    if CFG["notifications"]["enabled"] and CFG["notifications"]["bot_token"]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{CFG['notifications']['bot_token']}/sendMessage",
                data={
                    "chat_id": CFG["notifications"]["chat_id"],
                    "text": msg,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        except Exception as e:
            logger.warning(f"[Telegram] Failed: {e}")

# =========================
# Update container
# =========================
last_check_time = {}

def update_container(container):
    global last_check_time
    name = container.name

    # Rate limit
    now = time.time()
    if name in last_check_time and now - last_check_time[name] < CFG["check_interval"]:
        return
    last_check_time[name] = now

    if name in CFG["skip_containers"]:
        logger.info(f"Skipping {name} (skip list)")
        return

    image_name = container.image.tags[0] if container.image.tags else None
    if not image_name:
        logger.warning(f"Container {name} has no tagged image, skipping")
        return

    try:
        logger.info(f"Checking {name} ({image_name})...")
        if DRY_RUN:
            notify(event_type="dry_run")
            return

        # Pull latest image
        new_image = client.images.pull(image_name)
        # Re-tag to make sure it has the proper tag
        repo, tag = image_name.split(":")
        new_image.tag(repo, tag)

        # If already running latest, skip
        if container.image.id == new_image.id:
            notify(name, "up_to_date")
            return

        # Stop/remove container
        container.stop()
        container.remove()

        # Recreate container
        ports = {k: int(v[0]['HostPort']) for k, v in (container.attrs['HostConfig'].get('PortBindings') or {}).items()}
        env = container.attrs['Config'].get('Env')
        mounts = container.attrs.get('Mounts', [])
        volumes = {m['Destination']: {'bind': m['Destination'], 'mode': m.get('Mode', 'rw')} for m in mounts if "Destination" in m}
        restart_policy = container.attrs['HostConfig'].get('RestartPolicy')
        network = container.attrs['HostConfig'].get('NetworkMode')

        client.containers.run(
            image_name,
            name=name,
            detach=True,
            ports=ports or None,
            environment=env,
            volumes=volumes,
            restart_policy=restart_policy,
            network=network
        )

        notify(name, "update", image_name)
    except Exception as e:
        notify(name, "error", extra=str(e))

# =========================
# Cleanup dangling images
# =========================
def cleanup_unused_images():
    try:
        result = client.images.prune(filters={"dangling": True})
        reclaimed = result.get("SpaceReclaimed", 0)
        if reclaimed:
            notify("Docker Images", "cleanup", extra=reclaimed/(1024*1024))
    except Exception as e:
        notify("Docker Images", "error", extra=str(e))

# =========================
# Main loop
# =========================
def main():
    containers = client.containers.list()
    for c in containers:
        update_container(c)

    cleanup_unused_images()

    if RUN_ONCE:
        logger.info("Run-once mode, exiting")
        return

    while True:
        time.sleep(CFG["check_interval"])
        containers = client.containers.list()
        for c in containers:
            update_container(c)
        cleanup_unused_images()

if __name__ == "__main__":
    main()
