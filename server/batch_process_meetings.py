#!/usr/bin/env python3
"""Batch process Plaud recordings into Obsidian meeting notes.

Transcribes audio files, summarizes with Ollama, and creates
Obsidian meeting notes linked from daily notes.
"""

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from processors.meeting_summarizer import summarize_transcript
from processors.meeting_transcriber import transcribe_file

AUDIO_DIR = Path("/Users/sarvesh/code/plaud-transcription/audio")
VAULT_DIR = Path("/Users/sarvesh/Obsidian/vault")
MEETING_NOTES_DIR = VAULT_DIR / "Meeting Notes"
DAILY_NOTES_DIR = VAULT_DIR / "Daily Notes"

# Skip patterns
SKIP_SUFFIXES = {"(1)", "(2)"}  # duplicates
MAX_DURATION_HOURS = 5  # skip all-day recordings
MIN_DURATION_SECS = 60  # skip tiny clips


@dataclass
class MeetingFile:
    path: Path
    meeting_date: date | None
    title: str
    project: str
    duration_secs: float = 0.0
    skip: bool = False
    skip_reason: str = ""


def get_project(meeting_date: date | None, title: str) -> str:
    """Map meeting to project based on date and title."""
    if meeting_date is None:
        # Best guess from title
        if "palantir" in title.lower():
            return "project/palantir"
        return "project/gti"

    if meeting_date < date(2026, 1, 12):
        return "project/gti"
    elif meeting_date <= date(2026, 2, 5):
        return "project/gti-onboard"
    else:
        return "project/chatgti"


def parse_date_from_filename(filename: str) -> date | None:
    """Extract date from filename."""
    # Pattern: YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", filename)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # Pattern: MM-DD (assume 2026 for 01-XX, 2025 for 12-XX)
    m = re.match(r"(\d{2})-(\d{2})\s", filename)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = 2025 if month == 12 else 2026
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # Named files with dates in them
    if "2026-01-12" in filename:
        return date(2026, 1, 12)
    if "2026-01-13" in filename or "1-13" in filename:
        return date(2026, 1, 13)

    return None


def parse_title_from_filename(filename: str) -> str:
    """Extract a clean title from filename."""
    name = Path(filename).stem

    # Remove date prefixes
    name = re.sub(r"^\d{4}-\d{2}-\d{2}\s*\d{2}_\d{2}_\d{2}\s*", "", name)
    name = re.sub(r"^\d{2}-\d{2}\s+", "", name)

    # Clean up common patterns
    name = name.replace("_", " ").replace("  ", " ").strip()

    # Remove trailing (1), (2) etc
    name = re.sub(r"\s*\(\d+\)$", "", name)

    # If nothing left (timestamp-only file), return None to signal auto-detect
    if not name or re.match(r"^\d", name):
        return ""

    return name


def get_duration_secs(path: Path) -> float:
    """Get audio duration using ffprobe."""
    import subprocess

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def catalog_files() -> list[MeetingFile]:
    """Build catalog of all audio files."""
    files = []
    already_done = {"01-15 Consultation_ GTI Data Consolidation and AI Optimization.mp3"}

    for path in sorted(AUDIO_DIR.iterdir()):
        if path.name.startswith(".") or path.is_dir():
            continue
        if path.suffix.lower() not in (".mp3", ".ogg", ".wav", ".m4a", ".webm", ".mp4"):
            continue

        name = path.name

        # Check if duplicate
        is_dup = any(s in name for s in ["(1)", "(2)"])
        if is_dup:
            files.append(MeetingFile(path=path, meeting_date=None, title="", project="",
                                      skip=True, skip_reason="duplicate"))
            continue

        # Check if already done
        if name in already_done:
            files.append(MeetingFile(path=path, meeting_date=None, title="", project="",
                                      skip=True, skip_reason="already processed"))
            continue

        meeting_date = parse_date_from_filename(name)
        title = parse_title_from_filename(name)
        duration = get_duration_secs(path)

        # Skip too short
        if duration < MIN_DURATION_SECS:
            files.append(MeetingFile(path=path, meeting_date=meeting_date, title=title,
                                      project="", duration_secs=duration,
                                      skip=True, skip_reason=f"too short ({duration:.0f}s)"))
            continue

        # Skip too long
        if duration > MAX_DURATION_HOURS * 3600:
            files.append(MeetingFile(path=path, meeting_date=meeting_date, title=title,
                                      project="", duration_secs=duration,
                                      skip=True, skip_reason=f"all-day recording ({duration/3600:.1f}h)"))
            continue

        project = get_project(meeting_date, title)

        files.append(MeetingFile(
            path=path, meeting_date=meeting_date, title=title,
            project=project, duration_secs=duration,
        ))

    return files


