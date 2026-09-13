"""
Harley Voice Assistant - Conversational Context Manager
Maintains a time-sensitive sliding window of recent interactions to enable co-reference resolution.
"""

import time
import logging
from collections import deque
from typing import Optional

from commands.schema import StructuredIntent

logger = logging.getLogger("HarleyContext")

class TurnRecord:
    """A data class representing a single completed conversational turn."""
    def __init__(self, transcript: str, intent: Optional[StructuredIntent], response: str):
        self.timestamp = time.time()
        self.transcript = transcript
        self.intent = intent
        self.response = response

class ContextManager:
    def __init__(self, max_turns: int = 5, expiry_minutes: int = 5):
        """
        Initializes the sliding window memory.
        :param max_turns: Number of recent turns to keep in context.
        :param expiry_minutes: How long before the context is automatically flushed.
        """
        self.max_turns = max_turns
        self.expiry_seconds = expiry_minutes * 60
        # deque automatically pops the oldest item when maxlen is exceeded
        self.history = deque(maxlen=max_turns)
        self.active_web_context: Optional[str] = None  # Buffer for browser captures
        self.web_context_timestamp: float = 0.0
        
        logger.info(f"[ContextManager] Initialized: max {max_turns} turns, {expiry_minutes}m expiry.")

    def set_web_context(self, content: str):
        """Stores clean, visible text extracted from Chrome."""
        self.active_web_context = content.strip()
        self.web_context_timestamp = time.time()
        logger.info(f"[ContextManager] Web context cached ({len(self.active_web_context)} chars).")

    def get_web_context(self) -> str:
        """Returns active web context if captured within the last 5 minutes."""
        if not self.active_web_context:
            return ""
        if (time.time() - self.web_context_timestamp) > self.expiry_seconds:
            self.active_web_context = None
            return ""
        return f"<untrusted_web_content>\n{self.active_web_context}\n</untrusted_web_content>"

    def _check_expiry(self):
        """Flushes the history if the last interaction is older than the expiry limit."""
        if not self.history:
            return

        last_turn_time = self.history[-1].timestamp
        if (time.time() - last_turn_time) > self.expiry_seconds:
            logger.info("[ContextManager] Context expired. Flushing short-term memory.")
            self.clear_context()

    def add_turn(self, transcript: str, intent: Optional[StructuredIntent], response: str):
        """Records a completed turn to the sliding window."""
        self._check_expiry()
        self.history.append(TurnRecord(transcript, intent, response))
        logger.debug(f"[ContextManager] Turn recorded. Context size: {len(self.history)}")

    def get_context_summary(self) -> str:
        """
        Formats recent turns into a compact string to inject into the LLM system prompt.
        """
        self._check_expiry()

        if not self.history:
            return ""

        summary_lines = []
        for i, turn in enumerate(self.history, 1):
            action = turn.intent.action if turn.intent else "UNKNOWN_ACTION"
            target = turn.intent.application if turn.intent else "UNKNOWN_APP"
            
            summary_lines.append(
                f"Turn {i}:\n"
                f"  User said: \"{turn.transcript}\"\n"
                f"  System mapped to: [{target}] {action}\n"
                f"  System replied: \"{turn.response}\""
            )
        
        return "\n\n".join(summary_lines)

    def clear_context(self):
        """Flushes turn memory and active browser context."""
        self.history.clear()
        self.active_web_context = None
        logger.debug("[ContextManager] Context and web buffers cleared.")
