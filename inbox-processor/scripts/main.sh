#!/usr/bin/env bash
# =============================================================================
# LibrAIry — Pipeline Orchestrator
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTS_DIR="${REPORTS_DIR:-/data/reports}"
LOG_FILE="${LOG_FILE:-$REPORTS_DIR/pipeline.log}"
mkdir -p "$REPORTS_DIR"

# ── Colours ───────────────────────────────────────────────────────────────────
C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_RED='\033[0;31m'
C_BLUE='\033[0;34m';  C_CYAN='\033[0;36m';   C_BOLD='\033[1m'; C_RESET='\033[0m'

banner() {
  echo ""
  echo -e "${C_BOLD}${C_CYAN}╔══════════════════════════════════════════════╗${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}║              LibrAIry v2.0                   ║${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}║     AI-Powered Library Organizer             ║${C_RESET}"
  echo -e "${C_BOLD}${C_CYAN}╚══════════════════════════════════════════════╝${C_RESET}"
  echo ""
}

config_summary() {
  echo -e "${C_BOLD}Configuration:${C_RESET}"
  echo -e "  Inbox     : ${INBOX_DIR:-/data/inbox}"
  echo -e "  Library   : ${LIBRARY_DIR:-/data/library}"
  echo -e "  Quarantine: ${QUARANTINE_DIR:-/data/quarantine}"
  echo -e "  Reports   : ${REPORTS_DIR:-/data/reports}"
  echo ""
  echo -e "${C_BOLD}Catalog APIs:${C_RESET}"
  [[ -n "${TMDB_KEY:-}" ]]      && echo -e "  ${C_GREEN}✓${C_RESET} TMDB (movies/TV)" \
                                 || echo -e "  ${C_YELLOW}✗${C_RESET} TMDB not configured — set TMDB_KEY"
  [[ -n "${ACOUSTID_KEY:-}" ]]  && echo -e "  ${C_GREEN}✓${C_RESET} AcoustID (audio fingerprint)" \
                                 || echo -e "  ${C_YELLOW}✗${C_RESET} AcoustID not configured — set ACOUSTID_KEY"
  echo -e "  ${C_GREEN}✓${C_RESET} MusicBrainz (free, always on)"
  echo ""
  echo -e "${C_BOLD}AI Providers (fallback order):${C_RESET}"
  IFS=',' read -ra _order <<< "${AI_PROVIDER_ORDER:-ollama,openai,anthropic,gemini}"
  for _p in "${_order[@]}"; do
    _p="${_p// /}"
    case "$_p" in
      ollama)
        echo -e "  ${C_GREEN}✓${C_RESET} Ollama @ ${OLLAMA_HOST:-not set}"
        ;;
      openai)
        [[ -n "${OPENAI_API_KEY:-}" ]] \
          && echo -e "  ${C_GREEN}✓${C_RESET} OpenAI (${OPENAI_MODEL:-gpt-4o-mini})" \
          || echo -e "  ${C_YELLOW}–${C_RESET} OpenAI (no key)"
        ;;
      anthropic)
        [[ -n "${ANTHROPIC_API_KEY:-}" ]] \
          && echo -e "  ${C_GREEN}✓${C_RESET} Anthropic (${ANTHROPIC_MODEL:-claude-3-5-haiku})" \
          || echo -e "  ${C_YELLOW}–${C_RESET} Anthropic (no key)"
        ;;
      gemini)
        [[ -n "${GEMINI_API_KEY:-}" ]] \
          && echo -e "  ${C_GREEN}✓${C_RESET} Gemini (${GEMINI_MODEL:-gemini-1.5-flash})" \
          || echo -e "  ${C_YELLOW}–${C_RESET} Gemini (no key)"
        ;;
    esac
  done
  echo ""
}

run_step() {
  local num="$1"
  local name="$2"
  local script="$3"
  local optional="${4:-false}"

  echo -e "${C_BOLD}${C_BLUE}▶ Step $num — $name${C_RESET}"
  local start; start=$(date +%s)

  if bash "$SCRIPT_DIR/$script" 2>&1 | tee -a "$LOG_FILE"; then
    local elapsed=$(( $(date +%s) - start ))
    echo -e "${C_GREEN}  ✓ Done in ${elapsed}s${C_RESET}"
    echo ""
  else
    local ec=$?
    if [[ "$optional" == true ]]; then
      echo -e "${C_YELLOW}  ⚠ Step $num exited $ec (optional — continuing)${C_RESET}"
      echo ""
    else
      echo -e "${C_RED}  ✗ Step $num failed (exit $ec)${C_RESET}"
      echo -e "${C_RED}    See $LOG_FILE for details${C_RESET}"
      exit $ec
    fi
  fi
}

# =============================================================================
banner
config_summary

echo -e "${C_BOLD}Starting pipeline at $(date)${C_RESET}"
echo "─────────────────────────────────────────────────"
echo ""

run_step 1 "Duplicate scan (rmlint)"             "step1_scan.sh"             optional
run_step 2 "Deep duplicate scan (czkawka)"        "step2_hash_audio_video.sh" optional
run_step 3 "Classify inbox (catalog + AI)"        "step3_classify.sh"
run_step 4 "Dry-run preview"                      "step4_dryrun.sh"

echo ""
echo -e "${C_YELLOW}${C_BOLD}Dry run complete.${C_RESET}"
echo -e "Review the output above, then run step 5 to commit moves:"
echo ""
echo -e "  ${C_CYAN}./step5_commit.sh${C_RESET}"
echo ""
echo -e "${C_BOLD}Reports written to: ${REPORTS_DIR}${C_RESET}"
echo ""
