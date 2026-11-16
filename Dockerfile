FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Non-root; the named volume inherits /data ownership on first use.
RUN useradd --system --uid 10001 relay && mkdir -p /data/cache && chown -R relay /data
USER relay
ENV TOML_RELAY_CACHE_DIR=/data/cache

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
