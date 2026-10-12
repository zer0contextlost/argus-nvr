# Argus NVR container image. Run with docker-compose.yml (host networking).

FROM node:22-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG GO2RTC_VERSION=v1.9.14
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates libgl1 libglib2.0-0 tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/argus/tools \
 && curl -fsSL -o /opt/argus/tools/go2rtc \
    "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH:-amd64}" \
 && chmod +x /opt/argus/tools/go2rtc

WORKDIR /opt/argus/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel/ sentinel/
COPY --from=frontend /src/frontend/dist/ frontend/dist/
COPY docker/entrypoint.sh /usr/local/bin/argus-entrypoint

ENV ARGUS_DATA_ROOT=/data \
    ARGUS_TOOLS_ROOT=/opt/argus/tools \
    ARGUS_ENV_FILE=/data/.env \
    ARGUS_HOST=0.0.0.0 \
    ARGUS_PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080 8555/tcp 8555/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -fsS "http://127.0.0.1:${ARGUS_PORT}/api/health" >/dev/null || exit 1
ENTRYPOINT ["argus-entrypoint"]
