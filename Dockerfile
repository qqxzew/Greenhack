FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY Argus/requirements-prod.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY Argus/ ./Argus/
COPY index.html ./index.html

# Run from the Argus package directory so internal imports resolve
WORKDIR /app/Argus

ENV PORT=8080

# Shell form is required so $PORT is expanded at runtime (Cloud Run injects it)
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
