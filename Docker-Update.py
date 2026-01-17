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
import shutil
from typing import cast
from docker.types import RestartPolicy
import json

# =========================
# Environment setup
# =========================
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
def get_local_digest(repo: str, repo_digests: list[str]) -> str | None:
    for rd in repo_digests:
        if rd.startswith(f"{repo}@"):
            return rd.split("@", 1)[1]
    return None

def resolve_image_id(repo, tag):
    return client.images.get(f"{repo}:{tag}").id

def get_remote_digest(image_ref):
    cmd = ["docker", "manifest", "inspect", image_ref]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    data = json.loads(result.stdout)
    TARGET_ARCH = os.getenv("TARGET_ARCH", "amd64")

    # multi-arch manifest
    if "manifests" in data:
        for m in data["manifests"]:
            if m["platform"]["architecture"] == TARGET_ARCH:
                return m["digest"]

    # single-arch fallback
    if "Descriptor" in data:
        return data["Descriptor"]["digest"]

    raise RuntimeError(f"No digest found for architecture '{TARGET_ARCH}'")

# =========================
# Update container function
# =========================
updates_applied = False

def update_container(container):
    global updates_applied
    name = container.name

    if name in CFG["skip_containers"]:
        logger.info(f"Skipping container {name} (in skip list)")
        return

    container.reload()
    labels = container.attrs["Config"].get("Labels", {})
    stack_name = labels.get("com.docker.stack.namespace")
    compose_project = labels.get("com.docker.compose.project")
    compose_service = labels.get("com.docker.compose.service")

    if not container.image.tags:
        logger.warning(f"Container {name} has no tagged image. Skipping.")
        return

    image_name = container.image.tags[0]
    if ":" in image_name:
        repo, tag = image_name.rsplit(":", 1)
    else:
        repo, tag = image_name, "latest"

    auto_update = tag.lower() == "latest"
    logger.info(f"Checking {name} ({image_name})...")
    if not auto_update:
        logger.info(f"{name} uses pinned tag '{tag}', skipping update.")
        return

    repo_digests = container.image.attrs.get("RepoDigests", [])
    local_digest = get_local_digest(repo, repo_digests)
    if not local_digest:
        logger.warning(f"{name} has RepoDigests but none match repo '{repo}', skipping.")
        return

    try:
        remote_digest = get_remote_digest(image_name)
    except Exception as e:
        logger.error(f"Failed to fetch remote digest for {image_name}: {e}")
        notify(name, "error", extra=str(e))
        return

    if local_digest == remote_digest:
        logger.info(f"{name} already up to date (digest match)")
        notify(name, "up_to_date")
        return

    logger.info(f"🆕 Digest drift detected for {name}")
    logger.info(f"{name}: local={local_digest} remote={remote_digest}")

    if DRY_RUN:
        logger.info(f"[DRY-RUN] Would pull and update {name}")
        notify(name, "would_update", image_name)
        return

    try:
        client.images.pull(repo, tag=tag)
    except Exception as e:
        logger.error(f"Image pull failed for {image_name}: {e}")
        notify(name, "error", extra=str(e))
        return

    try:
        if stack_name:
            service_name = labels.get("com.docker.swarm.service.name")
            if not service_name:
                raise RuntimeError("Unable to resolve Swarm service name")
            logger.info(f"{name} is part of Swarm stack '{stack_name}'")
            subprocess.run(["docker", "service", "update", "--force", service_name], check=True)

        elif compose_project and compose_service:
            logger.info(f"{name} is part of docker-compose project '{compose_project}'")
            subprocess.run(
                ["docker", "compose", "-p", compose_project, "up", "-d", "--no-deps", "--force-recreate", compose_service],
                check=True
            )

        else:
            logger.info(f"{name} is a standalone container")

            # Save old image ID before stopping
            old_image_id = container.image.id

            # Pull updated image and capture object
            try:
                pulled_image = client.images.pull(repo, tag=tag)
            except Exception as e:
                logger.error(f"Failed to pull {repo}:{tag}: {e}")
                notify(name, "error", extra=str(e))
                return

            # Stop & remove container
            container.stop()
            container.remove()

            # Run the new container using explicit tag
            client.containers.run(
                f"{repo}:{tag}",
                name=name,
                detach=True,
                restart_policy=cast(RestartPolicy, {"Name": "unless-stopped"})
            )

            # Remove old image if different
            if old_image_id != pulled_image.id:
                try:
                    client.images.remove(old_image_id)
                    logger.info(f"Removed old image {old_image_id[:12]} for {name}")
                except Exception as e:
                    logger.warning(f"Failed to remove old image {old_image_id[:12]}: {e}")

            # Notify with correct tag
            notify(name, "update", f"{repo}:{tag}")


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
        while True:
            containers = client.containers.list()
            for c in containers:
                update_container(c)

            if updates_applied:
                cleanup_unused_images()
            else:
                logger.info("No updates applied; skipping image prune.")

            if RUN_ONCE:
                logger.info("Run-once mode: exiting after single cycle.")
                return

            logger.info(f"💤 Sleeping {CFG['check_interval']} seconds…")
            time.sleep(CFG["check_interval"])

    except KeyboardInterrupt:
        logger.info("Exiting Docker auto-update script.")

if __name__ == "__main__":
    if DRY_RUN:
        notify(event_type="dry_run")
    main()
