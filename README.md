## Docker Auto Update & Telegram Notifier
 - This repository provides an automated Docker image update workflow using:
 - A Python script that checks for new image versions, recreates containers, and sends notifications.
 - A GitHub Actions CI pipeline that builds and pushes the Docker image.
 - Optional Telegram alerts for operational visibility.

 ---

 ## Features

 - Monitors running Docker containers and checks for newer image versions.
 - Automatically pulls updates and restarts containers safely.
 - Sends real-time Telegram notifications (start, update, error).
 - Log rotation included for long-running environments.
 - Configurable check interval via environment variables (if not, default is 3600 seconds).
 - Deployable as a Docker container or standalone Python script.

 ---
## Requirements
**Local Usage**
 - Docker Engine ≥ 20.x
 - Python ≥ 3.10 (if not using Docker)

**CI/CD (optional)**

 - GitHub repository with Actions enabled
 - Docker Hub / GHCR account
 ---
 ## Environment Variables

 ```bash
| Variable             | Description                              | Required | Default                     |
| -------------------- | ---------------------------------------- | -------- | --------------------------- |
| `TELEGRAM`           | Choose true or false                     | No       | false                       |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token                       | Yes      | —                           |
| `TELEGRAM_CHAT_ID`   | Telegram chat ID                         | Yes      | —                           |
| `CHECK_INTERVAL`     | Interval in seconds between image checks | No       | `3600`                      |
| `SKIP_CONTAINERS`    | Choose containers not to updated         | No       | —                           |
| `LOG_PATH`           | Path to rotating log file                | No       | `/var/log/Auto-Update.log`  |
```
---
## Running the App via Docker
1. Pull the image
```bash
docker pull funmicra/docker-update
```
2. Run the container
```bash
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e TELEGRAM=true \
  -e TELEGRAM_BOT_TOKEN=your_bot_token \
  -e TELEGRAM_CHAT_ID=your_chat_id \
  --name Docker-Update \
  funmicra/docker-update
```
---
## Running Without Docker
Install dependencies:
```bash
pip install -r requirements.txt
```
Run the script:
```bash
python3 Docker-Update.py
```
---
## GitHub Actions CI/CD

This repository includes a GitHub Actions pipeline that:

1. Builds the Docker image.
2. Tags it based on the commit SHA.
3. Pushes it to Docker Hub or GHCR.
4. Triggers a Telegram notification on successful push.

**Secrets Required**
```bash
| Secret               | Purpose                    |
| -------------------- | -------------------------- |
| `DOCKERHUB_USERNAME` | Docker Hub / GHCR username |
| `DOCKERHUB_PASSWORD` | Docker Hub / GHCR token    |
| `TELEGRAM_BOT_TOKEN` | Bot token                  |
| `TELEGRAM_CHAT_ID`   | Telegram chat ID           |
```
---
## Logs

Logs are rotated using RotatingFileHandler.
Log directory default: /app/logs/

To mount logs externally:
```bash
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/docker-update-logs:/app/logs \
  funmicra/docker-update
```
## Folder Structure
```bash
/
├── Docker-Update.py
├── Dockerfile
├── requirements.txt
├── docker-compose.yaml
├── .env.example
└── .github/
    └── workflows/
        └── ci.yml
```
---