def auto_detect_title(path: Path) -> str:
    """Transcribe first 2 minutes to detect meeting topic."""
    import subprocess
    import tempfile

    # Extract first 2 min to temp WAV
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        subprocess.run(
            ["ffmpeg", "-i", str(path), "-t", "120", "-ar", "16000", "-ac", "1",
             "-f", "wav", "-y", tmp.name],
            capture_output=True, check=True, timeout=60,
        )
        result = transcribe_file(tmp.name)
        text = result.full_text[:500]

        # Ask the LLM to name this meeting
        import httpx
        resp = httpx.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3",
                "prompt": f"Given this meeting transcript excerpt, suggest a short meeting title (max 8 words, no quotes):\n\n{text}",
                "stream": False,
                "options": {"temperature": 0.3},
            },
            timeout=30,
        )
        title = resp.json().get("response", "").strip().split("\n")[0].strip('"').strip("'")
        return title[:80] if title else "Untitled Meeting"
    except Exception as e:
        logger.warning(f"Auto-detect failed for {path.name}: {e}")
        return "Untitled Meeting"
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def format_transcript(segments: list, speaker_names: dict[str, str] | None = None) -> str:
    """Format segments with 5-minute timestamp markers.

    When segments carry speaker labels (from diarization), render each speaker
    turn on its own line prefixed with the speaker's display name. `speaker_names`
    optionally maps raw labels (e.g. "speaker_0") to real attendee names.
    """
    speaker_names = speaker_names or {}
    has_speakers = any(getattr(s, "speaker", None) for s in segments)
    lines: list[str] = []
    last_marker_min = -5
    last_speaker: str | None = None

    for seg in segments:
        mins = seg.start_ms // 60000
        secs = (seg.start_ms % 60000) // 1000
        if mins >= last_marker_min + 5:
            lines.append(f"\n**[{mins:02d}:{secs:02d}]**\n")
            last_marker_min = mins
            last_speaker = None  # re-print speaker after a time marker

        if has_speakers:
            spk = getattr(seg, "speaker", None) or "unknown"
            name = speaker_names.get(spk, spk.replace("speaker_", "Speaker ").title())
            if spk != last_speaker:
                lines.append(f"\n**{name}:** {seg.text}")
                last_speaker = spk
            else:
                lines.append(seg.text)
        else:
            lines.append(seg.text)

    return " ".join(lines)


def create_meeting_note(mf: MeetingFile, transcript: str, summary_text: str,
                         action_items: list[str]) -> Path:
    """Create Obsidian meeting note."""
    MEETING_NOTES_DIR.mkdir(parents=True, exist_ok=True)

    date_str = mf.meeting_date.isoformat() if mf.meeting_date else "unknown"
    safe_title = re.sub(r'[/\\:*?"<>|]', "", mf.title)

    if mf.meeting_date:
        note_name = f"{date_str} — {safe_title}"
    else:
        note_name = safe_title

    note_path = MEETING_NOTES_DIR / f"{note_name}.md"

    # Don't overwrite existing notes
    if note_path.exists():
        logger.info(f"Note already exists: {note_path.name}, appending transcript")
        with open(note_path, "a") as f:
            f.write(f"\n\n### Transcript (auto-generated {datetime.now().strftime('%Y-%m-%d')})\n\n")
            f.write(transcript)
        return note_path

    actions_md = "\n".join(f"- [ ] {item}" for item in action_items) if action_items else "- [ ]"

    content = f"""---
created: {date_str}
tags: [meeting, {mf.project}]
status: complete
---

# {mf.title}

## Info
- **When:** {date_str}
- **Source:** Plaud recording (`{mf.path.name}`)

## Summary

{summary_text}

## Action Items
{actions_md}

## Notes

### Transcript (auto-generated {datetime.now().strftime('%Y-%m-%d')})

{transcript}

## Related
- [[{date_str}]]

---
**Last updated:** {datetime.now().strftime('%Y-%m-%d')}
"""
    note_path.write_text(content)
    return note_path


