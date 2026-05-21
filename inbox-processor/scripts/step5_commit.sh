#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# STEP 5 — Commit: execute the file moves planned by step4
# Reads step3_summary.json, moves/renames files for real.
# Run step4_dryrun.sh first and verify output before this.
# ============================================================

REPORTS_DIR="${REPORTS_DIR:-/data/reports}"
QUARANTINE_DIR="${QUARANTINE_DIR:-/data/quarantine}"
LOG_FILE="${LOG_FILE:-/tmp/step5_commit.log}"
STEP3_JSON="${REPORTS_DIR}/step3_summary.json"
SUMMARY_JSON="${REPORTS_DIR}/step5_summary.json"
REVIEW_DIR="/data/inbox/_review_pending"

DATE_TAG=$(date +%Y-%m-%d)
QUARANTINE_TODAY="${QUARANTINE_DIR}/${DATE_TAG}"

mkdir -p "$REPORTS_DIR" "$QUARANTINE_TODAY" "$REVIEW_DIR"

echo "============================================================" | tee -a "$LOG_FILE"
echo "COMMIT at $(date)" | tee -a "$LOG_FILE"
echo "Source: $STEP3_JSON" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"

[[ ! -f "$STEP3_JSON" ]] && { echo "[step5] Missing $STEP3_JSON — run step3 first"; exit 1; }

moves=0
skipped=0
quarantined=0
errors=0

move_log=()
skip_log=()
quarantine_log=()
error_log=()

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# ----------------------------------------------------------
# Helper: safe move with collision handling
# ----------------------------------------------------------
safe_move() {
  local src="$1"
  local dst="$2"

  if [[ ! -e "$src" ]]; then
    echo -e "${RED}[step5] MISSING: $src${NC}" | tee -a "$LOG_FILE"
    ((errors++))
    error_log+=("MISSING: $src")
    return 1
  fi

  mkdir -p "$(dirname "$dst")"

  # Collision: add _1, _2, ... suffix before extension
  if [[ -e "$dst" ]]; then
    local base ext stem counter=1
    base="$(basename "$dst")"
    ext="${base##*.}"
    stem="${base%.*}"
    local dir; dir="$(dirname "$dst")"
    while [[ -e "${dir}/${stem}_${counter}.${ext}" ]]; do
      ((counter++))
    done
    dst="${dir}/${stem}_${counter}.${ext}"
    echo -e "${YELLOW}[step5] COLLISION — renamed to: $(basename "$dst")${NC}" | tee -a "$LOG_FILE"
  fi

  if mv -n -- "$src" "$dst"; then
    echo -e "${GREEN}[step5] mv${NC} $(basename "$src") → $dst" | tee -a "$LOG_FILE"
    return 0
  else
    echo -e "${RED}[step5] mv FAILED: $src → $dst${NC}" | tee -a "$LOG_FILE"
    ((errors++))
    error_log+=("MV_FAIL: $src → $dst")
    return 1
  fi
}

