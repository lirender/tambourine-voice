"""Batch meeting transcription using MLX Whisper.

Transcribes audio files (WAV, MP3) and returns timestamped segments.
Designed for post-meeting processing, not real-time streaming.
"""

from __future__ import annotations

import importlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


@dataclass
class TranscriptSegment:
    """A single timestamped segment of transcribed text."""

    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptionResult:
    """Complete transcription output."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    full_text: str = ""
    duration_secs: float = 0.0


def _convert_to_wav(input_path: Path) -> Path:
    """Convert audio/video file to 16kHz mono WAV using ffmpeg.

    Returns path to the temporary WAV file. Caller must clean up.
    Raises RuntimeError if ffmpeg is not installed or conversion fails.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".wav":
        return input_path

    # Check ffmpeg availability
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        msg = (
            "ffmpeg is required for non-WAV files. "
            "Install with: brew install ffmpeg"
        )
        raise RuntimeError(msg) from e

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    output_path = Path(tmp.name)

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(input_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                "-y",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=600,  # 10 minutes max for large files
        )
    except subprocess.CalledProcessError as e:
        output_path.unlink(missing_ok=True)
        msg = f"ffmpeg conversion failed: {e.stderr.decode()}"
        raise RuntimeError(msg) from e

    return output_path


def transcribe_file(
    file_path: str,
    model_name: str = "mlx-community/whisper-large-v3-turbo",
    language: str = "en",
) -> TranscriptionResult:
    """Transcribe an audio/video file using MLX Whisper.

    Supports WAV, MP3, MP4, WebM (non-WAV requires ffmpeg).
    Returns timestamped segments and full text.
    """
    input_path = Path(file_path)
    if not input_path.exists():
        msg = f"File not found: {file_path}"
        raise FileNotFoundError(msg)

    # Check file size (2GB limit)
    size_gb = input_path.stat().st_size / (1024**3)
    if size_gb > 2.0:
        msg = f"File too large ({size_gb:.1f} GB). Maximum is 2 GB."
        raise ValueError(msg)

    # Convert to WAV if needed
    wav_path = _convert_to_wav(input_path)
    temp_wav = wav_path != input_path

    try:
        return _transcribe_wav(wav_path, model_name, language)
    finally:
        if temp_wav:
            wav_path.unlink(missing_ok=True)


def _transcribe_wav(
    wav_path: Path,
    model_name: str,
    language: str,
) -> TranscriptionResult:
    """Run MLX Whisper on a WAV file."""
    logger.info(f"Transcribing {wav_path.name} with model {model_name}")

    mlx_whisper = importlib.import_module("mlx_whisper")
    transcribe_fn = getattr(mlx_whisper, "transcribe", None)
    if not callable(transcribe_fn):
        msg = "mlx_whisper.transcribe is unavailable"
        raise RuntimeError(msg)

    result = transcribe_fn(
        str(wav_path),
        path_or_hf_repo=model_name,
        language=language,
        temperature=0.0,
        word_timestamps=True,
    )

    segments: list[TranscriptSegment] = []
    for seg in result.get("segments", []):
        segments.append(
            TranscriptSegment(
                start_ms=int(seg["start"] * 1000),
                end_ms=int(seg["end"] * 1000),
                text=seg["text"].strip(),
            )
        )

    full_text = " ".join(s.text for s in segments)
    duration = segments[-1].end_ms / 1000.0 if segments else 0.0

    logger.success(
        f"Transcription complete: {len(segments)} segments, "
        f"{duration:.1f}s duration, {len(full_text)} chars"
    )

    return TranscriptionResult(
        segments=segments,
        full_text=full_text,
        duration_secs=duration,
    )
