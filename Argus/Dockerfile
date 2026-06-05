# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools needed for faiss-cpu, scipy, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ─────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source (no venv, no reports, no __pycache__)
COPY agents/       agents/
COPY api/          api/
COPY core/         core/
COPY eval/         eval/
COPY simulation/   simulation/
COPY storage/      storage/
COPY make_investor_report.py .
COPY demo_app.py             .

# Cloud Run injects $PORT (default 8080); uvicorn reads it at startup
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn demo_app:app --host 0.0.0.0 --port ${PORT}"]
