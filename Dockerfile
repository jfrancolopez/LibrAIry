FROM python:3.12-slim AS runtime

LABEL description="LibrAIry - privacy-first file organizer" \
      version="0.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg chromaprint-tools libimage-exiftool-perl rmlint \
      cargo pkg-config build-essential \
    && cargo install czkawka_cli --locked \
    && cp /root/.cargo/bin/czkawka_cli /usr/local/bin/czkawka_cli \
    && cargo uninstall czkawka_cli \
    && apt-get purge -y --auto-remove cargo pkg-config build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cargo /root/.rustup

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data/inbox /data/library /data/quarantine /data/appdata \
    && ffprobe -version >/dev/null \
    && fpcalc -version >/dev/null \
    && rmlint --version >/dev/null \
    && command -v czkawka_cli >/dev/null \
    && librairy --help >/dev/null

CMD ["librairy", "worker"]
