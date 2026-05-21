#!/usr/bin/env bash
# =============================================================================
# LibrAIry — Interactive Setup Wizard
# =============================================================================
# Run this ONCE on your host machine before starting the container.
# It generates a .env file and creates the required directory structure.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"

# ── Colours ──────────────────────────────────────────────────────────────────
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[1;33m'
C_BLUE='\033[0;34m'
C_RED='\033[0;31m'
C_CYAN='\033[0;36m'
C_DIM='\033[2m'

banner() {
  echo ""
  echo -e "${C_BOLD}${C_CYAN}╔══════════════════════════════════════════════╗${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}║   LibrAIry — First-Run Setup Wizard  v2.0   ║${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}╚══════════════════════════════════════════════╝${C_RESET}"
  echo ""
}

section() {
  echo ""
  echo -e "${C_BOLD}${C_BLUE}── $1 ──────────────────────────────────────────${C_RESET}"
}

ok()   { echo -e "  ${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "  ${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "  ${C_RED}✗${C_RESET} $*"; }
info() { echo -e "  ${C_DIM}$*${C_RESET}"; }

ask() {
  local prompt="$1"
  local default="${2:-}"
  local var_name="$3"
  local is_secret="${4:-false}"

  if [[ -n "$default" ]]; then
    echo -ne "  ${C_BOLD}$prompt${C_RESET} ${C_DIM}[$default]${C_RESET}: "
  else
    echo -ne "  ${C_BOLD}$prompt${C_RESET}: "
  fi

  local value
  if [[ "$is_secret" == true ]]; then
    read -rs value; echo ""
  else
    read -r value
  fi

  if [[ -z "$value" ]]; then
    value="$default"
  fi

  eval "$var_name=\"\$value\""
}

ask_yn() {
  local prompt="$1"
  local default="${2:-y}"
  local var_name="$3"
  local hint; hint="[Y/n]"; [[ "$default" == "n" ]] && hint="[y/N]"
  echo -ne "  ${C_BOLD}$prompt${C_RESET} ${C_DIM}$hint${C_RESET}: "
  read -r reply
  [[ -z "$reply" ]] && reply="$default"
  if [[ "$reply" =~ ^[Yy] ]]; then
    eval "$var_name=true"
  else
    eval "$var_name=false"
  fi
}

# ── Load existing .env if present ────────────────────────────────────────────
banner

if [[ -f "$ENV_FILE" ]]; then
  warn ".env already exists. Editing existing configuration."
  # shellcheck disable=SC1090
  source "$ENV_FILE" 2>/dev/null || true
  echo ""
fi

# ── SECTION 1: Folder Paths ───────────────────────────────────────────────────
section "Folder Paths"
echo -e "  ${C_DIM}These map your host directories into the container."
echo -e "  Absolute paths recommended (e.g. /mnt/nas/inbox or /Volumes/Media/inbox).${C_RESET}"
echo ""

ask "Inbox folder  (files to organize)" "${HOST_INBOX_DIR:-$HOME/librairy/inbox}" HOST_INBOX_DIR
ask "Library folder (organized destination)" "${HOST_LIBRARY_DIR:-$HOME/librairy/library}" HOST_LIBRARY_DIR
ask "Quarantine folder (duplicates/flagged)" "${HOST_QUARANTINE_DIR:-$HOME/librairy/quarantine}" HOST_QUARANTINE_DIR
ask "Reports folder (JSON logs)" "${HOST_REPORTS_DIR:-$HOME/librairy/reports}" HOST_REPORTS_DIR

# Create directories on host
for dir in "$HOST_INBOX_DIR" "$HOST_LIBRARY_DIR" "$HOST_QUARANTINE_DIR" "$HOST_REPORTS_DIR"; do
  if mkdir -p "$dir" 2>/dev/null; then
    ok "Created: $dir"
  else
    err "Could not create: $dir  — check permissions"
  fi
done


# ── SECTION 2: Catalog APIs (required, free) ──────────────────────────────────
section "Catalog APIs  (required, 100% free)"
echo -e "  ${C_DIM}These replace AI for music/movie classification."
echo -e "  Without them the pipeline depends entirely on AI for everything.${C_RESET}"
echo ""

echo -e "  ${C_YELLOW}TMDB${C_RESET} — Movie & TV metadata"
info "  Get a free API key at: https://www.themoviedb.org/settings/api"
ask "TMDB API key" "${TMDB_KEY:-}" TMDB_KEY
[[ -n "$TMDB_KEY" ]] && ok "TMDB key set" || warn "TMDB key empty — video classification will use AI only"

echo ""
echo -e "  ${C_YELLOW}AcoustID${C_RESET} — Audio fingerprint → MusicBrainz lookup"
info "  Get a free key at: https://acoustid.org/login  → 'Register application'"
ask "AcoustID API key" "${ACOUSTID_KEY:-}" ACOUSTID_KEY
[[ -n "$ACOUSTID_KEY" ]] && ok "AcoustID key set" || warn "AcoustID key empty — audio fingerprinting disabled (embedded tags still work)"


# ── SECTION 3: AI Providers ───────────────────────────────────────────────────
section "AI Providers  (optional — fallback when catalog APIs don't match)"
echo -e "  ${C_DIM}Configure any providers you want available. The order below"
echo -e "  determines which is tried first when catalog lookup fails.${C_RESET}"
echo ""

# Ollama
echo -e "  ${C_YELLOW}Ollama${C_RESET} (local, no cost, fully private)"
info "  Use host.docker.internal:11434 if Ollama runs on the same machine as Docker"
ask "Ollama host URL" "${OLLAMA_HOST:-http://host.docker.internal:11434}" OLLAMA_HOST
ask "Primary model" "${OLLAMA_MODEL_PRIMARY:-llama3.1:8b}" OLLAMA_MODEL_PRIMARY
ask "Secondary model (fallback)" "${OLLAMA_MODEL_SECONDARY:-qwen2.5:7b}" OLLAMA_MODEL_SECONDARY

echo ""
# OpenAI
echo -e "  ${C_YELLOW}OpenAI${C_RESET} (paid, very accurate)"
ask "OpenAI API key (leave blank to skip)" "${OPENAI_API_KEY:-}" OPENAI_API_KEY true
if [[ -n "$OPENAI_API_KEY" ]]; then
  ask "OpenAI model" "${OPENAI_MODEL:-gpt-4o-mini}" OPENAI_MODEL
  ok "OpenAI configured"
else
  info "  OpenAI skipped"
  OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}"
fi

echo ""
# Anthropic
echo -e "  ${C_YELLOW}Anthropic / Claude${C_RESET} (paid, very accurate)"
ask "Anthropic API key (leave blank to skip)" "${ANTHROPIC_API_KEY:-}" ANTHROPIC_API_KEY true
if [[ -n "$ANTHROPIC_API_KEY" ]]; then
  ask "Anthropic model" "${ANTHROPIC_MODEL:-claude-3-5-haiku-20241022}" ANTHROPIC_MODEL
  ok "Anthropic configured"
else
  info "  Anthropic skipped"
  ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-3-5-haiku-20241022}"
