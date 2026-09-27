FROM python:3.12-slim

# ffmpeg нужен команде .вгф (видео → GIF); остальное — из requirements.txt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Секреты — только через окружение/.env (см. .env.example).
# Состояние (сессии, JSON, логи) — в /data через SLIMBOT_DATA_DIR.
ENV SLIMBOT_DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
