"""Meeting recording API endpoints.

Provides async transcription and summarization of audio files.
Jobs run in background and are polled via GET /api/meeting/job/{job_id}.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from utils.rate_limiter import RATE_LIMIT_CONFIG, get_ip_only, limiter

meeting_router = APIRouter(prefix="/api/meeting", tags=["meeting"])


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class Job:
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# In-memory job store (lost on restart, acceptable for Phase 1)
_jobs: dict[str, Job] = {}


class TranscribeRequest(BaseModel):
    wav_path: str
    model: str | None = None
    language: str = "en"
    backend: str | None = None  # "parakeet_remote" | "whisper_mlx"; None -> env default
    diarize: bool = True


class SummarizeRequest(BaseModel):
    transcript: str
    ollama_base_url: str | None = None
    ollama_model: str | None = None


@meeting_router.post("/transcribe")
@limiter.limit(RATE_LIMIT_CONFIG)
async def transcribe(request: Request, body: TranscribeRequest) -> dict[str, str]:
    """Start async transcription of an audio file.

    Returns a job_id immediately. Poll GET /api/meeting/job/{job_id} for status.
    """
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id)
    _jobs[job_id] = job

    settings = request.app.state.settings

    model = body.model or getattr(settings, "whisper_mlx_model", None) or "mlx-community/whisper-large-v3-turbo"

    backend = body.backend or getattr(settings, "meeting_stt_backend", None)

    asyncio.get_event_loop().run_in_executor(
        None,
        _run_transcription,
        job,
        body.wav_path,
        model,
        body.language,
        backend,
        body.diarize,
    )

    logger.info(f"Transcription job {job_id} queued for {body.wav_path}")
    return {"job_id": job_id}


@meeting_router.post("/summarize")
@limiter.limit(RATE_LIMIT_CONFIG)
async def summarize(request: Request, body: SummarizeRequest) -> dict[str, str]:
    """Start async summarization of a transcript.

    Returns a job_id immediately. Poll GET /api/meeting/job/{job_id} for status.
    """
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id)
    _jobs[job_id] = job

    settings = request.app.state.settings

    base_url = body.ollama_base_url or getattr(settings, "ollama_base_url", None) or "http://localhost:11434"
    model = body.ollama_model or getattr(settings, "ollama_model", None) or "llama3.2"

    asyncio.get_event_loop().run_in_executor(
        None,
        _run_summarization,
        job,
        body.transcript,
        base_url,
        model,
    )

    logger.info(f"Summarization job {job_id} queued ({len(body.transcript)} chars)")
    return {"job_id": job_id}


@meeting_router.get("/job/{job_id}")
@limiter.limit(RATE_LIMIT_CONFIG)
async def get_job(request: Request, job_id: str) -> dict:
    """Poll job status. Returns status and result when complete."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    response: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status.value,
    }

    if job.status == JobStatus.COMPLETE:
        response["result"] = job.result
    elif job.status == JobStatus.ERROR:
        response["error"] = job.error

    return response


@meeting_router.get("/health")
async def meeting_health() -> dict[str, str]:
    """Health check for meeting API."""
    return {"status": "ok", "active_jobs": str(len([j for j in _jobs.values() if j.status == JobStatus.PROCESSING]))}


def _run_transcription(
    job: Job,
    wav_path: str,
    model: str,
    language: str,
    backend: str | None = None,
    diarize: bool = True,
) -> None:
    """Run transcription in a thread pool."""
    job.status = JobStatus.PROCESSING
    try:
        from processors.meeting_transcriber import transcribe_file

        result = transcribe_file(
            wav_path, model_name=model, language=language, backend=backend, diarize=diarize
        )
        job.result = {
            "segments": [
                {"start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text, "speaker": s.speaker}
                for s in result.segments
            ],
            "full_text": result.full_text,
            "duration_secs": result.duration_secs,
            "speakers": result.speakers,
        }
        job.status = JobStatus.COMPLETE
        logger.success(f"Transcription job {job.job_id} complete")
    except Exception as e:
        job.status = JobStatus.ERROR
        job.error = str(e)
        logger.error(f"Transcription job {job.job_id} failed: {e}")


def _run_summarization(job: Job, transcript: str, base_url: str, model: str) -> None:
    """Run summarization in a thread pool."""
    job.status = JobStatus.PROCESSING
    try:
        from processors.meeting_summarizer import summarize_transcript

        result = summarize_transcript(
            transcript,
            ollama_base_url=base_url,
            ollama_model=model,
        )
        job.result = {
            "summary": result.summary,
            "action_items": result.action_items,
            "raw_response": result.raw_response,
        }
        job.status = JobStatus.COMPLETE
        logger.success(f"Summarization job {job.job_id} complete")
    except Exception as e:
        job.status = JobStatus.ERROR
        job.error = str(e)
        logger.error(f"Summarization job {job.job_id} failed: {e}")
