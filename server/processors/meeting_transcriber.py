"""Batch meeting transcription using MLX Whisper.

Transcribes audio files (WAV, MP3) and returns timestamped segments.
Designed for post-meeting processing, not real-time streaming.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# Remote Parakeet (GB10) service. When configured, meeting transcription is
# offloaded to NVIDIA parakeet-tdt-0.6b-v3 + Sortformer diarization on the
# DGX Spark, which yields higher accuracy AND per-segment speaker labels.
DEFAULT_PARAKEET_URL = os.environ.get("PARAKEET_SERVICE_URL", "http://gb10.local:8770")
# Backend selector: "parakeet_remote" (GB10) or "whisper_mlx" (local Mac fallback).
DEFAULT_BACKEND = os.environ.get("MEETING_STT_BACKEND", "whisper_mlx")


@dataclass
class TranscriptSegment:
    """A single timestamped segment of transcribed text."""

    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None  # e.g. "speaker_0"; None when diarization is unavailable


@dataclass
class TranscriptionResult:
    """Complete transcription output."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    full_text: str = ""
    duration_secs: float = 0.0
    speakers: list[str] = field(default_factory=list)  # distinct speaker labels, if diarized


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
    backend: str | None = None,
    diarize: bool = True,
) -> TranscriptionResult:
    """Transcribe an audio/video file.

    Backends:
        "parakeet_remote": offload to the GB10 Parakeet+Sortformer service
            (higher accuracy + speaker diarization).
        "whisper_mlx":     local MLX Whisper on this Mac (no diarization).

    The default backend comes from the MEETING_STT_BACKEND env var.
    Supports WAV, MP3, MP4, WebM (non-WAV requires ffmpeg).
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

    backend = backend or DEFAULT_BACKEND

    if backend == "parakeet_remote":
        try:
            return _transcribe_remote_parakeet(input_path, diarize=diarize)
        except Exception as e:  # noqa: BLE001 - fall back to local on any remote failure
            logger.warning(
                f"Parakeet remote backend failed ({e}); falling back to local MLX Whisper"
            )

    # Local MLX Whisper path. Convert to WAV if needed.
    wav_path = _convert_to_wav(input_path)
    temp_wav = wav_path != input_path

    try:
        return _transcribe_wav(wav_path, model_name, language)
    finally:
        if temp_wav:
            wav_path.unlink(missing_ok=True)


def _transcribe_remote_parakeet(
    input_path: Path,
    url: str | None = None,
    diarize: bool = True,
) -> TranscriptionResult:
    """Upload audio to the GB10 Parakeet service and parse diarized segments."""
    import httpx

    base = (url or DEFAULT_PARAKEET_URL).rstrip("/")
    logger.info(f"Transcribing {input_path.name} via Parakeet service at {base}")

    with open(input_path, "rb") as fh:
        files = {"file": (input_path.name, fh, "application/octet-stream")}
        data = {"diarize": "true" if diarize else "false"}
        # Long timeout: transcription of a full meeting can take minutes.
        resp = httpx.post(f"{base}/transcribe", files=files, data=data, timeout=1800.0)
    resp.raise_for_status()
    payload = resp.json()

    segments = [
        TranscriptSegment(
            start_ms=int(s["start_ms"]),
            end_ms=int(s["end_ms"]),
            text=s["text"].strip(),
            speaker=s.get("speaker"),
        )
        for s in payload.get("segments", [])
    ]
    speakers = payload.get("speakers", [])

    logger.success(
        f"Parakeet transcription complete: {len(segments)} segments, "
        f"{len(speakers)} speakers, {payload.get('duration_secs', 0):.1f}s"
    )

    return TranscriptionResult(
        segments=segments,
        full_text=payload.get("full_text", " ".join(s.text for s in segments)),
        duration_secs=float(payload.get("duration_secs", 0.0)),
        speakers=speakers,
    )


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
