FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system bot && useradd --system --gid bot --home-dir /app bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py admin.py ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data && chown -R bot:bot /app /data

USER bot

CMD ["python", "bot.py"]