def link_from_daily_note(mf: MeetingFile, note_name: str) -> None:
    """Add meeting link to the daily note."""
    if not mf.meeting_date:
        return

    daily_path = DAILY_NOTES_DIR / f"{mf.meeting_date.isoformat()}.md"
    if not daily_path.exists():
        logger.info(f"No daily note for {mf.meeting_date}, skipping link")
        return

    content = daily_path.read_text()

    # Check if already linked
    if note_name in content:
        return

    # Find or create ## Meetings section
    if "## Meetings" in content:
        # Append to existing section
        insert_point = content.index("## Meetings") + len("## Meetings")
        # Find end of line
        nl = content.index("\n", insert_point)
        new_line = f"\n- [[{note_name}]] — {mf.title}"
        content = content[:nl] + new_line + content[nl:]
    else:
        # Insert before ## Notes or ## Today's Focus or at end of schedule
        for marker in ["## Notes & Thoughts", "## Today's Focus", "## Notes", "## Ideas"]:
            if marker in content:
                idx = content.index(marker)
                section = f"## Meetings\n- [[{note_name}]] — {mf.title}\n\n"
                content = content[:idx] + section + content[idx:]
                break
        else:
            content += f"\n## Meetings\n- [[{note_name}]] — {mf.title}\n"

    daily_path.write_text(content)
    logger.info(f"Linked {note_name} from daily note {mf.meeting_date}")


def process_file(mf: MeetingFile, idx: int, total: int) -> dict:
    """Process a single meeting file."""
    logger.info(f"[{idx}/{total}] Processing: {mf.path.name} ({mf.duration_secs/60:.0f} min)")
    start = time.time()

    # Auto-detect title if needed
    if not mf.title:
        logger.info(f"  Auto-detecting title...")
        mf.title = auto_detect_title(mf.path)
        logger.info(f"  Title: {mf.title}")

    # Transcribe
    logger.info(f"  Transcribing...")
    try:
        result = transcribe_file(str(mf.path))
    except Exception as e:
        logger.error(f"  Transcription failed: {e}")
        return {"file": mf.path.name, "status": "error", "error": str(e)}

    transcript = format_transcript(result.segments)

    # Summarize
    logger.info(f"  Summarizing ({len(result.full_text)} chars)...")
    try:
        summary = summarize_transcript(result.full_text, ollama_model="llama3")
        summary_text = summary.summary
        action_items = summary.action_items
    except Exception as e:
        logger.warning(f"  Summarization failed: {e}, using transcript only")
        summary_text = "(Summarization failed)"
        action_items = []

    # Create note
    note_path = create_meeting_note(mf, transcript, summary_text, action_items)
    note_name = note_path.stem

    # Link from daily note
    link_from_daily_note(mf, note_name)

    elapsed = time.time() - start
    logger.success(
        f"  Done in {elapsed:.0f}s: {note_name} "
        f"({len(result.segments)} segments, {len(action_items)} actions)"
    )

    return {
        "file": mf.path.name,
        "status": "ok",
        "note": note_name,
        "segments": len(result.segments),
        "duration_min": mf.duration_secs / 60,
        "elapsed_s": elapsed,
        "actions": len(action_items),
    }


def main():
    logger.info("Cataloging audio files...")
    files = catalog_files()

    to_process = [f for f in files if not f.skip]
    skipped = [f for f in files if f.skip]

    total_minutes = sum(f.duration_secs / 60 for f in to_process)
    est_time = total_minutes / 25  # ~25x realtime

    logger.info(f"Found {len(files)} files total")
    logger.info(f"  Skipping: {len(skipped)} ({', '.join(set(f.skip_reason for f in skipped))})")
    logger.info(f"  Processing: {len(to_process)} files, {total_minutes:.0f} min audio")
    logger.info(f"  Estimated transcription time: {est_time:.0f} min")
    logger.info("")

    results = []
    for i, mf in enumerate(to_process, 1):
        result = process_file(mf, i, len(to_process))
        results.append(result)

    # Summary
    ok = [r for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"] == "error"]

    logger.info("=" * 60)
    logger.success(f"BATCH COMPLETE: {len(ok)}/{len(results)} processed successfully")
    if errors:
        logger.error(f"  Errors: {len(errors)}")
        for e in errors:
            logger.error(f"    {e['file']}: {e['error']}")

    total_elapsed = sum(r.get("elapsed_s", 0) for r in ok)
    logger.info(f"  Total time: {total_elapsed/60:.1f} min")
    logger.info(f"  Total audio: {sum(r.get('duration_min', 0) for r in ok):.0f} min")
    logger.info(f"  Total segments: {sum(r.get('segments', 0) for r in ok)}")
    logger.info(f"  Total action items: {sum(r.get('actions', 0) for r in ok)}")

    # Save results
    results_path = Path("/tmp/meeting-batch-results.json")
    results_path.write_text(json.dumps(results, indent=2))
    logger.info(f"  Results saved to {results_path}")


if __name__ == "__main__":
    main()
