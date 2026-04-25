# syntax=docker/dockerfile:1.7

FROM golang:1.25-bookworm AS wacli-builder

ARG WACLI_REPO=https://github.com/steipete/wacli.git
ARG WACLI_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/wacli
RUN git clone --depth 1 --branch "${WACLI_REF}" "${WACLI_REPO}" .
RUN go build -tags sqlite_fts5 -o /out/wacli ./cmd/wacli \
    && strip /out/wacli


FROM node:22-bookworm-slim AS web-builder

WORKDIR /src/whatsapp-backup/ui
COPY ui/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --ignore-scripts; else npm install --ignore-scripts; fi
COPY media /src/whatsapp-backup/media
COPY ui ./
RUN npm run build


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    WACLI_STORE_DIR=/data/wacli \
    ARCHIVE_DIR=/host-data/archive \
    WACLI_BIN=/usr/local/bin/wacli \
    WEB_ROOT=/app/web \
    HOST=0.0.0.0 \
    PORT=8080 \
    AUTO_SYNC=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY --from=web-builder /src/whatsapp-backup/ui/dist /app/web
COPY --from=wacli-builder /out/wacli /usr/local/bin/wacli

RUN chmod -R a+rX /app \
    && chmod 755 /usr/local/bin/wacli \
    && mkdir -p /data/wacli /data/state /host-data/archive/messages \
    && chmod -R 700 /data/wacli \
    && chmod -R 700 /data/state \
    && chmod -R 755 /host-data \
    && chmod 755 /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/app/app.py"]
