# syntax=docker/dockerfile:1.7

FROM golang:1.27-bookworm AS wacli-builder

ARG WACLI_REPO=https://github.com/steipete/wacli.git
ARG WACLI_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/wacli
RUN git clone --depth 1 --branch "${WACLI_REF}" "${WACLI_REPO}" .
COPY patches/wacli /tmp/wacli-patches
RUN git apply /tmp/wacli-patches/session-sqlite.patch \
    && cp /tmp/wacli-patches/session_sqlite_test.go internal/wa/backup_session_sqlite_test.go \
    && go test -tags sqlite_fts5 ./internal/wa
RUN go build -tags sqlite_fts5 -o /out/wacli ./cmd/wacli \
    && strip /out/wacli


FROM node:22-bookworm-slim AS web-builder

WORKDIR /src/whatsapp-backup/ui
COPY ui/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --ignore-scripts; else npm install --ignore-scripts; fi
COPY media /src/whatsapp-backup/media
COPY ui ./
# The build also writes dist/Third-Party-Notices.txt and fails when a bundled
# package or font has no license text. Each script carries a banner that
# points to that file.
RUN npm run build \
    && test -s dist/Third-Party-Notices.txt \
    && test -n "$(find dist/assets -name '*.js' -print -quit)" \
    && if grep -L 'Third-Party-Notices.txt' dist/assets/*.js | grep .; then exit 1; fi


FROM python:3.14-slim-bookworm AS runtime

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

RUN --mount=type=bind,source=tests,target=/tmp/tests \
    python -m unittest discover -s /tmp/tests -v

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
