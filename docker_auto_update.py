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

load_dotenv()

# =========================
# Configuration
# =========================
CFG = {
    "check_interval": os.getenv("CHECK_INTERVAL", 3600),  # seconds
    "skip_containers": [],
    "notifications": {
        "enabled": os.getenv("NOTIFY_ENABLED", "True") == "True",
        "type": "telegram",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID")
    },
    "logging": {
        "path": "logs/auto_update.log",
        "max_bytes": 10485760,
        "backup_count": 5
    }
}

# =========================
# Logging setup
# =========================
import logging
from logging.handlers import RotatingFileHandler
import os

LOG_PATH = "/app/logs/docker-auto-update.log"
os.makedirs("/app/logs", exist_ok=True)

logger = logging.getLogger("AutoUpdate")
logger.setLevel(logging.INFO)

# Console output
console = logging.StreamHandler()
console.setLevel(logging.INFO)

# File rotation: 5 MB × 5 backups
file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=5_000_000,
    backupCount=5
)
file_handler.setLevel(logging.INFO)

# Formatter
fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console.setFormatter(fmt)
file_handler.setFormatter(fmt)

# Register handlers
logger.addHandler(console)
logger.addHandler(file_handler)


# =========================
# Docker client
# =========================
client = docker.from_env()

# =========================
# Telegram notification
# =========================
def format_telegram_message(event_type, container_name, image=None, extra=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if event_type == "update":
        return f"🟢 *Update*\n🐳 Container: `{container_name}`\nNew Image: `{image}`\n🕒 Time: {ts}"
    elif event_type == "up_to_date":
        return f"✅ *No Update Needed*\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"
    elif event_type == "error":
        return f"⚠️ *Error*\n🐳 Container: `{container_name}`\nDetails: `{extra}`\n🕒 Time: {ts}"
    elif event_type == "cleanup":
        return f"🧹 *Cleanup*\nReclaimed space: `{extra:.2f} MB`\n🕒 Time: {ts}"
    else:
        return f"ℹ️ *Notification*\n🐳 Container: `{container_name}`\n🕒 Time: {ts}"

def notify(container_name, event_type="info", image=None, extra=None):
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
def update_container(container):
    name = container.name
    if name in CFG["skip_containers"]:
        logger.info(f"Skipping container {name}")
        return

    labels = container.attrs['Config'].get('Labels', {})
    stack_name = labels.get('com.docker.stack.namespace')
    compose_project = labels.get('com.docker.compose.project')
    compose_service = labels.get('com.docker.compose.service')
    image_name = container.image.tags[0] if container.image.tags else None

    if not image_name:
        logger.warning(f"Container {name} has no tagged image. Skipping.")
        return

    try:
        logger.info(f"Checking {name} ({image_name})...")
        new_image = client.images.pull(image_name)

        if new_image.id != container.image.id:
            # ================= STACK =================
            if stack_name:
                service_name = f"{stack_name}_{name}"
                logger.info(f"{name} is part of Swarm stack '{stack_name}'. Updating service...")
                notify(name, "update", image_name)
                cmd = ["docker", "service", "update", "--image", image_name, service_name]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    logger.info(f"Stack service {service_name} updated successfully.")
                    notify(service_name, "update", image_name)
                else:
                    logger.error(f"Service update failed: {result.stderr}")
                    notify(service_name, "error", extra=result.stderr)
                return

            # ================= COMPOSE =================
            elif compose_project and compose_service:
                logger.info(f"{name} is part of docker-compose project '{compose_project}' service '{compose_service}'. Updating...")
                notify(name, "update", image_name)

                # Pull latest image
                cmd_pull = ["docker-compose", "-p", compose_project, "pull", compose_service]
                result_pull = subprocess.run(cmd_pull, capture_output=True, text=True)
                if result_pull.returncode != 0:
                    logger.error(f"docker-compose pull failed: {result_pull.stderr}")
                    notify(name, "error", extra=result_pull.stderr)
                    return

                # Restart service
                cmd_up = ["docker-compose", "-p", compose_project, "up", "-d", "--no-deps", compose_service]
                result_up = subprocess.run(cmd_up, capture_output=True, text=True)
                if result_up.returncode == 0:
                    logger.info(f"docker-compose service '{compose_service}' updated successfully.")
                    notify(name, "update", image_name)
                else:
                    logger.error(f"docker-compose up failed: {result_up.stderr}")
                    notify(name, "error", extra=result_up.stderr)
                return

            # ================= STANDALONE =================
            else:
                notify(name, "update", image_name)
                ports = container.attrs['HostConfig']['PortBindings']
                env = container.attrs['Config']['Env']
                volumes = {m['Destination']: {'bind': m['Destination'], 'mode': m.get('Mode', 'rw')}
                           for m in container.attrs.get('Mounts', []) if "Destination" in m}
                restart_policy = container.attrs['HostConfig']['RestartPolicy']
                network = container.attrs['HostConfig']['NetworkMode']

                container.stop()
                container.remove()
                client.containers.run(
                    image_name,
                    name=name,
                    detach=True,
                    ports={k: int(v[0]['HostPort']) for k, v in ports.items()} if ports else None,
                    environment=env,
                    volumes=volumes,
                    restart_policy=restart_policy,
                    network=network
                )
                logger.info(f"{name} updated successfully!")
                notify(name, "update", image_name)
        else:
            logger.info(f"{name} is up to date.")
            notify(name, "up_to_date")

    except Exception as e:
        logger.error(f"Error updating {name}: {e}")
        notify(name, "error", extra=str(e))

# =========================
# Cleanup unused images
# =========================
def cleanup_unused_images():
    try:
        logger.info("🧹 Pruning unused images…")
        unused = client.images.prune(filters={"dangling": False})
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
            containers = client.containers.list()
            for c in containers:
                update_container(c)

            cleanup_unused_images()
            logger.info(f"💤 Sleeping {CFG['check_interval']} seconds…")
            time.sleep(CFG["check_interval"])
    except KeyboardInterrupt:
        logger.info("Exiting Docker auto-update script.")

if __name__ == "__main__":
    main()
