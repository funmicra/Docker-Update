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
# Helper functions
# =========================
def get_image_basename(container):
    tags = container.image.tags
    if not tags:
        raise ValueError("Container image has no tags (dangling <none>:<none>)")
    return tags[0].split(":")[0]

# =========================
# Update container function
# =========================
last_check_time = {}

def update_container(container):
    global last_check_time
    name = container.name
    
    new_image_id = None 
    # Rate limiting per container
    now = time.time()
    if name in last_check_time and now - last_check_time[name] < CFG["check_interval"]:
        return
    last_check_time[name] = now

    # Skip-list logic
    if name in CFG["skip_containers"]:
        logger.info(f"Skipping container {name} (in skip list)")
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

        if DRY_RUN:
            logger.info(f"[DRY-RUN] Would pull latest image for {name}")
            new_image_id = "SIMULATED-ID"
        else:
            # Force pull the image by first removing local copy (if exists)
            try:
                client.images.remove(image=image_name, force=True)
            except docker.errors.ImageNotFound:
                pass

            new_image = client.images.pull(image_name)
            repo, tag = image_name.split(":")
            new_image.tag(repo, tag)
            # new_image_id = new_image.id

        # Up-to-date check
        if not DRY_RUN and new_image_id == container.image.id:
            logger.info(f"{name} is up to date.")
            notify(name, "up_to_date")
            return

        logger.info(f"🆕 Update available for {name}")
        notify(name, "update", image_name)

        # ==================== SWARM ====================
        if stack_name:
            service_name = f"{stack_name}_{name}"
            logger.info(f"{name} is part of Swarm stack '{stack_name}'.")

            if DRY_RUN:
                logger.info(f"[DRY-RUN] Would run: docker service update --image {image_name} {service_name}")
                return

            cmd = ["docker", "service", "update", "--image", image_name, service_name]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                logger.info(f"Stack service {service_name} updated successfully.")
                notify(service_name, "update", image_name)
            else:
                logger.error(f"Service update failed: {result.stderr}")
                notify(service_name, "error", extra=result.stderr)
            return

        # ==================== COMPOSE ====================
        if compose_project and compose_service:
            logger.info(f"{name} is part of docker-compose project '{compose_project}'.")

            if DRY_RUN:
                logger.info(f"[DRY-RUN] Would pull and update {compose_service} in project {compose_project}")
                return

            # Force pull latest image
            cmd_pull = ["docker-compose", "-p", compose_project, "pull", compose_service]
            result_pull = subprocess.run(cmd_pull, capture_output=True, text=True)
            if result_pull.returncode != 0:
                logger.error(f"docker-compose pull failed: {result_pull.stderr}")
                notify(name, "error", extra=result_pull.stderr)
                return

            cmd_up = ["docker-compose", "-p", compose_project, "up", "-d", "--no-deps", compose_service]
            result_up = subprocess.run(cmd_up, capture_output=True, text=True)
            if result_up.returncode == 0:
                logger.info(f"docker-compose service '{compose_service}' updated successfully.")
                notify(name, "update", image_name)
            else:
                logger.error(f"docker-compose up failed: {result_up.stderr}")
                notify(name, "error", extra=result_up.stderr)
            return

        # ==================== STANDALONE ====================
        ports = container.attrs['HostConfig']['PortBindings']
        env = container.attrs['Config']['Env']
        mounts = container.attrs.get('Mounts', [])
        volumes = {
            m['Destination']: {
                'bind': m['Destination'],
                'mode': m.get('Mode', 'rw')
            } for m in mounts if "Destination" in m
        }
        restart_policy = container.attrs['HostConfig']['RestartPolicy']
        network = container.attrs['HostConfig']['NetworkMode']

        if DRY_RUN:
            logger.info(f"[DRY-RUN] Would stop/remove container {name} and recreate with {image_name}")
            return

        # Actual update: stop, remove, recreate with latest image
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

    except Exception as e:
        logger.error(f"Error updating {name}: {e}")
        notify(name, "error", extra=str(e))

def update_container(container):
    global last_check_time
    name = container.name

    # Always predefine the variable to eliminate unbound errors
    new_image_id = None

    # Rate limiting per container
    now = time.time()
    if name in last_check_time and now - last_check_time[name] < CFG["check_interval"]:
        return
    last_check_time[name] = now

    # Skip-list logic
    if name in CFG["skip_containers"]:
        logger.info(f"Skipping container {name} (in skip list)")
        return

    try:
        current_image = container.image.id
    except Exception as e:
        notify(name, "error", extra=f"Unable to retrieve image ID: {e}")
        return

    # Pull latest image
    try:
        pulled = client.images.pull(container.image.tags[0].split(":")[0], tag="latest")
        new_image_id = pulled.id
    except Exception as e:
        notify(name, "error", extra=f"Image pull failed: {e}")
        return

    # No change detected — early exit
    if new_image_id == current_image:
        notify(name, "up_to_date")
        return

    # Dry run mode
    if DRY_RUN:
        notify(name, "dry_run", image=new_image_id)
        return

    # Stop + remove legacy container
    try:
        container.stop()
        container.remove()
    except Exception as e:
        notify(name, "error", extra=f"Container removal failure: {e}")
        return

    # Deploy updated image
    try:
        client.containers.run(
            container.image.tags[0].split(":")[0] + ":latest",
            name=name,
            detach=True,
            restart_policy={"Name": "always"},
            ports=container.attrs.get("HostConfig", {}).get("PortBindings", {}),
            volumes=container.attrs.get("Mounts", {}),
            environment=container.attrs.get("Config", {}).get("Env", [])
        )
    except Exception as e:
        notify(name, "error", extra=f"Container deployment failure: {e}")
        return

    # Success path
    notify(name, "update", image=new_image_id)
    logger.info(f"Container {name} updated successfully → {new_image_id}")

 

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