fi

echo ""
# Gemini
echo -e "  ${C_YELLOW}Google Gemini${C_RESET} (free tier available)"
info "  Get a free key at: https://aistudio.google.com/apikey"
ask "Gemini API key (leave blank to skip)" "${GEMINI_API_KEY:-}" GEMINI_API_KEY true
if [[ -n "$GEMINI_API_KEY" ]]; then
  ask "Gemini model" "${GEMINI_MODEL:-gemini-1.5-flash}" GEMINI_MODEL
  ok "Gemini configured"
else
  info "  Gemini skipped"
  GEMINI_MODEL="${GEMINI_MODEL:-gemini-1.5-flash}"
fi

echo ""
# Provider order
available=()
available+=("ollama")
[[ -n "$OPENAI_API_KEY" ]] && available+=("openai")
[[ -n "$ANTHROPIC_API_KEY" ]] && available+=("anthropic")
[[ -n "$GEMINI_API_KEY" ]] && available+=("gemini")

default_order=$(IFS=','; echo "${available[*]}")
echo -e "  ${C_DIM}Available providers: ${available[*]}${C_RESET}"
ask "AI provider order (comma-separated)" "${AI_PROVIDER_ORDER:-$default_order}" AI_PROVIDER_ORDER


# ── SECTION 4: Advanced ───────────────────────────────────────────────────────
section "Advanced Settings"

ask_yn "Show advanced options?" "n" SHOW_ADVANCED

if [[ "$SHOW_ADVANCED" == true ]]; then
  ask "Confidence threshold (0.0–1.0)" "${CONFIDENCE_THRESHOLD:-0.80}" CONFIDENCE_THRESHOLD
  ask "AI timeout (seconds)" "${AI_TIMEOUT:-120}" AI_TIMEOUT
  ask "Max files to analyze per item (0=unlimited)" "${MAX_FILES_TO_ANALYZE:-0}" MAX_FILES_TO_ANALYZE
  ask "MusicBrainz rate limit (seconds, min 1.0)" "${MB_RATE_LIMIT:-1.1}" MB_RATE_LIMIT
  ask "Dashboard port" "${DASHBOARD_PORT:-8080}" DASHBOARD_PORT
else
  CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-0.80}"
  AI_TIMEOUT="${AI_TIMEOUT:-120}"
  MAX_FILES_TO_ANALYZE="${MAX_FILES_TO_ANALYZE:-0}"
  MB_RATE_LIMIT="${MB_RATE_LIMIT:-1.1}"
  DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
fi


# ── Write .env ────────────────────────────────────────────────────────────────
section "Writing configuration"

cat > "$ENV_FILE" <<EOF
# LibrAIry Configuration — generated by setup.sh on $(date)
# Edit this file or re-run ./setup.sh to change any setting.

