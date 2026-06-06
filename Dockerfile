FROM python:3.11-slim

# curl is used in the entrypoint to wait for the backend health check
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies (layer-cached before code is copied)
COPY Argus/requirements-prod.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and assets
COPY Argus/ ./Argus/
COPY index.html ./index.html
COPY argus-logo.png ./argus-logo.png
COPY videos/ ./videos/
COPY ["background images/", "./background images/"]
COPY ["OFFICE ALL CHARAKTER/", "./OFFICE ALL CHARAKTER/"]
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

ENV PORT=8080

CMD ["/app/entrypoint.sh"]