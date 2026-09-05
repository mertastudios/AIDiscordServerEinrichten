# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
#  AIDiscordServerEinrichten — Production Image für Render (Docker Runtime)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# Systempakete (curl wird für den Render-Healthcheck im Container gebraucht)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Dependencies zuerst (besserer Layer-Cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode
COPY bot/ ./bot/
COPY README.md ./

# Laufzeit-Datenverzeichnis (Sessions). Auf Render Free flüchtig – /connect neu ausführen.
RUN mkdir -p /app/data && useradd --create-home --uid 10001 botuser \
 && chown -R botuser:botuser /app
USER botuser

# Render setzt $PORT automatisch; 8080 ist der Default für lokales Testen.
ENV PORT=8080 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# tini = sauberer PID 1 (Signal-Handling → graceful shutdown bei Render-Deploys)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot"]
