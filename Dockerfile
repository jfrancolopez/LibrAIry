# =============================================================================
# LibrAIry — Container Image
# =============================================================================
# Lean Debian base with all required tools pre-installed.
# czkawka_cli is NOT built here (takes 10+ min) — see Instructions.md for
# optional manual build inside a running container.
# =============================================================================

FROM debian:bookworm-slim

LABEL maintainer="Franco <solosoyfranco>" \
      description="LibrAIry — AI-powered file organizer" \
      version="2.0"

ENV DEBIAN_FRONTEND=noninteractive

# Install all required system tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Core utilities
        bash curl wget git jq bc coreutils \
        # Media analysis
        ffmpeg \
        # Audio fingerprinting (fpcalc binary is in chromaprint-utils)
        chromaprint-utils \
        # Metadata extraction
        libimage-exiftool-perl \
        # Duplicate detection (hash-based)
        rmlint \
        # Python runtime
        python3 python3-pip python3-venv \
        # Misc
        iputils-ping procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Verify key tools are available
RUN ffprobe -version 2>&1 | head -1 \
    && fpcalc -version \
    && rmlint --version \
    && python3 --version

# Set up workspace
WORKDIR /workspace

# Copy the inbox-processor package
COPY inbox-processor/ /workspace/inbox-processor/

# Make scripts executable
RUN find /workspace/inbox-processor/scripts -name "*.sh" -exec chmod +x {} \; \
    && chmod +x /workspace/inbox-processor/catalog/catalog_main.py

# Create data directory structure (actual data comes from volume mounts)
RUN mkdir -p \
        /data/inbox \
        /data/library \
        /data/quarantine \
        /data/reports

# Default working directory for running scripts
WORKDIR /workspace/inbox-processor/scripts

CMD ["/bin/bash"]
