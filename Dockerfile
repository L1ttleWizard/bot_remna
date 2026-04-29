# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Корневые модули
COPY bot.py app.py auth.py config.py database.py remnawave_api.py scheduler.py ./
COPY clients.py formatters.py keyboards.py ./
# Хендлеры и сервисы
COPY handlers/ ./handlers/
COPY services/ ./services/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# При read_only rootfs кэш и временные файлы не пишем в слой образа.
ENV HOME=/tmp

# Образ без секретов; переменные задаются при запуске (compose / orchestrator).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "bot.py"]
