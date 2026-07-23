FROM python:3.12-slim-bookworm AS builder

ARG CZKAWKA_CLI_VERSION=11.0.1
ARG CZKAWKA_SHA256_AMD64=2f81d63f79047294629253f4232c47cf5a2c6e55b9e34f23d11c2c810cfcbc09
ARG CZKAWKA_SHA256_ARM64=eb333e3b29d576db6d2365cd9deff454cfc9e7bc9b8b6dfefb4ab82b14db7dc8
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip wheel --no-cache-dir --wheel-dir /wheels . \
    && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
         amd64) CZ_ASSET=linux_czkawka_cli_x86_64; CZ_SHA="${CZKAWKA_SHA256_AMD64}" ;; \
         arm64) CZ_ASSET=linux_czkawka_cli_arm64;  CZ_SHA="${CZKAWKA_SHA256_ARM64}" ;; \
         *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /wheels/czkawka_cli \
       "https://github.com/qarmin/czkawka/releases/download/${CZKAWKA_CLI_VERSION}/${CZ_ASSET}" \
    && echo "${CZ_SHA}  /wheels/czkawka_cli" | sha256sum -c - \
    && chmod 0755 /wheels/czkawka_cli

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
      libchromaprint-tools \
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
    && czkawka_cli --version >/dev/null \
    && librairy --help >/dev/null

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["librairy", "run"]
