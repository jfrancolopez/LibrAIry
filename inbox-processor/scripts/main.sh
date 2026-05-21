# Master runner script - /workspace/inbox-processor/scripts/main.sh
#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Starting complete AI-powered inbox processing pipeline..."

echo "📋 Step 1: Duplicate scan"
./step1_scan.sh

echo "🎵 Step 2: Media duplicate quarantine"  
./step2_hash_audio_video.sh

echo "🤖 Step 3: AI classification"
./step3_classify.sh

echo "🧠 Step 3 Analysis: AI-powered quality review" 
./step3_analyze_ai.sh

echo "🧪 Step 4: Dry run simulation"
./step4_dryrun.sh

echo "✅ All steps completed!"