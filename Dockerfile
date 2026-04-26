FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for yfinance / lxml / sqlite
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc curl ca-certificates \
        libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy code
COPY backend/ /app/backend/
COPY scraper/ /app/scraper/
COPY app/     /app/app/

# Default DB location (overridable)
ENV DATABASE_PATH=/data/eventalpha.db \
    LOG_DIR=/data/logs

# Sensible defaults — override with env vars or compose
ENV PORT=5000 \
    SCRAPER_INTERVAL=3 \
    SIGNAL_COOLDOWN_HOURS=24 \
    SIGNAL_EXPIRY_DAYS=7 \
    FEED_TIMEOUT_SECS=10 \
    RL_WINDOW_SECS=60 \
    RL_MAX_HITS=120

# Persist DB + logs
VOLUME ["/data"]

EXPOSE 5000

# Initialize the schema on first run, then launch the API.
WORKDIR /app/backend
CMD ["sh", "-c", "python ../scraper/database_schema.py >/dev/null 2>&1 || true; python api.py"]
