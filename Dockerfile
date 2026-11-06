FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHAT_DB_PATH=/data/chat_history.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

# Run as non-root. UID 1000 matches the default user on Amazon Linux / Ubuntu EC2
# (ec2-user / ubuntu), so a read-only mounted ~/.aws with 600 permissions is readable.
RUN useradd --uid 1000 --create-home app \
    && mkdir /data && chown app:app /data
USER app

VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz')" || exit 1

# One worker + threads: the app uses a single SQLite file.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
