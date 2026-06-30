"""LLM Gate Filter - Controls frame flow to the LLM aggregator.

This filter sits between TurnController and the LLMUserAggregator to:
1. Own the LLM bypass state (single source of truth)
2. Gate frames selectively for the aggregator
3. Emit RawTranscriptionMessage when recording ends with LLM bypassed

Key insight: The aggregator only accumulates frames between UserStartedSpeakingFrame
and UserStoppedSpeakingFrame. By blocking UserStartedSpeakingFrame, we prevent
accumulation while still letting TranscriptionFrames flow through for RTVI
UserTranscript events.

Pipeline position:
    TurnController → LLMGateFilter → LLMUserAggregator
"""

from __future__ import annotations

from typing import Any

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

from protocol.messages import EmptyTranscriptMessage, RawTranscriptionMessage
from utils.logger import logger


class LLMGateFilter(FrameProcessor):
    """Gates frames to LLM aggregator and handles bypass output.

    When LLM formatting is disabled:
    - Blocks UserStartedSpeakingFrame (aggregator stays idle)
    - Passes TranscriptionFrame through (RTVI gets UserTranscript events)
    - Blocks UserStoppedSpeakingFrame and emits RawTranscriptionMessage instead

    When LLM formatting is enabled:
    - Passes all frames through unchanged
    """

    def __init__(self, formatter: Any = None, **kwargs: Any) -> None:
        """Initialize the LLM gate filter.

        Args:
            formatter: Optional async callable (text: str) -> str that formats the
                full transcript via a direct LLM call. When set and formatting is
                enabled, the gate uses it instead of the (fragile) streaming
                aggregator path.
        """
        super().__init__(**kwargs)
        self._llm_formatting_enabled: bool = True
        self._accumulated_text: list[str] = []
        self._formatter = formatter

    def set_llm_formatting_enabled(self, enabled: bool) -> None:
        """Set whether LLM formatting is enabled.

        Args:
            enabled: True to use LLM formatting, False for raw transcription
        """
        self._llm_formatting_enabled = enabled
        if enabled:
            logger.info("LLM formatting enabled (LLMGateFilter)")
        else:
            logger.info("LLM formatting disabled (LLMGateFilter)")

    def get_llm_formatting_enabled(self) -> bool:
        """Get whether LLM formatting is enabled."""
        return self._llm_formatting_enabled

    def reset_for_recording(self) -> None:
        """Reset state for a new recording.

        Called when recording starts to clear any accumulated text.
        """
        self._accumulated_text = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Process frames. The gate always accumulates the transcript and emits
        the result itself (raw, or formatted via a direct LLM call), bypassing the
        fragile streaming aggregator entirely.
        """
        await super().process_frame(frame, direction)

        match frame:
            case UserStartedSpeakingFrame():
                # Block - the downstream aggregator is bypassed in both modes.
                self._accumulated_text = []

            case TranscriptionFrame(text=text) if text:
                self._accumulated_text.append(text)
                # Pass through for RTVI UserTranscript events (live transcript UI).
                await self.push_frame(frame, direction)

            case UserStoppedSpeakingFrame():
                combined_text = " ".join(self._accumulated_text).strip()
                self._accumulated_text = []

                if not combined_text:
                    await self.push_frame(
                        RTVIServerMessageFrame(data=EmptyTranscriptMessage().model_dump()),
                        direction,
                    )
                    return

                output_text = combined_text
                if self._llm_formatting_enabled and self._formatter is not None:
                    try:
                        output_text = await self._formatter(combined_text)
                        logger.info(f"LLM formatted: '{combined_text}' -> '{output_text}'")
                    except Exception as e:  # noqa: BLE001 - never lose the dictation
                        logger.warning(f"Formatting failed ({e}); emitting raw transcription")
                        output_text = combined_text
                else:
                    logger.info(f"Raw transcription (formatting off): '{combined_text}'")

                if not output_text.strip():
                    output_text = combined_text  # never emit empty after formatting
                await self.push_frame(
                    RTVIServerMessageFrame(
                        data=RawTranscriptionMessage(text=output_text.strip()).model_dump()
                    ),
                    direction,
                )

            case _:
                await self.push_frame(frame, direction)
