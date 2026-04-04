"""Batch meeting summarization using Ollama.

Takes a transcript and produces a structured summary with action items.
Uses Ollama's REST API directly for simple request/response (not Pipecat streaming).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from loguru import logger

MEETING_SUMMARY_PROMPT = """You are a meeting note assistant. Given a meeting transcript, produce a structured summary.

Output format (use exactly these headers):

## Summary
A concise 2-4 sentence summary of what was discussed.

## Key Decisions
- Decision 1
- Decision 2

## Action Items
- [ ] Action item with assignee if mentioned
- [ ] Another action item

If there are no decisions or action items, write "None identified."

TRANSCRIPT:
{transcript}"""

# Chunk overlap: last 20% of segments from chunk N prepend chunk N+1
CHUNK_OVERLAP_RATIO = 0.2


@dataclass
class SummaryResult:
    """Structured meeting summary."""

    summary: str = ""
    action_items: list[str] = field(default_factory=list)
    raw_response: str = ""


def summarize_transcript(
    transcript: str,
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "llama3.2",
    context_window: int | None = None,
) -> SummaryResult:
    """Summarize a meeting transcript using Ollama.

    If the transcript exceeds the model's context window, it's chunked
    with 20% overlap and summaries are merged.
    """
    if not transcript.strip():
        return SummaryResult(summary="No transcript content to summarize.")

    # Get model context window if not provided
    if context_window is None:
        context_window = _get_context_window(ollama_base_url, ollama_model)

    # Rough token estimate: 1 token ≈ 4 chars
    estimated_tokens = len(transcript) // 4
    # Reserve 30% of context for prompt + response
    available_tokens = int(context_window * 0.7)

    if estimated_tokens <= available_tokens:
        logger.info(
            f"Transcript fits in context ({estimated_tokens} est. tokens, "
            f"{available_tokens} available). Single-pass summarization."
        )
        raw = _call_ollama(transcript, ollama_base_url, ollama_model)
        return _parse_summary(raw)

    # Chunk and merge
    logger.info(
        f"Transcript exceeds context ({estimated_tokens} est. tokens, "
        f"{available_tokens} available). Chunking with {CHUNK_OVERLAP_RATIO:.0%} overlap."
    )
    return _chunked_summarize(transcript, available_tokens, ollama_base_url, ollama_model)


def _get_context_window(base_url: str, model: str) -> int:
    """Query Ollama for the model's context window size."""
    try:
        resp = httpx.post(
            f"{base_url}/api/show",
            json={"name": model},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Extract from modelfile parameters or model_info
        model_info = data.get("model_info", {})
        for key, value in model_info.items():
            if "context_length" in key and isinstance(value, int):
                logger.info(f"Model {model} context window: {value}")
                return value
    except Exception:
        logger.warning(f"Could not query context window for {model}, defaulting to 8192")
    return 8192


def _call_ollama(transcript: str, base_url: str, model: str) -> str:
    """Make a single Ollama generate call."""
    prompt = MEETING_SUMMARY_PROMPT.format(transcript=transcript)

    logger.info(f"Calling Ollama {model} ({len(prompt)} chars prompt)")

    resp = httpx.post(
        f"{base_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3},
        },
        timeout=300,  # 5 min for large transcripts
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


def _chunked_summarize(
    transcript: str,
    available_tokens: int,
    base_url: str,
    model: str,
) -> SummaryResult:
    """Split transcript into overlapping chunks, summarize each, merge."""
    # Split by sentences (rough)
    sentences = transcript.replace(". ", ".\n").split("\n")
    sentences = [s.strip() for s in sentences if s.strip()]

    chunk_char_limit = available_tokens * 4  # rough token-to-char
    overlap_chars = int(chunk_char_limit * CHUNK_OVERLAP_RATIO)

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for sentence in sentences:
        if current_len + len(sentence) > chunk_char_limit and current_chunk:
            chunks.append(" ".join(current_chunk))
            # Keep overlap from end of current chunk
            overlap_sentences: list[str] = []
            overlap_len = 0
            for s in reversed(current_chunk):
                if overlap_len + len(s) > overlap_chars:
                    break
                overlap_sentences.insert(0, s)
                overlap_len += len(s)
            current_chunk = overlap_sentences
            current_len = overlap_len

        current_chunk.append(sentence)
        current_len += len(sentence)

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    logger.info(f"Split into {len(chunks)} chunks")

    # Summarize each chunk
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        logger.info(f"Summarizing chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)")
        try:
            summary = _call_ollama(chunk, base_url, model)
            chunk_summaries.append(summary)
        except Exception:
            logger.error(f"Failed to summarize chunk {i + 1}, skipping")
            chunk_summaries.append(f"[Chunk {i + 1} summarization failed]")

    # Merge summaries
    if len(chunk_summaries) == 1:
        return _parse_summary(chunk_summaries[0])

    merge_prompt = (
        "You are a meeting note assistant. Below are summaries of different parts "
        "of the same meeting. Merge them into a single coherent summary.\n\n"
        "Output format (use exactly these headers):\n\n"
        "## Summary\nA concise 2-4 sentence summary.\n\n"
        "## Key Decisions\n- Decision 1\n\n"
        "## Action Items\n- [ ] Action item\n\n"
        "PARTIAL SUMMARIES:\n\n"
        + "\n\n---\n\n".join(chunk_summaries)
    )

    merged = _call_ollama_raw(merge_prompt, base_url, model)
    return _parse_summary(merged)


def _call_ollama_raw(prompt: str, base_url: str, model: str) -> str:
    """Make a raw Ollama call without the meeting template."""
    resp = httpx.post(
        f"{base_url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


def _parse_summary(raw: str) -> SummaryResult:
    """Parse structured summary from LLM response."""
    summary = ""
    action_items: list[str] = []

    lines = raw.split("\n")
    current_section = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Summary"):
            current_section = "summary"
            continue
        if stripped.startswith("## Key Decisions"):
            current_section = "decisions"
            continue
        if stripped.startswith("## Action Items"):
            current_section = "actions"
            continue

        if current_section == "summary" and stripped:
            summary += stripped + " "
        elif current_section == "actions" and stripped.startswith("- ["):
            # Extract the text after "- [ ] " or "- [x] "
            item_text = stripped[6:].strip() if len(stripped) > 6 else stripped
            if item_text and item_text.lower() != "none identified.":
                action_items.append(item_text)

    return SummaryResult(
        summary=summary.strip(),
        action_items=action_items,
        raw_response=raw,
    )
