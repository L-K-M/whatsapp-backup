# Build stage: compile wacli from source
FROM golang:1.25-bookworm AS builder

ARG WACLI_VERSION=v0.6.0

WORKDIR /build

# Install build dependencies required for CGO/SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Clone wacli at the specified version
RUN git clone --depth=1 --branch ${WACLI_VERSION} https://github.com/steipete/wacli.git .

# Build wacli with FTS5 full-text search support
RUN CGO_ENABLED=1 GOFLAGS="-buildvcs=false" \
    go build -tags sqlite_fts5 -trimpath -ldflags="-s -w" \
    -o /usr/local/bin/wacli ./cmd/wacli

# Runtime stage
FROM debian:bookworm-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the wacli binary from the build stage
COPY --from=builder /usr/local/bin/wacli /usr/local/bin/wacli

# Copy the entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create the data directory for persistent storage
RUN mkdir -p /data

# Store directory used by wacli
ENV WACLI_STORE_DIR=/data

# Persist the wacli store (messages, media, session) across container restarts
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]

# Default: run a continuous sync (requires prior authentication via `auth`)
CMD ["sync", "--follow"]
