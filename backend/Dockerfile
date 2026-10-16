# Backend + phone app in one image. Build from the repository root:
#   docker compose up --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# ffmpeg turns spoken replies into Telegram voice notes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml backend/alembic.ini backend/
COPY backend/app backend/app
ARG EXTRAS=voice
RUN pip install "./backend[${EXTRAS}]"
COPY config config
COPY mobile-pwa/index.html mobile-pwa/app.js mobile-pwa/sw.js mobile-pwa/styles.css \
     mobile-pwa/manifest.webmanifest mobile-pwa/
COPY mobile-pwa/icons mobile-pwa/icons

RUN useradd --create-home --uid 10001 assistant && mkdir -p /data && chown assistant /data
USER assistant
WORKDIR /app/backend
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers"]
