FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY docker_auto_update.py .
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p logs

VOLUME ["/var/run/docker.sock"]

CMD ["python", "docker_auto_update.py"]