# ----------------------------------------------------------
# Main bundle loop
# ----------------------------------------------------------
while read -r bundle; do
  src_root=$(echo "$bundle" | jq -r '.source_path // empty')
  bundle_type=$(echo "$bundle" | jq -r '.bundle_type // "Unknown"')
  confidence=$(echo "$bundle" | jq -r '.confidence // 0.0')
  suggested_name=$(echo "$bundle" | jq -r '.suggested_name // empty')
  recommended_path=$(echo "$bundle" | jq -r '.recommended_path // empty')
  year=$(echo "$bundle" | jq -r '.metadata.year // null')

  [[ -z "$src_root" || -z "$suggested_name" || -z "$recommended_path" ]] && {
    echo -e "${RED}[step5] Skipping invalid bundle${NC}" | tee -a "$LOG_FILE"
    ((skipped++))
    continue
  }

  # Skip low-confidence items → review queue
  if (( $(echo "$confidence < 0.5" | bc -l) )); then
    echo -e "${YELLOW}[step5] Low confidence ($confidence) — sending to review: $src_root${NC}" | tee -a "$LOG_FILE"
    if [[ -e "$src_root" ]]; then
      mv -n -- "$src_root" "$REVIEW_DIR/" 2>/dev/null || true
    fi
    skip_log+=("$src_root (confidence=$confidence)")
    ((skipped++))
    continue
  fi

  # Sanitize destination root
  dest_root="${recommended_path%/}"
  [[ -z "$dest_root" ]] && dest_root="/data/library/RAM/Misc/Unsorted"

  # Build final folder name (append year for MusicAlbum)
  final_folder="$suggested_name"
  if [[ "$bundle_type" == "MusicAlbum" && -n "$year" && "$year" != "null" ]]; then
    final_folder="${suggested_name%_[0-9][0-9][0-9][0-9]}"   # strip trailing year if already there
    [[ "$suggested_name" != *"_$year" ]] && final_folder="${suggested_name}_${year}"
  fi

  echo -e "${BLUE}[step5]${NC} $src_root" | tee -a "$LOG_FILE"
  echo   "        Type: $bundle_type  Conf: $confidence" | tee -a "$LOG_FILE"

  files_json=$(echo "$bundle" | jq -c '.files // []')
  file_count=$(echo "$files_json" | jq 'length')

  # ── SINGLE FILE ──────────────────────────────────────────
  if [[ -f "$src_root" ]]; then
    orig_name=$(basename "$src_root")
    rename_to=$(echo "$bundle" | jq -r '.files[0].rename_to // empty')
    [[ -z "$rename_to" || "$rename_to" == "null" ]] && rename_to="$orig_name"
    clean_name=$(echo "$rename_to" | tr -d ':\*\?"<>|' | sed 's/[^A-Za-z0-9._\/-]/_/g')
    dest_path="${dest_root}/${clean_name}"
    if safe_move "$src_root" "$dest_path"; then
      ((moves++))
      move_log+=("$src_root → $dest_path")
    fi
    continue
  fi

  # ── DIRECTORY — multi-file mode ──────────────────────────
  if [[ -d "$src_root" ]]; then
    if (( file_count > 0 )); then
      for i in $(seq 0 $((file_count - 1))); do
        orig_name=$(echo "$files_json" | jq -r ".[$i].original_name // empty")
        rename_to=$(echo "$files_json" | jq -r ".[$i].rename_to // empty")
        file_dest=$(echo "$files_json" | jq -r ".[$i].recommended_path // empty")

        [[ -z "$orig_name" ]] && continue

        # Locate the source file (may be in a subfolder of src_root)
        src_file=$(find "$src_root" -name "$orig_name" -type f 2>/dev/null | head -1)
        if [[ -z "$src_file" || ! -f "$src_file" ]]; then
          echo -e "${YELLOW}[step5] Not found: $orig_name — skipping${NC}" | tee -a "$LOG_FILE"
          ((skipped++))
          skip_log+=("NOT_FOUND: ${src_root}/${orig_name}")
          continue
        fi

        [[ -z "$rename_to" || "$rename_to" == "null" ]] && rename_to="$orig_name"
        [[ -z "$file_dest" || "$file_dest" == "null" ]] && file_dest="${dest_root}/${final_folder}"
        clean_name=$(echo "$rename_to" | tr -d ':\*\?"<>|' | sed 's/[^A-Za-z0-9._\/-]/_/g')
        dest_file="${file_dest%/}/${clean_name}"

        if safe_move "$src_file" "$dest_file"; then
          ((moves++))
          move_log+=("$src_file → $dest_file")
        fi
      done
    else
      # No per-file list — move entire folder
      folder_dest="${dest_root}/${final_folder}"
      if safe_move "$src_root" "$folder_dest"; then
        ((moves++))
        move_log+=("$src_root → $folder_dest")
      fi
    fi

    # Remove source folder if now empty
    if [[ -d "$src_root" ]] && [[ -z "$(ls -A "$src_root" 2>/dev/null)" ]]; then
      rmdir "$src_root" 2>/dev/null || true
      echo "[step5] Removed empty folder: $src_root" | tee -a "$LOG_FILE"
    fi
    continue
  fi

  # ── SOURCE MISSING ────────────────────────────────────────
  echo -e "${RED}[step5] Source missing: $src_root${NC}" | tee -a "$LOG_FILE"
  quarantine_log+=("MISSING: $src_root")
  ((quarantined++))

done < <(jq -c '.[]' "$STEP3_JSON")

# ----------------------------------------------------------
# Write JSON summary
# ----------------------------------------------------------
jq -n \
  --arg  date        "$(date -Iseconds)" \
  --argjson moves    "$moves" \
  --argjson skipped  "$skipped" \
  --argjson quar     "$quarantined" \
  --argjson errs     "$errors" \
  --argjson move_list   "$(printf '%s\n' "${move_log[@]+"${move_log[@]}"}"   | jq -R . | jq -s .)" \
  --argjson skip_list   "$(printf '%s\n' "${skip_log[@]+"${skip_log[@]}"}"   | jq -R . | jq -s .)" \
  --argjson quar_list   "$(printf '%s\n' "${quarantine_log[@]+"${quarantine_log[@]}"}" | jq -R . | jq -s .)" \
  --argjson error_list  "$(printf '%s\n' "${error_log[@]+"${error_log[@]}"}"  | jq -R . | jq -s .)" \
  '{
    timestamp:           $date,
    files_moved:         $moves,
    files_skipped:       $skipped,
    files_quarantined:   $quar,
    errors:              $errs,
    move_log:            $move_list,
    skip_log:            $skip_list,
    quarantine_log:      $quar_list,
    error_log:           $error_list
  }' > "$SUMMARY_JSON"

echo "============================================================" | tee -a "$LOG_FILE"
echo -e "${GREEN}COMMIT COMPLETE${NC}" | tee -a "$LOG_FILE"
echo -e "  Moved:       $moves" | tee -a "$LOG_FILE"
echo -e "  Skipped:     $skipped" | tee -a "$LOG_FILE"
echo -e "  Quarantined: $quarantined" | tee -a "$LOG_FILE"
echo -e "  Errors:      $errors" | tee -a "$LOG_FILE"
echo -e "  Report:      $SUMMARY_JSON" | tee -a "$LOG_FILE"
echo "============================================================" | tee -a "$LOG_FILE"

(( errors > 0 )) && exit 1 || exit 0