# ── Folder Paths ─────────────────────────────────────────────────────────────
HOST_INBOX_DIR=${HOST_INBOX_DIR}
HOST_LIBRARY_DIR=${HOST_LIBRARY_DIR}
HOST_QUARANTINE_DIR=${HOST_QUARANTINE_DIR}
HOST_REPORTS_DIR=${HOST_REPORTS_DIR}

# ── Catalog APIs (required, free) ────────────────────────────────────────────
TMDB_KEY=${TMDB_KEY}
ACOUSTID_KEY=${ACOUSTID_KEY}
MB_RATE_LIMIT=${MB_RATE_LIMIT}

# ── AI Provider Orchestration ─────────────────────────────────────────────────
AI_PROVIDER_ORDER=${AI_PROVIDER_ORDER}
CONFIDENCE_THRESHOLD=${CONFIDENCE_THRESHOLD}
USE_MULTI_AI=true

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_HOST=${OLLAMA_HOST}
OLLAMA_MODEL_PRIMARY=${OLLAMA_MODEL_PRIMARY}
OLLAMA_MODEL_SECONDARY=${OLLAMA_MODEL_SECONDARY}

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL}

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_MODEL=${ANTHROPIC_MODEL}

# ── Gemini ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=${GEMINI_MODEL}

# ── Pipeline Tuning ───────────────────────────────────────────────────────────
AI_TIMEOUT=${AI_TIMEOUT}
MAX_FILES_TO_ANALYZE=${MAX_FILES_TO_ANALYZE}
MAX_AI_RETRIES=2
BATCH_SIZE=50
CZKAWKA_EXTENSIONS=jpg,png,jpeg,gif,bmp,heic,avif,mp4,mkv,mov,avi,mp3,flac,wav,ogg,txt,pdf,docx

# ── Internal container paths (do not change) ──────────────────────────────────
INBOX_DIR=/data/inbox
LIBRARY_DIR=/data/library
QUARANTINE_DIR=/data/quarantine
REPORTS_DIR=/data/reports
CATALOG_DIR=/workspace/inbox-processor/catalog

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_PORT=${DASHBOARD_PORT}
EOF

ok ".env written to $ENV_FILE"


# ── API connectivity tests ────────────────────────────────────────────────────
section "Testing API connectivity"

test_api() {
  local name="$1"; local url="$2"; local key_check="$3"
  if [[ -z "$key_check" ]]; then
    info "  $name: not configured (skipped)"
    return
  fi
  if curl -sS --max-time 8 "$url" >/dev/null 2>&1; then
    ok "$name: reachable"
  else
    warn "$name: unreachable — check key/network (pipeline will skip it)"
  fi
}

test_api "MusicBrainz" "https://musicbrainz.org/ws/2/artist?query=test&fmt=json&limit=1" "always"
test_api "TMDB" "https://api.themoviedb.org/3/configuration?api_key=${TMDB_KEY}" "$TMDB_KEY"
test_api "AcoustID" "https://api.acoustid.org/v2/lookup?client=${ACOUSTID_KEY}&format=json" "$ACOUSTID_KEY"
test_api "Ollama" "${OLLAMA_HOST}/api/tags" "${OLLAMA_HOST}"
[[ -n "$OPENAI_API_KEY" ]] && test_api "OpenAI" "https://api.openai.com/v1/models" "$OPENAI_API_KEY" || true
[[ -n "$ANTHROPIC_API_KEY" ]] && test_api "Anthropic" "https://api.anthropic.com/v1/models" "$ANTHROPIC_API_KEY" || true
[[ -n "$GEMINI_API_KEY" ]] && test_api "Gemini" "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}" "$GEMINI_API_KEY" || true


# ── Summary ───────────────────────────────────────────────────────────────────
section "Setup complete"

echo ""
echo -e "  ${C_GREEN}${C_BOLD}Configuration saved to:${C_RESET}  $ENV_FILE"
echo ""
echo -e "  ${C_BOLD}Next steps:${C_RESET}"
echo ""
echo -e "  ${C_CYAN}1.${C_RESET}  Build the container:"
echo -e "       ${C_DIM}docker compose build${C_RESET}"
echo ""
echo -e "  ${C_CYAN}2.${C_RESET}  Drop files into your inbox:"
echo -e "       ${C_DIM}$HOST_INBOX_DIR${C_RESET}"
echo ""
echo -e "  ${C_CYAN}3.${C_RESET}  Run the pipeline (interactive):"
echo -e "       ${C_DIM}docker compose run --rm librairy${C_RESET}"
echo -e "       ${C_DIM}./main.sh${C_RESET}         # inside container"
echo ""
echo -e "  ${C_CYAN}4.${C_RESET}  Or run non-interactively:"
echo -e "       ${C_DIM}docker compose run --rm librairy ./main.sh${C_RESET}"
echo ""
echo -e "  ${C_CYAN}5.${C_RESET}  Auto-watch inbox (runs pipeline on new files):"
echo -e "       ${C_DIM}docker compose --profile watch up -d${C_RESET}"
echo ""
