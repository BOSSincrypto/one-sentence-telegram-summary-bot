# syntax=docker/dockerfile:1

# Wheels are built in a throwaway stage so the runtime image carries no
# compiler and no build cache — it lands around 120 MB, which keeps both the
# Fly image cost and the cold-start time down.
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/bot.db \
    PORT=8080

WORKDIR /app

COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY app ./app

# The volume is mounted here at run time; creating it keeps local `docker run`
# working without one.
RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "-m", "app.main"]
