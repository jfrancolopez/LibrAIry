# Debian trixie, not bookworm. Bookworm ships perl/nss/cjson/mbedtls/jpeg-xl/tiff
# versions whose critical+high CVEs have no fix in that release, so the only way
# to clear them is to move the whole base forward. Python stays on 3.12 to match
# the CI matrix.
FROM python:3.12-slim-trixie AS builder

ARG CZKAWKA_CLI_VERSION=11.0.1
ARG CZKAWKA_SHA256_AMD64=2f81d63f79047294629253f4232c47cf5a2c6e55b9e34f23d11c2c810cfcbc09
ARG CZKAWKA_SHA256_ARM64=eb333e3b29d576db6d2365cd9deff454cfc9e7bc9b8b6dfefb4ab82b14db7dc8

# Debian's rclone package is compiled against Go 1.19, which drags in 4 critical
# and 29 high CVEs from the Go standard library that Debian will not backport.
# Upstream's static build tracks current Go, so we vendor it instead.
ARG RCLONE_VERSION=v1.75.0
ARG RCLONE_SHA256_AMD64=aa2804e08f48250e71009c727124b6341cd0288465804a9a09d14663cabafbaa
ARG RCLONE_SHA256_ARM64=d0ad88ba4c8e285b7c9efa591e0ab643280a91741e13c27f3a9c0957ccfa5203

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip wheel --no-cache-dir --wheel-dir /wheels . \
    && mkdir -p /out \
    && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
         amd64) CZ_ASSET=linux_czkawka_cli_x86_64; CZ_SHA="${CZKAWKA_SHA256_AMD64}"; \
                RC_ARCH=amd64; RC_SHA="${RCLONE_SHA256_AMD64}" ;; \
         arm64) CZ_ASSET=linux_czkawka_cli_arm64;  CZ_SHA="${CZKAWKA_SHA256_ARM64}"; \
                RC_ARCH=arm64; RC_SHA="${RCLONE_SHA256_ARM64}" ;; \
         *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/czkawka_cli \
       "https://github.com/qarmin/czkawka/releases/download/${CZKAWKA_CLI_VERSION}/${CZ_ASSET}" \
    && echo "${CZ_SHA}  /tmp/czkawka_cli" | sha256sum -c - \
    && install -m 0755 /tmp/czkawka_cli /out/czkawka_cli \
    && curl -fsSL -o /tmp/rclone.zip \
       "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${RC_ARCH}.zip" \
    && echo "${RC_SHA}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip -q -j /tmp/rclone.zip "*/rclone" -d /out \
    && chmod 0755 /out/rclone \
    && rm -f /tmp/czkawka_cli /tmp/rclone.zip

FROM python:3.12-slim-trixie AS runtime

LABEL org.opencontainers.image.title="LibrAIry" \
      org.opencontainers.image.description="Privacy-first file organizer" \
      org.opencontainers.image.version="1.2.0" \
      org.opencontainers.image.source="https://github.com/jfrancolopez/LibrAIry"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PUID=99 \
    PGID=100

# No gosu: Debian's build is the other Go-1.19 binary. setpriv ships in
# util-linux (C, no Go runtime) and drops privileges the same way — exec, no
# extra supervisor process.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      libchromaprint-tools \
      libimage-exiftool-perl \
      poppler-utils \
      rmlint \
      util-linux \
      passwd \
    && groupadd --system --gid 1000 librairy \
    && useradd --system --uid 1000 --gid librairy --home-dir /app --shell /usr/sbin/nologin librairy \
    && mkdir -p /data/inbox /data/library /data/quarantine /data/appdata /app \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /tmp/wheels
COPY --from=builder /out/czkawka_cli /out/rclone /usr/local/bin/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && chmod 0755 /usr/local/bin/czkawka_cli /usr/local/bin/rclone \
                  /usr/local/bin/docker-entrypoint.sh \
    && rm -rf /tmp/wheels \
    && chown -R 1000:1000 /data /app \
    && ffprobe -version >/dev/null \
    && fpcalc -version >/dev/null \
    && pdftotext -v >/dev/null 2>&1 \
    && rclone version >/dev/null \
    && rmlint --version >/dev/null \
    && czkawka_cli --version >/dev/null \
    && setpriv --help >/dev/null \
    && librairy --help >/dev/null

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DASHBOARD_PORT:-8080}/healthz" || exit 1

# Non-root by default. PUID/PGID remapping needs root to chown the mounts, so
# hosts that rely on it (UNRAID: 99:100) run the container with `user: "0:0"` —
# the entrypoint still takes its root path and drops to PUID:PGID itself.
USER librairy

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["librairy", "run"]
