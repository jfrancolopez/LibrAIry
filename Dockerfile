FROM python:3.12-slim-bookworm AS builder

ARG CZKAWKA_CLI_VERSION=8.0.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      cargo \
      pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip wheel --no-cache-dir --wheel-dir /wheels . \
    && cargo install czkawka_cli --locked --version "${CZKAWKA_CLI_VERSION}" \
    && cp /root/.cargo/bin/czkawka_cli /wheels/czkawka_cli

FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="LibrAIry" \
      org.opencontainers.image.description="Privacy-first file organizer" \
      org.opencontainers.image.version="0.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PUID=99 \
    PGID=100

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      chromaprint-tools \
      libimage-exiftool-perl \
      poppler-utils \
      rclone \
      rmlint \
      gosu \
      passwd \
    && groupadd --system --gid 1000 librairy \
    && useradd --system --uid 1000 --gid librairy --home-dir /app --shell /usr/sbin/nologin librairy \
    && mkdir -p /data/inbox /data/library /data/quarantine /data/appdata /app \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /tmp/wheels
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && install -m 0755 /tmp/wheels/czkawka_cli /usr/local/bin/czkawka_cli \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && rm -rf /tmp/wheels \
    && ffprobe -version >/dev/null \
    && fpcalc -version >/dev/null \
    && pdftotext -v >/dev/null 2>&1 \
    && rclone version >/dev/null \
    && rmlint --version >/dev/null \
    && command -v czkawka_cli >/dev/null \
    && librairy --help >/dev/null

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["librairy", "run"]
