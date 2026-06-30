"""Parakeet ASR + diarization service for GB10 (DGX Spark).

Wraps NVIDIA NeMo parakeet-tdt-0.6b-v3 (transcription) and
diar_sortformer_4spk-v1 (speaker diarization). Returns timestamped
segments tagged with speaker labels.

Endpoints:
    GET  /health           -> model readiness
    POST /transcribe       -> multipart audio upload, returns diarized segments

Run on gb10.local; the Mac server calls it as the `parakeet_remote` STT provider.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import asyncio
import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from loguru import logger

ASR_MODEL_NAME = os.environ.get("ASR_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
DIAR_MODEL_NAME = os.environ.get("DIAR_MODEL", "nvidia/diar_sortformer_4spk-v1")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

app = FastAPI(title="Parakeet ASR + Diarization", version="1.0.0")

# Lazily-loaded singletons (loaded on first request / startup)
_asr_model: Any = None
_diar_model: Any = None


def _load_models() -> None:
    """Load ASR and diarization models onto the GPU (idempotent)."""
    global _asr_model, _diar_model

    if _asr_model is None:
        import nemo.collections.asr as nemo_asr

        logger.info(f"Loading ASR model {ASR_MODEL_NAME} on {DEVICE}...")
        _asr_model = nemo_asr.models.ASRModel.from_pretrained(model_name=ASR_MODEL_NAME)
        _asr_model = _asr_model.to(DEVICE).eval()
        logger.success("ASR model loaded")

    if _diar_model is None:
        from nemo.collections.asr.models import SortformerEncLabelModel

        logger.info(f"Loading diarization model {DIAR_MODEL_NAME} on {DEVICE}...")
        _diar_model = SortformerEncLabelModel.from_pretrained(model_name=DIAR_MODEL_NAME)
        _diar_model = _diar_model.to(DEVICE).eval()
        logger.success("Diarization model loaded")


@app.on_event("startup")
def _startup() -> None:
    try:
        _load_models()
    except Exception as e:  # noqa: BLE001 - log and allow /health to report
        logger.error(f"Model load on startup failed (will retry on request): {e}")


def _to_wav_16k_mono(src: Path) -> Path:
    """Convert any audio to 16kHz mono WAV via ffmpeg."""
    if src.suffix.lower() == ".wav":
        # Still normalise sample rate to be safe.
        pass
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    out = Path(tmp.name)
    subprocess.run(
        ["ffmpeg", "-i", str(src), "-ar", "16000", "-ac", "1", "-f", "wav", "-y", str(out)],
        capture_output=True,
        check=True,
        timeout=900,
    )
    return out


def _assign_speakers(
    words: list[dict[str, Any]], diar_segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Assign each ASR word to the speaker whose turn maximally overlaps it.

    diar_segments: [{"start": s, "end": s, "speaker": "speaker_0"}, ...]
    words:         [{"word": str, "start": s, "end": s}, ...]
    """
    for w in words:
        ws, we = w["start"], w["end"]
        best_spk, best_overlap = "speaker_0", 0.0
        for d in diar_segments:
            overlap = max(0.0, min(we, d["end"]) - max(ws, d["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_spk = d["speaker"]
        w["speaker"] = best_spk
    return words


def _group_into_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group consecutive same-speaker words into segments."""
    segments: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for w in words:
        spk = w.get("speaker", "speaker_0")
        if cur is None or cur["speaker"] != spk:
            if cur is not None:
                segments.append(cur)
            cur = {
                "start_ms": int(w["start"] * 1000),
                "end_ms": int(w["end"] * 1000),
                "speaker": spk,
                "text": w["word"],
            }
        else:
            cur["end_ms"] = int(w["end"] * 1000)
            cur["text"] += " " + w["word"]
    if cur is not None:
        segments.append(cur)
    return segments


def _run_diarization(wav_path: Path) -> list[dict[str, Any]]:
    """Run Sortformer; return list of {start, end, speaker} in seconds."""
    # Sortformer returns per-file predicted segments as "start end speaker" strings.
    preds = _diar_model.diarize(audio=[str(wav_path)], batch_size=1)
    diar_segments: list[dict[str, Any]] = []
    # preds is a list (one per input file) of lists of "start end speaker_k" strings.
    for entry in preds[0]:
        parts = str(entry).split()
        if len(parts) >= 3:
            diar_segments.append(
                {"start": float(parts[0]), "end": float(parts[1]), "speaker": parts[2]}
            )
    return diar_segments


def _run_asr(wav_path: Path) -> list[dict[str, Any]]:
    """Run Parakeet with word timestamps; return list of {word, start, end}."""
    out = _asr_model.transcribe([str(wav_path)], timestamps=True)
    hyp = out[0]
    words: list[dict[str, Any]] = []
    ts = getattr(hyp, "timestamp", None) or {}
    for w in ts.get("word", []):
        words.append({"word": w["word"], "start": float(w["start"]), "end": float(w["end"])})
    if not words:
        # Fallback: no word timestamps -> single segment from full text.
        text = getattr(hyp, "text", None)
        if text is None and isinstance(hyp, str):
            text = hyp
        text = (text or "").strip()
        if text:
            words.append({"word": text, "start": 0.0, "end": 0.0})
    return words


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok" if (_asr_model is not None and _diar_model is not None) else "loading",
        "device": DEVICE,
        "asr_model": ASR_MODEL_NAME,
        "diar_model": DIAR_MODEL_NAME,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }


# Serialize model access across the streaming socket and the batch endpoint.
_model_lock = asyncio.Lock()

# Streaming params for dictation: re-transcribe the growing utterance buffer
# every ~400ms of new audio (16kHz * 2 bytes = 32000 B/s -> 12800 B ≈ 0.4s).
_STREAM_CHUNK_BYTES = 12800
_MIN_BYTES = 4000  # don't bother transcribing < ~0.12s


def _pcm16_to_float32(buf: bytes) -> "np.ndarray":
    return np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe_array(arr: "np.ndarray") -> str:
    """Blocking ASR on a raw float32 16kHz mono array. Runs in an executor.

    Returns "" for empty/silent audio. Never falls back to str(hyp) — the raw
    Hypothesis repr would otherwise leak into the transcript stream.
    """
    out = _asr_model.transcribe([arr], verbose=False)
    hyp = out[0]
    text = getattr(hyp, "text", None)
    if text is None and isinstance(hyp, str):
        text = hyp
    return (text or "").strip()


@app.websocket("/ws")
async def stream(ws: WebSocket) -> None:
    """Pseudo-streaming dictation ASR.

    Protocol (matches the Mac server's NVidiaWebSocketSTTService):
      client -> binary PCM16 16kHz mono frames, or {"type":"reset"} (JSON text)
      server -> {"type":"ready"} on connect
                {"type":"transcript","text":..,"is_final":false} interim
                {"type":"transcript","text":..,"is_final":true}  on reset
    """
    await ws.accept()
    try:
        _load_models()
    except Exception as e:  # noqa: BLE001
        await ws.close(code=1011, reason=f"model load failed: {e}")
        return
    await ws.send_json({"type": "ready"})

    buf = bytearray()
    loop = asyncio.get_event_loop()

    async def finalize() -> None:
        """One transcribe per utterance, on reset. No interims -> no GPU pile-up
        starving uvicorn's event loop (the cause of the 400-handshake wedge)."""
        if len(buf) < _MIN_BYTES:
            await ws.send_json({"type": "transcript", "text": "", "is_final": True})
            return
        arr = _pcm16_to_float32(bytes(buf))
        async with _model_lock:
            text = await loop.run_in_executor(None, _transcribe_array, arr)
        await ws.send_json({"type": "transcript", "text": text, "is_final": True})

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                buf.extend(data)
            elif (text := msg.get("text")) is not None:
                try:
                    obj = __import__("json").loads(text)
                except Exception:  # noqa: BLE001
                    continue
                if obj.get("type") == "reset":
                    await finalize()
                    buf.clear()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"stream error: {e}")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    diarize: bool = Form(True),
) -> dict[str, Any]:
    """Transcribe (and optionally diarize) an uploaded audio file."""
    _load_models()
    start = time.time()

    suffix = Path(file.filename or "audio").suffix or ".wav"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()
    src = Path(tmp.name)

    wav = None
    try:
        wav = _to_wav_16k_mono(src)
        words = _run_asr(wav)

        if diarize:
            try:
                diar = _run_diarization(wav)
                words = _assign_speakers(words, diar)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Diarization failed, returning unlabeled: {e}")

        segments = _group_into_segments(words)
        full_text = " ".join(s["text"] for s in segments)
        duration = segments[-1]["end_ms"] / 1000.0 if segments else 0.0
        speakers = sorted({s["speaker"] for s in segments})

        elapsed = time.time() - start
        logger.success(
            f"Transcribed {file.filename}: {len(segments)} segs, "
            f"{len(speakers)} speakers, {duration:.0f}s audio in {elapsed:.0f}s"
        )
        return {
            "segments": segments,
            "full_text": full_text,
            "duration_secs": duration,
            "speakers": speakers,
            "elapsed_secs": elapsed,
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"ffmpeg failed: {e.stderr.decode()[:500]}")
    except Exception as e:  # noqa: BLE001
        logger.exception("Transcription failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        src.unlink(missing_ok=True)
        if wav is not None and wav != src:
            wav.unlink(missing_ok=True)
