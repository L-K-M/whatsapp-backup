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


# One notices file for the whole image: wacli, the Go standard library and
# every Go module linked into the binary (read from its embedded module
# list, with the toolchain and module cache that built it), then the web
# UI's notices. Fails when a component has no license text.
FROM wacli-builder AS notices

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/licenses/go-dart-notices.py licenses/go-overrides.json /tmp/licenses/
COPY --from=web-builder /src/whatsapp-backup/ui/dist/Third-Party-Notices.txt /tmp/licenses/web-ui-Third-Party-Notices.txt
RUN repo=$(git remote get-url origin) \
    && python3 /tmp/licenses/go-dart-notices.py go --binary /out/wacli --title wacli \
         --main-dir /src/wacli --main-source "${repo%.git}/tree/{revision}" \
         --main-note "Built with the patches in patches/wacli/ of https://github.com/L-K-M/whatsapp-backup applied." \
         --note "This file covers the whatsapp-backup Docker image: the wacli binary first, then the web UI, whose notices are appended at the end." \
         --note "wacli links go.mau.fi/libsignal (GPL-3.0-only), so the wacli binary is a combined work distributed under GPL-3.0-only. Its source is wacli at the revision on its Source line with the patches its entry names, and the exact module versions listed below." \
         --overrides /tmp/licenses/go-overrides.json \
         --append /tmp/licenses/web-ui-Third-Party-Notices.txt \
         -o /out/notices/Third-Party-Notices.txt


FROM python:3.14-slim-bookworm AS base

FROM base AS runtime

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
# OS package notices, right after the package installation: every Debian
# package with its source version, checked for its copyright file.
RUN --mount=type=bind,source=scripts/licenses/os-packages.sh,target=/tmp/os-packages.sh \
    --mount=type=bind,from=base,source=/var/lib/dpkg/status,target=/tmp/base-db \
    sh /tmp/os-packages.sh --image whatsapp-backup --base-db /tmp/base-db \
      /usr/share/doc/whatsapp-backup/OS-Packages.txt

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY --from=web-builder /src/whatsapp-backup/ui/dist /app/web
COPY --from=wacli-builder /out/wacli /usr/local/bin/wacli
# Third-party notices and the project's own license, next to OS-Packages.txt.
COPY --from=notices /out/notices/ /usr/share/doc/whatsapp-backup/
COPY LICENSE /usr/share/doc/whatsapp-backup/LICENSE
# whatsapp-backup's own code is Unlicense; the other terms come from the
# notices above (scripts/licenses/oci-licenses.py expr). tini is MIT.
LABEL org.opencontainers.image.licenses="Unlicense AND Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND GPL-3.0-only AND ISC AND MIT AND MPL-2.0 AND OFL-1.1"

# The image must carry its notices: fail when one is missing or empty, or
# lacks a component the README names for its terms. CPython's license comes
# with the base image.
RUN doc=/usr/share/doc/whatsapp-backup \
    && test -s "$doc/Third-Party-Notices.txt" \
    && test -s "$doc/OS-Packages.txt" \
    && test -s "$doc/LICENSE" \
    && test -s /app/web/Third-Party-Notices.txt \
    && test -s "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')/LICENSE.txt" \
    && for sentinel in '^Go standard library and runtime ' 'Note: Built with the patches in patches/wacli/' \
         '^go.mau.fi/libsignal ' '^go.mau.fi/whatsmeow ' 'Sysfont' 'Geneva 9.1' 'SIL OPEN FONT LICENSE Version 1.1'; do \
         grep -q "$sentinel" "$doc/Third-Party-Notices.txt" \
           || { echo "Third-Party-Notices.txt lacks '$sentinel'" >&2; exit 1; }; \
       done

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
