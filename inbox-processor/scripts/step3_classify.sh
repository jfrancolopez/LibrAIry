#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# ============================================================
# STEP 3 — AI-Powered File Classification
# Optimized for local Ollama models with robust error handling
# ============================================================

# Configuration
INBOX_DIR="/data/inbox"
LIBRARY_DIR="/data/library"
REPORTS_DIR="/data/reports"
REPORT_FILE="$REPORTS_DIR/step3_summary.json"
QUARANTINE_DIR="/data/quarantine"

# Ollama Configuration
OLLAMA_HOST="${OLLAMA_HOST:-http://192.168.1.94:11434}"
OLLAMA_MODEL_PRIMARY="${OLLAMA_MODEL:-llama3.1:8b}"
OLLAMA_MODEL_SECONDARY="${OLLAMA_MODEL_SECONDARY:-qwen2.5:7b}"

# AI Provider Configuration
USE_MULTI_AI="${USE_MULTI_AI:-true}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-3-5-haiku-20241022}"
CONFIDENCE_THRESHOLD=0.80

TEMP_DIR="/tmp/ai_step3"
LOG_FILE="$REPORTS_DIR/step3_ai.log"

# Performance settings
MAX_FILES_TO_ANALYZE=0
AI_TIMEOUT=120
MAX_AI_RETRIES=2
BATCH_SIZE=50

# Files and folders to ignore
DEFAULT_IGNORE_PATTERNS=(
    ".git"
    ".DS_Store" 
    "Thumbs.db"
    "desktop.ini"
    "*.tmp"
    "*.temp"
    "*.log"
    "*.cache"
    "*.swp"
    "*.swo"
    ".~lock.*"
)

# Combine default ignores with any user-provided ones
IFS=':' read -ra USER_IGNORE_PATTERNS <<< "${IGNORE_PATTERNS:-}"
IGNORE_LIST=("${DEFAULT_IGNORE_PATTERNS[@]}" "${USER_IGNORE_PATTERNS[@]}")

# Initialize
mkdir -p "$REPORTS_DIR" "$TEMP_DIR" "$QUARANTINE_DIR"
: > "$LOG_FILE"

trap 'ec=$?; echo "ERROR [step3] Failed at line $LINENO (exit $ec)"; cleanup_temp; exit $ec' ERR

