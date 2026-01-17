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
import shutil


load_dotenv()

HOSTNAME = os.getenv("HOST_MACHINE", "unknown-host")

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

def resolve_image_id(repo, tag):
    """
    Resolve the image ID that repo:tag currently points to.
    """
    image = client.images.get(f"{repo}:{tag}")
    return image.id

def get_compose_cmd():
    """
    Detect whether docker-compose (v1) or docker compose (v2) is available.
    Returns the command as a list.
    """
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


# =========================
# Update container function
# =========================
last_check_time = {}

def update_container(container):
    global last_check_time
    name = container.name
    
    # Rate limiting per container
    now = time.time()
    if name in last_check_time and now - last_check_time[name] < CFG["check_interval"]:
        return
    last_check_time[name] = now

    # Skip-list logic
    if name in CFG["skip_containers"]:
        logger.info(f"Skipping container {name} (in skip list)")
        return

    # Labels for Swarm/Compose detection
    labels = container.attrs['Config'].get('Labels', {})
    stack_name = labels.get('com.docker.stack.namespace')
    compose_project = labels.get('com.docker.compose.project')
    compose_service = labels.get('com.docker.compose.service')

    # Determine image
    image_name = container.image.tags[0] if container.image.tags else None
    if not image_name:
        logger.warning(f"Container {name} has no tagged image. Skipping.")
        return

    # Determine repo and tag
    if ":" in image_name:
        repo, tag = image_name.rsplit(":", 1)
    else:
        repo, tag = image_name, "latest"

    auto_update = tag.lower() == "latest" or tag.isdigit()

    try:
        logger.info(f"Checking {name} ({image_name})...")

        if DRY_RUN:
            logger.info(f"[DRY-RUN] Would pull latest image for {name}")
            new_image = container.image
        else:
            # Pull latest if applicable
            if auto_update:
                logger.info(f"Pulling latest image for {name}...")
                client.images.pull(repo, tag=tag)
                new_image = client.images.get(f"{repo}:{tag}")
            else:
                logger.info(f"Skipping pull for {name} (custom tag: {tag})")
                new_image = container.image

        # Up-to-date check
        if auto_update:
            try:
                latest_id = resolve_image_id(repo, tag)
            except docker.errors.ImageNotFound:
                logger.error(f"Unable to resolve {repo}:{tag}")
                notify(name, "error", extra=f"Cannot resolve {repo}:{tag}")
                return

            current_id = container.image.id
            latest_id = client.images.get(f"{repo}:{tag}").id

            if current_id == latest_id:
                logger.info(f"{name} is already running the latest image.")
                notify(name, "up_to_date")
                return

            logger.info(f"🆕 Image drift detected for {name}")
            logger.info(f"{name}: current={current_id[:12]} latest={latest_id[:12]}")
        else:
            logger.info(f"{name} uses pinned tag '{tag}', skipping update.")
            return

        logger.info(f"🆕 Update needed for {name}")
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

            compose_cmd = get_compose_cmd()

            if DRY_RUN:
                logger.info(
                    f"[DRY-RUN] Would run: {' '.join(compose_cmd)} "
                    f"-p {compose_project} pull {compose_service}"
                )
                logger.info(
                    f"[DRY-RUN] Would run: {' '.join(compose_cmd)} "
                    f"-p {compose_project} up -d --no-deps {compose_service}"
                )
                return

            # Pull
            cmd_pull = compose_cmd + ["-p", compose_project, "pull", compose_service]
            result_pull = subprocess.run(cmd_pull, capture_output=True, text=True)

            if result_pull.returncode != 0:
                logger.error(f"Compose pull failed: {result_pull.stderr.strip()}")
                notify(name, "error", extra=result_pull.stderr.strip())
                return

            # Update
            cmd_up = compose_cmd + [
                "-p", compose_project,
                "up", "-d", "--no-deps", compose_service
            ]
            result_up = subprocess.run(cmd_up, capture_output=True, text=True)

            if result_up.returncode == 0:
                logger.info(
                    f"Compose service '{compose_service}' updated successfully."
                )
                notify(name, "update", image_name)
            else:
                logger.error(f"Compose up failed: {result_up.stderr.strip()}")
                notify(name, "error", extra=result_up.stderr.strip())

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

        # Stop, remove, recreate container
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