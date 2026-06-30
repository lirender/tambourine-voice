#!/usr/bin/env bash
# Turnkey meeting recorder for the Mac (run from a Terminal window that has
# Screen Recording permission). Captures system audio via ScreenCaptureKit,
# then submits the WAV to the local Tambourine server, which routes to the
# GB10 Parakeet + Sortformer diarization backend and writes a diarized
# transcript next to the recording.
#
# Usage:  ./record_meeting.sh [minutes] [meeting title]
#         ./record_meeting.sh 20 "Standup"
set -euo pipefail

MINUTES="${1:-20}"
TITLE="${2:-Meeting}"
DURATION=$(( MINUTES * 60 ))

REPO="/Users/sarvesh/code/Tambourine"
SCKIT="$REPO/tools/sckit-poc/target/release/sckit-poc"
SERVER="http://localhost:8765"

ts="$(date +%Y%m%d-%H%M%S)"
log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

log "Recording system audio for ${MINUTES} min (title: ${TITLE})..."
log "Stop early with Ctrl-C only if you don't need the recording — it finalizes at the end of the duration."
"$SCKIT" --duration "$DURATION"

# Grab the freshly written WAV (sckit-poc writes /tmp/sckit-poc-<epoch>.wav).
WAV="$(ls -t /tmp/sckit-poc-*.wav 2>/dev/null | head -1)"
if [[ -z "${WAV:-}" || ! -s "$WAV" ]]; then
  echo "ERROR: no WAV produced — Screen Recording permission may be missing for this Terminal." >&2
  exit 1
fi
log "Recorded: $WAV ($(du -h "$WAV" | cut -f1))"

log "Submitting to Tambourine server for Parakeet transcription + diarization..."
JOB="$(curl -s -X POST "$SERVER/api/meeting/transcribe" \
  -H 'Content-Type: application/json' \
  -d "{\"wav_path\":\"$WAV\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')"
log "Job: $JOB"

OUT="$REPO/tools/transcripts/${ts}-$(echo "$TITLE" | tr ' /' '__').md"
mkdir -p "$(dirname "$OUT")"

while true; do
  R="$(curl -s "$SERVER/api/meeting/job/$JOB")"
  ST="$(printf '%s' "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')"
  [[ "$ST" == "complete" || "$ST" == "error" ]] && break
  sleep 3
done

printf '%s' "$R" | python3 - "$OUT" "$TITLE" "$WAV" <<'PY'
import json, sys
out_path, title, wav = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(sys.stdin)
if d["status"] == "error":
    print("Transcription error:", d.get("error")); sys.exit(1)
r = d["result"]
segs, speakers = r["segments"], r.get("speakers", [])
lines = [f"# {title}", "", f"Source: `{wav}`", f"Speakers detected: {', '.join(speakers) or 'none'}", ""]
last = None
for s in segs:
    spk = s.get("speaker") or "unknown"
    name = spk.replace("speaker_", "Speaker ").title()
    ts = s["start_ms"] // 1000
    if spk != last:
        lines.append(f"\n**{name}** [{ts//60:02d}:{ts%60:02d}]: {s['text']}")
        last = spk
    else:
        lines[-1] += " " + s["text"]
open(out_path, "w").write("\n".join(lines))
print("Wrote transcript:", out_path)
print("Speakers:", speakers)
PY
log "Done. Map speaker_N to attendees from the transcript above."