cleanup_temp() {
    find "$TEMP_DIR" -type f -mmin +60 -delete 2>/dev/null || true
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Function to check if a path should be ignored
should_ignore() {
    local path="$1"
    local name=$(basename "$path")
    
    for pattern in "${IGNORE_LIST[@]}"; do
        pattern="${pattern#./}"
        
        # Exact match
        if [[ "$name" == "$pattern" ]]; then
            return 0
        fi
        
        # Wildcard match
        if [[ "$pattern" == *"*"* ]]; then
            if [[ "$name" == $pattern ]]; then
                return 0
            fi
        fi
        
        # Path contains pattern
        if [[ "$path" == *"/$pattern/"* ]] || [[ "$path" == *"/$pattern" ]]; then
            return 0
        fi
    done
    
    return 1
}

log "============================================================"
log "AI Classification Starting"
log "Primary Model: $OLLAMA_MODEL_PRIMARY @ $OLLAMA_HOST"
log "Multi-AI: $USE_MULTI_AI"
log "Ignore patterns: ${IGNORE_LIST[*]}"
log "============================================================"

# ============================================================
# Simplified Context-Aware Analysis Functions
# ============================================================

analyze_item_with_python() {
    local item="$1"
    local output_file="$2"
    
    python3 - "$item" "$output_file" "$MAX_FILES_TO_ANALYZE" "$INBOX_DIR" <<'PYTHON_ANALYZE'
import sys
import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import subprocess
import re

def get_file_type(ext):
    """Enhanced type detection with comprehensive extensions"""
    type_map = {
        # Audio
        'mp3': 'audio', 'flac': 'audio', 'wav': 'audio', 'ogg': 'audio',
        'aac': 'audio', 'm4a': 'audio', 'wma': 'audio', 'opus': 'audio',
        'alac': 'audio', 'ape': 'audio', 'aiff': 'audio', 'dsf': 'audio',
        # Video
        'mp4': 'video', 'mkv': 'video', 'avi': 'video', 'mov': 'video',
        'webm': 'video', 'wmv': 'video', 'm4v': 'video', 'flv': 'video',
        'mpg': 'video', 'mpeg': 'video', 'ts': 'video', 'm2ts': 'video',
        'vob': 'video', 'ogv': 'video', '3gp': 'video', 'mts': 'video',
        # Images
        'jpg': 'image', 'jpeg': 'image', 'png': 'image', 'gif': 'image',
        'heic': 'image', 'webp': 'image', 'bmp': 'image', 'tiff': 'image',
        'svg': 'image', 'raw': 'image', 'cr2': 'image', 'nef': 'image',
        'arw': 'image', 'dng': 'image', 'psd': 'image', 'ai': 'image',
        'eps': 'image', 'ico': 'image', 'tga': 'image',
        # Documents
        'pdf': 'document', 'doc': 'document', 'docx': 'document',
        'xls': 'document', 'xlsx': 'document', 'ppt': 'document',
        'pptx': 'document', 'odt': 'document', 'txt': 'document',
        'rtf': 'document', 'csv': 'document', 'ods': 'document',
        'odp': 'document', 'pages': 'document', 'numbers': 'document',
        'key': 'document', 'epub': 'document', 'mobi': 'document',
        # 3D Models
        'stl': 'model', 'obj': 'model', 'fbx': 'model', '3mf': 'model',
        'blend': 'model', 'step': 'model', 'stp': 'model', 'iges': 'model',
        'igs': 'model', 'dae': 'model', 'gltf': 'model', 'glb': 'model',
        'max': 'model', 'ma': 'model', 'mb': 'model', 'c4d': 'model',
        # Print files
        'gcode': 'print', 'nc': 'print', 'cnc': 'print',
        # Archives
        'zip': 'archive', '7z': 'archive', 'rar': 'archive', 'tar': 'archive',
        'gz': 'archive', 'bz2': 'archive', 'xz': 'archive', 'tgz': 'archive',
        'tbz': 'archive', 'txz': 'archive', 'lz': 'archive', 'lzma': 'archive',
        # Disk Images
        'dmg': 'diskimage', 'iso': 'diskimage', 'img': 'diskimage',
        'toast': 'diskimage', 'vdi': 'diskimage', 'vmdk': 'diskimage',
        'vhd': 'diskimage', 'qcow2': 'diskimage',
        # Code
        'py': 'code', 'js': 'code', 'java': 'code', 'cpp': 'code',
        'c': 'code', 'h': 'code', 'sh': 'code', 'go': 'code',
        'rs': 'code', 'php': 'code', 'rb': 'code', 'swift': 'code',
        'kt': 'code', 'ts': 'code', 'jsx': 'code', 'tsx': 'code',
        'css': 'code', 'scss': 'code', 'html': 'code', 'xml': 'code',
        'json': 'code', 'yaml': 'code', 'yml': 'code', 'toml': 'code',
        'sql': 'code', 'r': 'code', 'bat': 'code', 'ps1': 'code',
        # Subtitles
        'srt': 'subtitle', 'vtt': 'subtitle', 'ass': 'subtitle',
        'ssa': 'subtitle', 'sub': 'subtitle', 'idx': 'subtitle',
        # Fonts
        'ttf': 'font', 'otf': 'font', 'woff': 'font', 'woff2': 'font',
        # Database
        'db': 'database', 'sqlite': 'database', 'sqlite3': 'database',
        'mdb': 'database', 'accdb': 'database',
        # Configuration
        'conf': 'config', 'cfg': 'config', 'ini': 'config', 'properties': 'config',
        'env': 'config', 'toml': 'config', 'plist': 'config',
        # Game files
        'rom': 'game', 'gba': 'game', 'nds': 'game', 'sfc': 'game',
        'nes': 'game', 'n64': 'game', 'z64': 'game', 'sav': 'game',
    }
    return type_map.get(ext.lower(), 'other')

def extract_year_from_name(name):
    """Extract year from filename"""
    match = re.search(r'\b(19\d{2}|20\d{2})\b', name)
    return int(match.group(1)) if match else None

def extract_metadata(file_path, file_type, is_cover_art):
    """Extract metadata using external tools"""
    metadata = {}
    try:
        if file_type in ['audio', 'video']:
            ff_out = subprocess.check_output(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', file_path],
                stderr=subprocess.STDOUT, timeout=10
            )
            ff_data = json.loads(ff_out)
            duration = float(ff_data['format'].get('duration', 0))
            bit_rate = int(ff_data['format'].get('bit_rate', 0)) // 1000 if 'bit_rate' in ff_data['format'] else None
            
            if file_type == 'video' and 'streams' in ff_data:
                video_stream = next((s for s in ff_data['streams'] if s.get('codec_type') == 'video'), {})
                width = video_stream.get('width')
                height = video_stream.get('height')
                resolution = f"{width}x{height}" if width and height else None
            else:
                resolution = None
            
            metadata = {
                'duration_seconds': int(duration) if duration else None,
                'bitrate_kbps': bit_rate,
                'resolution': resolution
            }
        elif file_type == 'image':
            ex_out = subprocess.check_output(['exiftool', '-j', file_path], stderr=subprocess.STDOUT, timeout=5)
            ex_data = json.loads(ex_out)[0]
            width = ex_data.get('ImageWidth')
            height = ex_data.get('ImageHeight')
            dimensions = f"{width}x{height}" if width and height else None
            
            gps_lat = ex_data.get('GPSLatitude')
            gps_lon = ex_data.get('GPSLongitude')
            location = f"{gps_lat}, {gps_lon}" if gps_lat and gps_lon else None
            
            has_exif = any(k.startswith('EXIF') or k in ['GPSLatitude', 'GPSLongitude', 'DateTimeOriginal'] for k in ex_data.keys())
            
            metadata = {
                'type': 'image',
                'dimensions': dimensions,
                'has_exif': has_exif,
                'is_cover_art': is_cover_art,
                'date_taken': ex_data.get('DateTimeOriginal') or ex_data.get('CreateDate'),
                'orientation': ex_data.get('Orientation'),
                'camera_model': ex_data.get('Model'),
                'camera_make': ex_data.get('Make'),
                'gps_location': location,
                'city': ex_data.get('City'),
                'country': ex_data.get('Country'),
                'keywords': ex_data.get('Keywords', [])
            }
    except Exception:
        pass
    return metadata

def analyze_folder_context(folder_path, max_files, inbox_dir):
    """Comprehensive folder analysis with context awareness"""
    files_data = []
    file_types = defaultdict(int)
    extensions = defaultdict(int)
    years = []
    total_size = 0
    dates = []
    subfolder_names = []
    track_numbers = []

    file_count = 0
    unlimited = (max_files is None) or (int(max_files) <= 0)

    # Analyze parent context
    parent_context = analyze_parent_context(folder_path, inbox_dir)

    for root, dirs, files in os.walk(folder_path, topdown=True):
        dirs[:] = [d for d in dirs]
        
        if root == folder_path:
            subfolder_names = [d for d in sorted(dirs)]

        for filename in files:
            if filename in {'.DS_Store', 'Thumbs.db', 'desktop.ini'}:
                continue

            if (not unlimited) and (file_count >= max_files):
                break

            file_path = os.path.join(root, filename)
            try:
                stat_info = os.stat(file_path)
                file_size = stat_info.st_size
                mod_time = datetime.fromtimestamp(stat_info.st_mtime)

                path_obj = Path(filename)
                ext = path_obj.suffix.lower().lstrip('.')
                stem = path_obj.stem

                file_type = get_file_type(ext)
                file_types[file_type] += 1
                extensions[ext] += 1
                total_size += file_size

                file_date = mod_time.strftime('%Y-%m-%d')
                dates.append(file_date)

                year = extract_year_from_name(stem)
                if year:
                    years.append(year)

                track_match = re.match(r'^(\d{1,3})', stem)
                track_number = int(track_match.group(1)) if track_match else None
                if track_number:
                    track_numbers.append(track_number)

                is_cover = any(kw in stem.lower() for kw in ['cover', 'folder', 'front', 'album', 'artwork'])
                rel_path = os.path.relpath(file_path, folder_path)
                metadata = extract_metadata(file_path, file_type, is_cover)

                files_data.append({
                    "name": filename,
                    "path": rel_path,
                    "extension": ext,
                    "type": file_type,
                    "size_bytes": file_size,
                    "size_human": format_size(file_size),
                    "year": year,
                    "modification_date": file_date,
                    "track_number": track_number,
                    "is_cover_art": is_cover,
                    "metadata": metadata
                })

                file_count += 1

            except (OSError, IOError) as e:
                print(f"Warning: Could not process {file_path}: {e}", file=sys.stderr)
                continue

        if (not unlimited) and (file_count >= max_files):
            break

    max_type_count = max(file_types.values()) if file_types else 0
    coherence = round(max_type_count / file_count, 2) if file_count > 0 else 0.0
    dominant_type = max(file_types, key=file_types.get) if file_types else "other"
    dominant_ext = max(extensions, key=extensions.get) if extensions else "none"

    has_track_numbers = len(track_numbers) > 0
    is_sequential = False
    if len(track_numbers) >= 3:
        sorted_tracks = sorted(set(track_numbers))
        is_sequential = all(sorted_tracks[i] + 1 == sorted_tracks[i + 1]
                            for i in range(min(5, len(sorted_tracks) - 1)))

    return {
        "files": files_data,
        "summary": {
            "file_count": file_count,
            "total_size_bytes": total_size,
            "total_size_human": format_size(total_size),
            "has_subfolders": len(subfolder_names) > 0,
            "subfolder_names": subfolder_names[:10],
            "bundle_coherence_score": coherence,
            "dominant_category": dominant_type,
            "dominant_extension": dominant_ext,
            "file_type_distribution": dict(file_types),
            "extension_distribution": dict(extensions),
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None
            },
            "year_range": {
                "earliest": min(years) if years else None,
                "latest": max(years) if years else None
            },
            "has_track_numbers": has_track_numbers,
            "is_sequential_tracks": is_sequential,
            "track_count": len(track_numbers)
        },
        "context": parent_context
    }

def analyze_file_context(file_path, inbox_dir):
    """Single file analysis with context awareness"""
    try:
        stat_info = os.stat(file_path)
        file_size = stat_info.st_size
        mod_time = datetime.fromtimestamp(stat_info.st_mtime)
        
        path_obj = Path(file_path)
        filename = path_obj.name
        ext = path_obj.suffix.lower().lstrip('.')
        stem = path_obj.stem
        
        file_type = get_file_type(ext)
        year = extract_year_from_name(stem)
        file_date = mod_time.strftime('%Y-%m-%d')
        
        is_cover = any(kw in stem.lower() for kw in ['cover', 'folder', 'front', 'album', 'artwork'])
        
        metadata = extract_metadata(file_path, file_type, is_cover)
        
        # Analyze parent context
        parent_context = analyze_parent_context(file_path, inbox_dir)
        
        return {
            "files": [{
                "name": filename,
                "path": filename,
                "extension": ext,
                "type": file_type,
                "size_bytes": file_size,
                "size_human": format_size(file_size),
                "year": year,
                "modification_date": file_date,
                "track_number": None,
                "is_cover_art": is_cover,
                "metadata": metadata
            }],
            "summary": {
                "file_count": 1,
                "total_size_bytes": file_size,
                "total_size_human": format_size(file_size),
                "has_subfolders": False,
                "subfolder_names": [],
                "bundle_coherence_score": 1.0,
                "dominant_category": file_type,
                "dominant_extension": ext,
                "file_type_distribution": {file_type: 1},
                "extension_distribution": {ext: 1},
                "date_range": {
                    "earliest": file_date,
                    "latest": file_date
                },
                "year_range": {
                    "earliest": year,
                    "latest": year
                },
                "has_track_numbers": False,
                "is_sequential_tracks": False,
                "track_count": 0
            },
            "context": parent_context
        }
    except Exception as e:
        raise

def analyze_parent_context(item_path, inbox_dir):
    """Analyze parent folders to understand context - SIMPLIFIED VERSION"""
    context = {
        "parent_folders": [],
        "likely_bundle_type": None,
        "content_hints": [],
        "path_depth": 0,
        "parent_bundle_name": None
    }
    
    try:
        # Get relative path from inbox
        rel_path = os.path.relpath(item_path, inbox_dir)
        path_parts = rel_path.split(os.sep)
        context["path_depth"] = len(path_parts)
        
        # Analyze parent folders for context clues
        for i, part in enumerate(path_parts[:-1]):  # Exclude current item
            hints = extract_folder_hints(part)
            context["parent_folders"].append({
                "name": part,
                "level": i + 1,
                "hints": hints
            })
            
            # Set parent bundle name from the top-level folder
            if i == 0 and any(hint in hints for hint in ['music_content', 'video_content', 'known_artist']):
                context["parent_bundle_name"] = sanitize_folder_name(part)
        
        # Determine likely bundle type from context
        context["likely_bundle_type"] = infer_bundle_from_context(context)
        context["content_hints"] = extract_content_hints(context)
        
    except Exception as e:
        print(f"Warning: Could not analyze context for {item_path}: {e}", file=sys.stderr)
    
    return context

def sanitize_folder_name(name):
    """Sanitize folder name for use in paths"""
    import re
    # Remove common junk tags
    name = re.sub(r'\[\w+\.\w+\]', '', name)
    name = re.sub(r'Beats⭐', '', name)
    name = re.sub(r'\[?WEBRip\]?', '', name, flags=re.I)
    name = re.sub(r'\[?BluRay\]?', '', name, flags=re.I)
    name = re.sub(r'\[?DVD\d?\]?', '', name, flags=re.I)
    name = re.sub(r'[^\w\s-]', ' ', name)
    name = re.sub(r'[\s-]+', ' ', name)
    name = name.replace(' ', '_')
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name if name else 'unnamed'

def extract_folder_hints(folder_name):
    """Extract hints from folder names"""
    hints = []
    name_lower = folder_name.lower()
    
    # Music/Video hints
    if any(keyword in name_lower for keyword in ['album', 'music', 'song', 'track', 'mp3', 'flac']):
        hints.append("music_content")
    if any(keyword in name_lower for keyword in ['movie', 'film', 'video', 'dvd', 'bluray', 'mp4', 'mkv']):
        hints.append("video_content")
    if any(keyword in name_lower for keyword in ['concert', 'live', 'performance', 'tour']):
        hints.append("live_content")
    if any(keyword in name_lower for keyword in ['screenshot', 'screen shot']):
        hints.append("screenshot_content")
    if any(keyword in name_lower for keyword in ['cover', 'artwork', 'poster']):
        hints.append("cover_art")
    if any(keyword in name_lower for keyword in ['doc', 'document', 'pdf', 'ebook']):
        hints.append("document_content")
    if any(keyword in name_lower for keyword in ['photo', 'image', 'picture', 'jpg', 'png']):
        hints.append("image_content")
    
    # Artist/band detection
    if re.search(r'\b(queen|beatles|rolling stones|led zeppelin|madonna|beyonce|taylor swift)\b', name_lower, re.I):
        hints.append("known_artist")
    
    # Year detection
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', folder_name)
    if year_match:
        hints.append(f"year_{year_match.group(0)}")
    
    return hints

def infer_bundle_from_context(context):
    """Infer bundle type from folder context - SIMPLIFIED"""
    parent_hints = []
    for folder in context["parent_folders"]:
        parent_hints.extend(folder["hints"])
    
    # Context-based inference - SIMPLIFIED
    if "music_content" in parent_hints or "known_artist" in parent_hints:
        if "video_content" in parent_hints or "live_content" in parent_hints:
            return "MusicVideo"
        else:
            return "MusicAlbum"
    elif "video_content" in parent_hints:
        if any(keyword in str(context["parent_folders"]).lower() for keyword in ['season', 'episode', 'tv', 'show']):
            return "TVShow"
        else:
            return "VideoBundle"
    elif "screenshot_content" in parent_hints:
        return "Screenshot"
    elif "image_content" in parent_hints and "cover_art" not in parent_hints:
        return "PhotoAlbum"
    elif "document_content" in parent_hints:
        return "DocumentSet"
    
    return None

def extract_content_hints(context):
    """Extract content hints from context"""
    hints = []
    all_hints = []
    
    for folder in context["parent_folders"]:
        all_hints.extend(folder["hints"])
    
    # Music genre hints
    music_keywords = ['rock', 'pop', 'jazz', 'classical', 'hiphop', 'rap', 'country', 'electronic', 'rnb', 'reggae']
    for keyword in music_keywords:
        if any(keyword in hint.lower() for hint in all_hints):
            hints.append(f"genre_{keyword}")
    
    # Content type hints
    if "live_content" in all_hints:
        hints.append("live_performance")
    if "cover_art" in all_hints:
        hints.append("is_cover_art")
    if "screenshot_content" in all_hints:
        hints.append("is_screenshot")
    
    return hints

def format_size(bytes_size):
    """Human-readable file size"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"

if __name__ == "__main__":
    item_path = sys.argv[1]
    output_file = sys.argv[2]
    max_files = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    inbox_dir = sys.argv[4] if len(sys.argv) > 4 else "/data/inbox"
    
    try:
        if os.path.isdir(item_path):
            result = analyze_folder_context(item_path, max_files, inbox_dir)
        else:
            result = analyze_file_context(item_path, inbox_dir)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
    except Exception as e:
        print(f"Analysis error: {e}", file=sys.stderr)
        sys.exit(1)
PYTHON_ANALYZE
}

# ============================================================
# Enhanced AI Prompt with Context Awareness
# ============================================================

create_ai_prompt() {
    local item="$1"
    local is_folder="$2"
    local analysis_json="$3"
    local output_file="$4"
    
    cat > "$output_file" << EOF
You are a file organization AI. Classify this item and return ONLY valid JSON.

CRITICAL CONTEXT-AWARE RULES:
1. FOLDER CONTEXT OVERRIDES FILE TYPE: Files inherit their parent folder's classification
2. SCREENSHOTS IN MEDIA FOLDERS: Screenshot images inside music/video folders stay with parent content
3. SUPPORTING FILES: All files in subfolders (screens, covers, subtitles) belong to parent bundle
4. DEEP HIERARCHY: Files deep in folder structures inherit parent's classification

ITEM: $(basename "$item")
TYPE: $( [[ "$is_folder" == "true" ]] && echo "FOLDER" || echo "FILE" )
FULL PATH: $item

ANALYSIS:
$(cat "$analysis_json")

LIBRARY STRUCTURE:
RAM/:
  - Music/Genre/Albums/Artist_Album/ → music albums with tracks
  - Music/Genre/Singles/ → single tracks
  - MusicVideos/Genre/Artist_Video/ → music videos with screenshots in /screens/
  - Movies/Genre/Movie_Name/ → movies with screenshots in /screens/
  - Shows/Genre/Show_Name/ → TV shows
  - Games/Platform/Game_Name/ → game files
  - Tutorials/ → course materials
  - Software/OS/UseCase/ → applications
  - 3dModels/Projects/ → 3D model projects
  - Misc/Unsorted/ → unclassified

ROM/:
  - Photos/Subcategory/ → photo albums (Travel, Events, Personal, Nature)
  - Images/Screenshots/ → ONLY standalone screenshots (not from media)
  - Images/Wallpapers/ → wallpapers
  - Documents/Sets/ → document collections
  - Archives/ → compressed backups
  - Backups/ → system backups
  - Private/ → sensitive files
  - Tags/ProjectName/ → tagged projects (#tag)
  - Misc/Code/ → source code
  - Misc/Configs/ → configuration files

BUNDLE TYPES:
- MusicAlbum: Audio files with track numbers
- MusicVideo: Video music content (concerts, music videos)
- VideoBundle: Movies/films
- TVShow: TV series episodes
- Screenshot: Screenshots FROM media content (stay with parent)
- PhotoAlbum: Standalone photo collections
- DocumentSet: Document collections
- ModelBundle: 3D model files
- Game: Game ROMs/files
- Tutorial: Educational content
- Standalone: Single file with no context
- MixedBundle: Mixed content

CONTEXT-AWARE CLASSIFICATION EXAMPLES:
- "Queen - DVD5/screens/vlcsnap.png" → Bundle: Screenshot, Path: /RAM/MusicVideos/Rock/Queen_DVD5/screens/
- "Music Album/Cover.jpg" → Bundle: MusicAlbum, Path: /RAM/Music/Rock/Album_Name/ (as cover art)
- "Concert Video/backstage.jpg" → Bundle: MusicVideo, Path: /RAM/MusicVideos/Rock/Concert_Name/Extras/
- "Movie/screenshots/shot.png" → Bundle: Screenshot, Path: /RAM/Movies/Action/Movie_Name/screenshots/

SPECIAL CONTEXT RULES:
- Files in 'screens', 'screenshots', 'covers', 'subtitles' folders → INHERIT PARENT BUNDLE
- Images with 'vlcsnap', 'screenshot' in name → Screenshot bundle, stay with parent
- Files 2+ levels deep in music/video folders → INHERIT PARENT BUNDLE
- Supporting files (nfo, txt, md5, ifo, vob) → Stay with main content
- NEVER move screenshots from media folders to /ROM/Images/Screenshots/

RESPOND WITH ONLY JSON:
{
  "bundle_type": (one of: MusicAlbum, MusicVideo, VideoBundle, TVShow, Screenshot, PhotoAlbum, DocumentSet, ModelBundle, Game, Tutorial, Standalone, MixedBundle),
  "suggested_name": "Parent_Folder_Name_or_File_Name",
  "recommended_path": "/data/library/RAM/.../Parent_Folder_Name/Subfolder/",
  "confidence": 0.00 to 1.0,
  "reasoning": "Detailed explanation including context analysis",
  "tags": ["genre", "artist", "live", "official", ...],
  "category": (one of: Music, Video, MusicVideo, Photo, Document, Model, Game, Software, Archive, Code, Other, Tutorial),
  "storage_zone": "RAM" or "ROM",
  "genre": "Rock" or "Pop" or "Action" or "Programming" or "General" or ...,
  "subcategory": "Events" or "Personal" or "Nature" or "Books" or ...,
  "os": "windows" or "macos" or "linux" or "android" or "ios" or "unknown",
  "usecase": "gaming" or "productivity" or "development" or "entertainment" or "unsorted" or ...,
  "platform": "PC" or "Mac" or "Linux" or "Android" or "iOS" or "Nintendo" or "PlayStation" or ...,
  "video_context": "concert" or "music_video" or "live" or "movie" or ...,
  "subfolder_plan": {
    "enabled": true,
    "map": {"Image": "screens"},
    "reasoning": "Screenshots go in screens subfolder"
  },
  "actions": {
    "move": true,
    "rename": false,
    "extract_year": false,
    "create_subfolders": true,
    "generate_tags": false,
    "verify_duplicates": false,
    "preserve_structure": true,
    "flatten_hierarchy": false
  },
  "warnings": [],
  "recommendations": [],
  "processing_notes": {
    "special_handling": "None",
    "estimated_time_seconds": 5,
    "risk_level": "low"
  },
  "bundle_coherence_score": 1.0,
  "metadata": {
    "year": (extracted year or "null"),
    "file_count": integer,
    "dominant_category": "video" or "audio" or "image" or "document" or ...,
    "dominant_extension": "dmg" or "mp4" or "mp3" or ...,
    "file_type_distribution": {
      "type1": count1,
      "type2": count2, ...
    },
    "size_total": "KB/MB/GB/etc.",
    "has_subfolders": true | false,
    "subfolder_names": ["subfolder1", "subfolder2", ...],
    "contains_sensitive_data": false,
    "detected_language": "English" or "Spanish" or "None" or ...
  },
  "files": [
    {
      "original_path": "/data/inbox/path/to/file.extension",
      "original_name": "file.extension",
      "category": "Image" or "Audio" or "Video" or "Document" or "Model" or "Game" or "Other",
      "rename_to": "file.extension",
      "recommended_path": "/data/library/RAM/.../Parent_Folder/screens/",
      "track_number": null or integer,
      "file_size": "KB/MB/GB/etc.",
      "file_extension": "extension",
      "keep_original": false,
      "needs_processing": false,
      "metadata": {}
    }
  ],
  "related_items": [],
  "source_path": "/data/inbox/path/to/item",
  "is_folder": false
}
EOF
}

# ============================================================
# Enhanced Merge Function with Context Awareness - IMPROVED
# ============================================================

merge_with_python() {
    local analysis_file="$1"
    local bundle_file="$2"
    local item="$3"
    local is_folder="$4"
    local output_file="$5"
    local project_tag="${6:-}"
    
    python3 - "$analysis_file" "$bundle_file" "$item" "$is_folder" "$output_file" "$project_tag" <<'PYTHON_MERGE'
import sys
import json
import os
import re
from pathlib import Path

analysis_file = sys.argv[1]
bundle_file = sys.argv[2]
item = sys.argv[3]
is_folder = sys.argv[4] == 'true'
output_file = sys.argv[5]
project_tag = sys.argv[6] if len(sys.argv) > 6 else ""

with open(analysis_file, 'r') as f:
    analysis = json.load(f)

with open(bundle_file, 'r') as f:
    bundle = json.load(f)

def sanitize_name(name):
    name = re.sub(r'\[\w+\.\w+\]', '', name)
    name = re.sub(r'Beats⭐', '', name)
    name = re.sub(r'\[?WEBRip\]?', '', name, flags=re.I)
    name = re.sub(r'\[?BluRay\]?', '', name, flags=re.I)
    name = re.sub(r'\[?DVD\d?\]?', '', name, flags=re.I)
    name = re.sub(r'[^\w\s-]', ' ', name)
    name = re.sub(r'[\s-]+', ' ', name)
    name = name.replace(' ', '_')
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name if name else 'unnamed'

# Apply context-aware overrides
context = analysis.get('context', {})
parent_folders = context.get('parent_folders', [])
likely_bundle = context.get('likely_bundle_type')
content_hints = context.get('content_hints', [])
parent_bundle_name = context.get('parent_bundle_name')

# Context-based bundle type override
if likely_bundle and bundle.get('bundle_type') in ['PhotoAlbum', 'Standalone']:
    # Override if context suggests different bundle type
    if 'is_screenshot' in content_hints:
        bundle['bundle_type'] = 'Screenshot'
        bundle['category'] = 'Image'
    elif likely_bundle in ['MusicVideo', 'VideoBundle'] and bundle['bundle_type'] == 'PhotoAlbum':
        bundle['bundle_type'] = 'Screenshot'
        bundle['category'] = 'Image'

# Use parent bundle name for suggested name if available
if parent_bundle_name and bundle.get('bundle_type') in ['Screenshot', 'MusicVideo', 'VideoBundle', 'MusicAlbum']:
    bundle['suggested_name'] = parent_bundle_name
else:
    bundle['suggested_name'] = sanitize_name(bundle.get('suggested_name', 'Unknown'))

bundle['bundle_coherence_score'] = analysis['summary']['bundle_coherence_score']

year = analysis['summary']['year_range']['latest'] if analysis['summary']['year_range']['latest'] else "null"
detected_language = "English" if analysis['summary']['dominant_category'] in ['audio', 'document'] else "None"

bundle['metadata'] = {
    "year": year,
    "file_count": analysis['summary']['file_count'],
    "dominant_category": analysis['summary']['dominant_category'],
    "dominant_extension": analysis['summary']['dominant_extension'],
    "file_type_distribution": analysis['summary']['file_type_distribution'],
    "size_total": analysis['summary']['total_size_human'],
    "has_subfolders": analysis['summary']['has_subfolders'],
    "subfolder_names": analysis['summary']['subfolder_names'],
    "contains_sensitive_data": False,
    "detected_language": detected_language,
    "context_analysis": context
}

zone_map = {
    'PhotoAlbum': 'ROM', 'Screenshot': 'RAM', 'DocumentSet': 'ROM', 'ModelBundle': 'RAM',
    'MusicAlbum': 'RAM', 'MusicVideo': 'RAM', 'Karaoke': 'RAM', 'VideoBundle': 'RAM',
    'TVShow': 'RAM', 'Game': 'RAM', 'Tutorial': 'RAM',
    'Standalone': 'RAM', 'MixedBundle': 'RAM'
}
bundle['storage_zone'] = zone_map.get(bundle['bundle_type'], bundle.get('storage_zone', 'RAM'))

os_ = bundle.get('os', 'unknown')
if os_ == 'unknown' or os_ is None:
    ext = analysis['summary']['dominant_extension']
    if ext == 'dmg':
        os_ = 'macos'
    elif ext == 'exe':
        os_ = 'windows'
    elif ext == 'deb':
        os_ = 'linux'
    elif ext == 'apk':
        os_ = 'android'
    bundle['os'] = os_

usecase = bundle.get('usecase', 'unsorted')
genre = bundle.get('genre', 'Rock') if bundle.get('genre') else 'Rock'
subcategory = bundle.get('subcategory', 'General') if bundle.get('subcategory') else 'General'

bundle_type = bundle['bundle_type']
storage_zone = bundle['storage_zone']
suggested_name = bundle['suggested_name']

# IMPROVED Context-aware path generation
if bundle_type == 'MusicAlbum':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Music/{genre}/Albums/{suggested_name}/"
elif bundle_type == 'MusicVideo':
    # Music videos with their content
    bundle['recommended_path'] = f"/data/library/{storage_zone}/MusicVideos/{genre}/{suggested_name}/"
elif bundle_type == 'VideoBundle':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Movies/{genre}/{suggested_name}/"
elif bundle_type == 'TVShow':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Shows/{genre}/{suggested_name}/"
elif bundle_type == 'Screenshot':
    # Screenshots stay with their parent content
    if parent_folders:
        # Find the appropriate parent content type
        for folder in parent_folders:
            if any(hint in folder.get('hints', []) for hint in ['music_content', 'known_artist']):
                bundle['recommended_path'] = f"/data/library/{storage_zone}/MusicVideos/{genre}/{parent_bundle_name or suggested_name}/"
                break
            elif 'video_content' in folder.get('hints', []):
                bundle['recommended_path'] = f"/data/library/{storage_zone}/Movies/{genre}/{parent_bundle_name or suggested_name}/"
                break
        else:
            # Default screenshots location (should rarely happen)
            bundle['recommended_path'] = f"/data/library/{storage_zone}/Images/Screenshots/"
    else:
        bundle['recommended_path'] = f"/data/library/{storage_zone}/Images/Screenshots/"
elif bundle_type == 'PhotoAlbum':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Photos/{subcategory}/{suggested_name}/"
elif bundle_type == 'ModelBundle':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/3dModels/Projects/{suggested_name}/"
elif bundle_type == 'DocumentSet':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Documents/{subcategory}/{suggested_name}/"
elif bundle_type == 'Tutorial':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Tutorials/{genre}/{suggested_name}/"
elif bundle_type == 'Game':
    platform = bundle.get('platform', 'PC')
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Games/{platform}/{suggested_name}/"
elif bundle.get('category') == 'Software' or (bundle_type == 'Standalone' and 'software' in bundle.get('tags', [])):
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Software/{os_}/{usecase}/{suggested_name}/"
elif bundle.get('category') == 'Archive':
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Archives/{suggested_name}/"
elif bundle.get('category') == 'Code':
    bundle['recommended_path'] = f"/data/library/ROM/Misc/Code/{suggested_name}/"
else:
    bundle['recommended_path'] = f"/data/library/{storage_zone}/Misc/Unsorted/{suggested_name}/"

if not bundle['recommended_path'].endswith('/'):
    bundle['recommended_path'] += '/'

files = []
for f in analysis['files']:
    file_category = f['type'].capitalize()
    ext = f['extension']
    name = f['name']
    stem = Path(name).stem
    track_number = f['track_number']
    is_cover_art = f['is_cover_art']
    
    if track_number is not None:
        track_match = re.match(r'^(\d{1,3})\s*[-._]?\s*', stem)
        if track_match and int(track_match.group(1)) == track_number:
            stem = stem[track_match.end():]
    
    sanitized_stem = sanitize_name(stem)
    if is_cover_art:
        rename_to = 'cover.' + ext if ext else 'cover'
        keep_original = True
    else:
        if track_number is not None:
            rename_to = f"{track_number:02d}_{sanitized_stem}.{ext}" if ext else f"{track_number:02d}_{sanitized_stem}"
        else:
            rename_to = f"{sanitized_stem}.{ext}" if ext else sanitized_stem
        keep_original = False
    
    if bundle_type == 'Standalone' and len(analysis['files']) == 1:
        rename_to = bundle['suggested_name'] + '.' + ext if ext else bundle['suggested_name']
    
    recommended_path = bundle['recommended_path']
    
    # IMPROVED Context-aware file placement
    if bundle_type in ['MusicVideo', 'VideoBundle', 'TVShow'] and file_category == 'Image':
        if 'screenshot' in name.lower() or 'vlcsnap' in name.lower():
            recommended_path += 'screens/'
        elif is_cover_art or any(kw in name.lower() for kw in ['cover', 'folder', 'poster']):
            recommended_path += 'covers/'
    
    # For screenshots, always put them in screens subfolder
    if bundle_type == 'Screenshot' and file_category == 'Image':
        recommended_path += 'screens/'
    
    if bundle.get('subfolder_plan', {}).get('enabled', False):
        subfolder = bundle['subfolder_plan'].get('map', {}).get(file_category, '')
        if bundle_type == 'MusicAlbum' and file_category == 'Image':
            subfolder = 'covers'
        if subfolder and not (
            (file_category == 'Audio' and bundle_type in ['MusicAlbum', 'Karaoke']) or
            (file_category == 'Video' and bundle_type in ['VideoBundle', 'MusicVideo', 'TVShow'])
        ):
            recommended_path += subfolder + '/'
    
    file_entry = {
        "original_path": os.path.join(item, f['path']),
        "original_name": name,
        "category": file_category,
        "rename_to": rename_to,
        "recommended_path": recommended_path,
        "track_number": track_number,
        "file_size": f['size_human'],
        "file_extension": ext,
        "keep_original": keep_original,
        "needs_processing": False,
        "metadata": f['metadata']
    }
    files.append(file_entry)

bundle['files'] = files

recommendations = bundle.get('recommendations', [])
if bundle_type == 'MusicAlbum' and 'image' not in analysis['summary']['file_type_distribution']:
    recommendations.append("Download cover art from internet")
if bundle_type in ['VideoBundle', 'TVShow'] and 'subtitle' not in analysis['summary']['file_type_distribution']:
    recommendations.append("Download subtitles from internet")
if year == "null":
    recommendations.append("Extract year from internet based on file name")
bundle['recommendations'] = recommendations

bundle.setdefault('related_items', [])
bundle.setdefault('warnings', [])
bundle.setdefault('processing_notes', {
    "special_handling": "None",
    "estimated_time_seconds": 5,
    "risk_level": "low"
})
if 'actions' not in bundle:
    bundle['actions'] = {}
bundle['actions'].setdefault('extract_year', bool(year != "null"))

if project_tag:
    project_path = f"/data/library/ROM/Tags/{project_tag}/"
    bundle['recommended_path'] = project_path
    if 'tags' not in bundle:
        bundle['tags'] = []
    bundle['tags'].append(f"project:{project_tag}")
    bundle['warnings'].append(f"All files routed to project: {project_tag}")
    for file_entry in bundle['files']:
        file_entry['recommended_path'] = project_path

with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(bundle, f, ensure_ascii=False, indent=2)
PYTHON_MERGE
}

# ============================================================
# AI Interaction Functions (keep existing ones)
# ============================================================

call_ollama_ai() {
    local prompt_file="$1"
    local output_file="$2"
    local model="${3:-$OLLAMA_MODEL_PRIMARY}"
    
    curl -sS --max-time "$AI_TIMEOUT" "$OLLAMA_HOST/api/generate" \
        -H "Content-Type: application/json" \
        -d @- <<EOF 2>/dev/null > "$output_file"
{
  "model": "$model",
  "prompt": $(jq -Rs . < "$prompt_file"),
  "stream": false,
  "options": {
    "temperature": 0.0,
    "top_p": 0.9,
    "num_ctx": 8192
  }
}
EOF
}

call_openai_ai() {
    local prompt_file="$1"
    local output_file="$2"
    
    if [[ -z "$OPENAI_API_KEY" ]]; then
        return 1
    fi
    
    curl -sS --max-time "$AI_TIMEOUT" https://api.openai.com/v1/chat/completions \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $OPENAI_API_KEY" \
        -d @- <<EOF > "$output_file" 2>/dev/null
{
  "model": "$OPENAI_MODEL",
  "messages": [{"role": "user", "content": $(jq -Rs . < "$prompt_file")}],
  "temperature": 0.0
}
EOF
}

call_anthropic_ai() {
    local prompt_file="$1"
    local output_file="$2"
    
    if [[ -z "$ANTHROPIC_API_KEY" ]]; then
        return 1
    fi
    
    curl -sS --max-time "$AI_TIMEOUT" https://api.anthropic.com/v1/messages \
        -H "Content-Type: application/json" \
        -H "x-api-key: $ANTHROPIC_API_KEY" \
        -H "anthropic-version: 2023-06-01" \
        -d @- <<EOF > "$output_file" 2>/dev/null
{
  "model": "$ANTHROPIC_MODEL",
  "max_tokens": 4096,
  "temperature": 0.0,
  "messages": [{"role": "user", "content": $(jq -Rs . < "$prompt_file")}]
}
EOF
}

extract_json_from_response() {
    local response_file="$1"
    local output_file="$2"
    
    jq -r '.response' "$response_file" 2>/dev/null | \
        sed -E '1s/^```(json)?[[:space:]]*//; $s/[[:space:]]*```$//' > "$output_file"
}

extract_openai_response() {
    local response_file="$1"
    local output_file="$2"
    
    jq -r '.choices[0].message.content' "$response_file" 2>/dev/null | \
        sed -E '1s/^```(json)?[[:space:]]*//; $s/[[:space:]]*```$//' > "$output_file"
}

extract_anthropic_response() {
    local response_file="$1"
    local output_file="$2"
    
    jq -r '.content[0].text' "$response_file" 2>/dev/null | \
        sed -E '1s/^```(json)?[[:space:]]*//; $s/[[:space:]]*```$//' > "$output_file"
}

validate_ai_json() {
    local json_file="$1"
    
    jq -e '
        type == "object" and
        has("bundle_type") and
        has("suggested_name") and
        has("recommended_path") and
        (.bundle_type | type == "string") and
        (.suggested_name | type == "string") and
        (.recommended_path | type == "string")
    ' "$json_file" >/dev/null 2>&1
}

# ============================================================
# Fallback Classification
# ============================================================

create_fallback() {
    local item="$1"
    local is_folder="$2"
    
    python3 - "$item" "$is_folder" <<'PYTHON_FALLBACK'
import sys
import json
import os
from pathlib import Path
import re

item = sys.argv[1]
is_folder = sys.argv[2] == "true"

base_name = os.path.basename(item)
ext = Path(item).suffix.lower().lstrip('.') if not is_folder else ''

type_routes = {
    'mp3': ('MusicAlbum', 'Audio', '/data/library/RAM/Music/Unknown/Albums/', 'Unknown'),
    'flac': ('MusicAlbum', 'Audio', '/data/library/RAM/Music/Unknown/Albums/', 'Unknown'),
    'mp4': ('VideoBundle', 'Video', '/data/library/RAM/Movies/Unknown/', 'Unknown'),
    'mkv': ('VideoBundle', 'Video', '/data/library/RAM/Movies/Unknown/', 'Unknown'),
    'jpg': ('PhotoAlbum', 'Image', '/data/library/ROM/Photos/Unsorted/', None),
    'png': ('PhotoAlbum', 'Image', '/data/library/ROM/Photos/Unsorted/', None),
    'pdf': ('DocumentSet', 'Document', '/data/library/ROM/Documents/Sets/', None),
    'stl': ('ModelBundle', 'Model', '/data/library/RAM/3dModels/Projects/', None),
    'gcode': ('ModelBundle', 'Print', '/data/library/RAM/3dModels/GCode/', None),
    'dmg': ('Standalone', 'Software', '/data/library/RAM/Software/macos/unsorted/', None),
    'exe': ('Standalone', 'Software', '/data/library/RAM/Software/windows/unsorted/', None),
    'iso': ('Standalone', 'DiskImage', '/data/library/RAM/Misc/DiskImages/', None),
    'zip': ('Standalone', 'Archive', '/data/library/ROM/Archives/', None),
    'rar': ('Standalone', 'Archive', '/data/library/ROM/Archives/', None),
}

if is_folder:
    bundle_type = 'MixedBundle'
    category = 'Other'
    path = '/data/library/RAM/Misc/Unsorted/'
    genre = 'Unknown'
    os_ = 'unknown'
    usecase = 'unsorted'
else:
    bundle_type, category, path, genre = type_routes.get(ext, ('Standalone', 'Other', '/data/library/RAM/Misc/Unsorted/', None))
    os_ = 'macos' if ext == 'dmg' else 'windows' if ext == 'exe' else 'linux' if ext == 'deb' else 'unknown'
    usecase = 'unsorted'
    if category == 'Software':
        path = f'/data/library/RAM/Software/{os_}/{usecase}/'

clean_stem = Path(base_name).stem
year_match = re.search(r'\b(19\d{2}|20\d{2})\b', clean_stem)
year = f"_{year_match.group(0)}" if year_match else ''
title = re.sub(r'\.\d{4}.*', '', clean_stem).replace('.', ' ').strip()
clean_name = '_'.join(word.capitalize() for word in title.split())
clean_name += year
clean_name = re.sub(r'[^\w]', '_', clean_name)
clean_name = re.sub(r'_+', '_', clean_name).strip('_')

path += clean_name + '/' if is_folder else ''

result = {
    "bundle_type": bundle_type,
    "suggested_name": clean_name,
    "recommended_path": path,
    "confidence": 0.60,
    "reasoning": "Fallback rule-based classification",
    "tags": ["fallback", category.lower()],
    "category": category,
    "storage_zone": 'RAM' if bundle_type in ['MusicAlbum', 'VideoBundle', 'Standalone'] else 'ROM',
    "genre": genre,
    "os": os_,
    "usecase": usecase,
    "subfolder_plan": {"enabled": is_folder, "map": {}, "reasoning": "Fallback"},
    "actions": {
        "move": True,
        "rename": True,
        "extract_year": False,
        "create_subfolders": is_folder,
        "generate_tags": False,
        "verify_duplicates": True,
        "preserve_structure": False,
        "flatten_hierarchy": False
    },
    "warnings": ["AI classification failed, using fallback"],
    "recommendations": ["Manual review recommended"],
    "processing_notes": {
        "special_handling": "Fallback",
        "estimated_time_seconds": 5,
        "risk_level": "medium"
    }
}

print(json.dumps(result, ensure_ascii=False))
PYTHON_FALLBACK
}

# ============================================================
# Genre and Path Normalization - FIXED VERSION
# ============================================================

normalize_genre_and_paths() {
    local in_json="$1"
    local out_json="$2"
    
    python3 - "$in_json" "$out_json" <<'PYTHON_NORMALIZE'
import sys
import json
import re

def firstseg(value):
    """Extract first segment and clean"""
    if isinstance(value, str):
        return value.split("/")[0].split(",")[0].split(";")[0].strip()
    return value

def canon(g):
    """Canonicalize genre - pick only ONE"""
    if not g:
        g = "General"
    x = str(g).lower()
    
    if x in ["r&b", "rnb"]:
        return "RnB"
    elif x in ["hip-hop", "hiphop"]:
        return "HipHop"
    elif x in ["sci-fi", "scifi"]:
        return "SciFi"
    elif x in ["action/comedy", "comedy/action"]:
        return "Comedy"
    elif x in ["rnb/soul", "soul/rnb"]:
        return "RnB"
    elif x in ["", "unknown", "unsorted"]:
        return "General"
    else:
        # Split and take first if multiple
        first_part = x.split("/")[0].split(",")[0].split(";")[0].strip()
        if first_part:
            return first_part[0].upper() + first_part[1:]
        return "General"

def primary_genre(data):
    """Pick primary genre - SINGLE ONLY"""
    if data.get('genre') and isinstance(data['genre'], str) and data['genre'].strip():
        return firstseg(data['genre'])
    elif data.get('tags') and isinstance(data['tags'], list):
        for tag in data['tags']:
            if isinstance(tag, str) and '/' not in tag:
                return firstseg(tag)
    return "General"

def primary_subcategory(data):
    """Pick primary subcategory - SINGLE ONLY"""
    if data.get('subcategory') and isinstance(data['subcategory'], str) and data['subcategory'].strip():
        return firstseg(data['subcategory'])
    return "General"

def normalize_tags(tags):
    """Normalize tags to remove slashes and duplicates"""
    if not isinstance(tags, list):
        return []
    normalized = []
    for tag in tags:
        if isinstance(tag, str):
            if '/' in tag:
                normalized.append(tag.split('/')[0])
            else:
                normalized.append(tag)
    return list(set(normalized))

def main():
    in_file = sys.argv[1]
    out_file = sys.argv[2]
    
    with open(in_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Get normalized values
    genre_raw = primary_genre(data)
    subcategory_raw = primary_subcategory(data)
    
    genre = canon(genre_raw)
    subcategory = canon(subcategory_raw)
    
    # Update the data
    data['genre'] = genre
    data['subcategory'] = subcategory
    data['tags'] = normalize_tags(data.get('tags', []))
    
    # Build paths with SINGLE genre only
    bundle_type = data.get('bundle_type', 'Standalone')
    storage_zone = data.get('storage_zone', 'RAM')
    suggested_name = data.get('suggested_name', 'Unknown')
    
    if bundle_type == "VideoBundle":
        data['recommended_path'] = f"/data/library/{storage_zone}/Movies/{genre}/{suggested_name}/"
    elif bundle_type == "MusicAlbum":
        data['recommended_path'] = f"/data/library/{storage_zone}/Music/{genre}/Albums/{suggested_name}/"
    elif bundle_type == "MusicVideo":
        video_context = data.get('video_context', 'music_video')
        if video_context == "concert":
            data['recommended_path'] = f"/data/library/{storage_zone}/MusicVideos/{genre}/LivePerformances/{suggested_name}/"
        else:
            data['recommended_path'] = f"/data/library/{storage_zone}/MusicVideos/{genre}/Official/{suggested_name}/"
    elif bundle_type == "PhotoAlbum":
        data['recommended_path'] = f"/data/library/{storage_zone}/Photos/{subcategory}/{suggested_name}/"
    elif bundle_type == "DocumentSet":
        data['recommended_path'] = f"/data/library/{storage_zone}/Documents/{subcategory}/{suggested_name}/"
    
    # Update all file paths to use the same single-genre path
    if 'files' in data and isinstance(data['files'], list):
        for file_entry in data['files']:
            if 'recommended_path' in file_entry:
                old_path = file_entry['recommended_path']
                # Simple replacement for genre in path
                new_path = re.sub(r'/([^/]+)/([^/]+)/', f'/{genre}/', old_path, count=1)
                file_entry['recommended_path'] = new_path
    
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
PYTHON_NORMALIZE
}

# ============================================================
# Project Tag Detection
# ============================================================

detect_project_tag() {
    local item_name="$1"
    local tag=""
    
    if [[ "$item_name" =~ \#([a-zA-Z0-9_-]+) ]]; then
        tag="${BASH_REMATCH[1]}"
        echo "$tag"
    fi
}

# ============================================================
# Candidate Collection - IMPROVED DEEP SCANNING
# ============================================================

collect_candidates() {
    local -n candidates_ref=$1
    local -a temp_candidates=()
    local -A processed_items=()
    
    log "Scanning $INBOX_DIR recursively for candidates..."
    
    # First, collect all files and directories that contain files
    while IFS= read -r -d '' item; do
        if [[ -z "$item" ]] || [[ "$item" == "$INBOX_DIR" ]]; then
            continue
        fi
        
        # Skip ignored items
        if should_ignore "$item"; then
            continue
        fi
        
        # Skip if already processed
        if [[ -n "${processed_items[$item]:-}" ]]; then
            continue
        fi
        
        # For files, add directly
        if [[ -f "$item" ]]; then
            temp_candidates+=("$item")
            processed_items["$item"]=1
            continue
        fi
        
        # For directories, check if they contain any non-ignored files
        if [[ -d "$item" ]]; then
            local has_files=false
            
            # Use find to check for non-ignored files in this directory
            while IFS= read -r -d '' file; do
                if ! should_ignore "$file"; then
                    has_files=true
                    break
                fi
            done < <(find "$item" -maxdepth 1 -type f -print0 2>/dev/null)
            
            if [[ "$has_files" == true ]]; then
                temp_candidates+=("$item")
                processed_items["$item"]=1
            fi
        fi
    done < <(find "$INBOX_DIR" -mindepth 1 -print0 2>/dev/null)
    
    # Remove duplicates and sort by depth (process deepest first to avoid conflicts)
    while IFS= read -r candidate; do
        candidates_ref+=("$candidate")
    done < <(printf "%s\n" "${temp_candidates[@]}" | awk '!seen[$0]++' | awk -F/ '{print NF, $0}' | sort -rn | cut -d' ' -f2-)
    
    log "Collected ${#candidates_ref[@]} candidates after filtering"
}

# ============================================================
# Main Classification Loop
# ============================================================

declare -a CANDIDATES=()
collect_candidates CANDIDATES

log "Found ${#CANDIDATES[@]} candidates to classify"
log ""
log "Candidates:"
for item in "${CANDIDATES[@]}"; do
    log "  - ${item#$INBOX_DIR/}"
done
log ""

echo "[" > "$REPORT_FILE"
FIRST_ENTRY=true
PROCESSED=0
FAILED=0

for item in "${CANDIDATES[@]}"; do
    rel_path="${item#$INBOX_DIR/}"
    log "Processing: $rel_path"
    
    # Skip empty directories
    if [[ -d "$item" ]] && ! find "$item" -type f ! -name '.*' ! -name '.DS_Store' ! -name 'Thumbs.db' -print | head -1 | grep -q .; then
        log "  Skipping empty folder"
        continue
    fi
    
    is_folder=false
    [[ -d "$item" ]] && is_folder=true
    
    ANALYSIS_FILE="$TEMP_DIR/analysis_$$_$(date +%s%N).json"
    
    if ! analyze_item_with_python "$item" "$ANALYSIS_FILE"; then
        log "  ⚠️  Analysis failed"
        FAILED=$((FAILED + 1))
        continue
    fi
    
    AI_SUCCESS=false
    BUNDLE_JSON_FILE="$TEMP_DIR/bundle_$$_$(date +%s%N).json"
    FINAL_CONFIDENCE=0.0
    
    declare -a AI_CHAIN=("ollama_primary")
    
    if [[ "$USE_MULTI_AI" == "true" ]]; then
        AI_CHAIN+=("ollama_secondary")
        [[ -n "$OPENAI_API_KEY" ]] && AI_CHAIN+=("openai")
        [[ -n "$ANTHROPIC_API_KEY" ]] && AI_CHAIN+=("anthropic")
    fi
    
    for provider in "${AI_CHAIN[@]}"; do
        PROMPT_FILE="$TEMP_DIR/prompt_$$_$(date +%s%N).txt"
        RAW_RESPONSE="$TEMP_DIR/raw_$$_$(date +%s%N).json"
        CLEAN_JSON="$TEMP_DIR/clean_$$_$(date +%s%N).json"
        
        create_ai_prompt "$item" "$is_folder" "$ANALYSIS_FILE" "$PROMPT_FILE"
        
        case "$provider" in
            ollama_primary)
                log "  🤖 Trying Ollama ($OLLAMA_MODEL_PRIMARY)"
                call_ollama_ai "$PROMPT_FILE" "$RAW_RESPONSE" "$OLLAMA_MODEL_PRIMARY" && \
                    extract_json_from_response "$RAW_RESPONSE" "$CLEAN_JSON"
                ;;
            ollama_secondary)
                log "  🤖 Trying Ollama secondary ($OLLAMA_MODEL_SECONDARY)"
                call_ollama_ai "$PROMPT_FILE" "$RAW_RESPONSE" "$OLLAMA_MODEL_SECONDARY" && \
                    extract_json_from_response "$RAW_RESPONSE" "$CLEAN_JSON"
                ;;
            openai)
                log "  🌐 Trying OpenAI ($OPENAI_MODEL)"
                call_openai_ai "$PROMPT_FILE" "$RAW_RESPONSE" && \
                    extract_openai_response "$RAW_RESPONSE" "$CLEAN_JSON"
                ;;
            anthropic)
                log "  🌐 Trying Anthropic ($ANTHROPIC_MODEL)"
                call_anthropic_ai "$PROMPT_FILE" "$RAW_RESPONSE" && \
                    extract_anthropic_response "$RAW_RESPONSE" "$CLEAN_JSON"
                ;;
        esac
        
        if validate_ai_json "$CLEAN_JSON"; then
            FINAL_CONFIDENCE=$(jq -r '.confidence // 0.0' "$CLEAN_JSON" 2>/dev/null || echo "0.0")
            
            if (( $(echo "$FINAL_CONFIDENCE >= $CONFIDENCE_THRESHOLD" | bc -l 2>/dev/null || echo "1") )); then
                cp "$CLEAN_JSON" "$BUNDLE_JSON_FILE"
                AI_SUCCESS=true
                log "  ✓ $provider succeeded (confidence: $FINAL_CONFIDENCE)"
                break
            else
                log "  ⚠️  $provider low confidence ($FINAL_CONFIDENCE)"
            fi
        else
            log "  ⚠️  $provider failed validation"
        fi
        
        rm -f "$PROMPT_FILE" "$RAW_RESPONSE" "$CLEAN_JSON" 2>/dev/null || true
        sleep 1
    done
    
    if [[ "$AI_SUCCESS" == false ]]; then
        log "  ⚠️  All AI failed, using fallback"
        FALLBACK_JSON=$(create_fallback "$item" "$is_folder")
        echo "$FALLBACK_JSON" > "$BUNDLE_JSON_FILE"
        FAILED=$((FAILED + 1))
    fi
    
    MERGED_JSON="$TEMP_DIR/merged_$$_$(date +%s%N).json"
    PROJECT_TAG=$(detect_project_tag "$(basename "$item")")
    
    merge_with_python "$ANALYSIS_FILE" "$BUNDLE_JSON_FILE" "$item" "$is_folder" "$MERGED_JSON" "$PROJECT_TAG"
    
    if [[ -n "$PROJECT_TAG" ]]; then
        log "  📌 Project tag detected: #$PROJECT_TAG (all files routed to project folder)"
    fi
    
    # Normalize genre/paths - NOW USING PYTHON VERSION
    NORMALIZED_JSON="$TEMP_DIR/normalized_$$_$(date +%s%N).json"
    normalize_genre_and_paths "$MERGED_JSON" "$NORMALIZED_JSON"
    
    FINAL_JSON=$(jq --arg src "$item" --argjson folder "$is_folder" \
        '. + {source_path: $src, is_folder: $folder}' "$NORMALIZED_JSON")
    
    if [[ "$FIRST_ENTRY" == true ]]; then
        FIRST_ENTRY=false
    else
        echo "," >> "$REPORT_FILE"
    fi
    echo "$FINAL_JSON" >> "$REPORT_FILE"
    
    PROCESSED=$((PROCESSED + 1))
    
    bundle=$(jq -r '.bundle_type' <<< "$FINAL_JSON")
    name=$(jq -r '.suggested_name' <<< "$FINAL_JSON")
    conf=$(jq -r '.confidence' <<< "$FINAL_JSON")
    log "  → $name ($bundle, confidence: $conf)"
    
    rm -f "$ANALYSIS_FILE" "$PROMPT_FILE" "$RAW_RESPONSE" "$CLEAN_JSON" "$BUNDLE_JSON_FILE" "$MERGED_JSON" "$NORMALIZED_JSON" 2>/dev/null || true
done

echo "]" >> "$REPORT_FILE"

log "============================================================"
log "Classification Complete"
log "Processed: $PROCESSED items"
log "Failed: $FAILED items"
log "Report: $REPORT_FILE"

if command -v jq >/dev/null 2>&1; then
    log ""
    log "Classification Summary:"
    jq -r '
        group_by(.bundle_type) |
        map({type: .[0].bundle_type, count: length}) |
        .[] |
        "  \(.type): \(.count)"
    ' "$REPORT_FILE" 2>/dev/null | tee -a "$LOG_FILE" || true
    
    AVG_CONF=$(jq '[.[].confidence] | add / length' "$REPORT_FILE" 2>/dev/null || echo "0")
    log "Average Confidence: $AVG_CONF"
fi

log "============================================================"
log "Log file: $LOG_FILE"

cleanup_temp

exit 0