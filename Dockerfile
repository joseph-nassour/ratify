# Ratify — Foxit "Your Agent Shouldn't Sign That"
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DRY_RUN=true
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The agent is spawned as a subprocess of this process with a scrubbed environment
# (app/supervisor.py). It is deliberately NOT a separate container: the isolation
# claim is about credential scope, and a subprocess makes that auditable in one file.
EXPOSE 8000
# Render injects $PORT. Never hard-code the port.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
