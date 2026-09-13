"""
commands/schema.py — Pydantic schemas for Harley's Structured Intent system
============================================================================
Every action Harley executes MUST be represented as a StructuredIntent before
any executor touches it.  The AI model is untrusted; this schema is the hard
boundary between "model output" and "Python execution".

Spec: docs/specs/02_security.md §1
CLAUDE.md invariant: No Direct Execution — only Executors act on this schema.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, Any, Optional

from pydantic import BaseModel, Field, field_validator, ConfigDict, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class IntentOrigin(str, Enum):
    """
    Tracks how the intent reached the Orchestrator.

    DIRECT_USER          — typed/spoken directly by the authenticated user.
    INDIRECT_WEB_CONTENT — sourced from a web page via the Chrome extension.
                           Taint Analysis MUST downgrade ALLOW → CONFIRM.
                           (docs/specs/02_security.md §3)
    SYSTEM               — generated internally by Harley (e.g., reminders).
    CHROME_EXTENSION     — bridged from the MV3 extension stdio host, but
                           DOM-sanitised. Treated as INDIRECT_WEB_CONTENT
                           unless explicitly overridden.
    """
    DIRECT_USER          = "DIRECT_USER"
    INDIRECT_WEB_CONTENT = "INDIRECT_WEB_CONTENT"
    SYSTEM               = "SYSTEM"
    CHROME_EXTENSION     = "CHROME_EXTENSION"


class PermissionLevel(str, Enum):
    """
    4-level permission matrix (docs/specs/02_security.md §2).

    ALLOW   — execute immediately, no user prompt.
    CONFIRM — pause, show Toast + voice prompt, wait for explicit user approval.
    DENY    — silently drop the intent; log the rejection.
    BLOCK   — hard block; log, alert user, increment suspicion counter.
    """
    ALLOW   = "ALLOW"
    CONFIRM = "CONFIRM"
    DENY    = "DENY"
    BLOCK   = "BLOCK"


# ---------------------------------------------------------------------------
# Core schema
# ---------------------------------------------------------------------------

class StructuredIntent(BaseModel):
    """
    The strict boundary schema for all AI-generated intents.
    Frozen to prevent post-creation tampering.
    Extra fields are forbidden to prevent hallucinated parameters.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: str = Field(description="A brief description of the intent")
    application: str = Field(default="Harley", description="The target application")
    action: str = Field(description="The action to perform in UPPER_SNAKE_CASE")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Action arguments")
    origin: IntentOrigin = Field(default=IntentOrigin.DIRECT_USER, description="Source of intent")
    confidence: float = Field(default=1.0, description="Confidence of the parsed intent")

    @model_validator(mode="before")
    @classmethod
    def sanitize_origin(cls, data: Any) -> Any:
        """Auto-converts legacy or alternate origins to the strict enum."""
        if isinstance(data, dict):
            raw_origin = data.get("origin")
            if raw_origin in ("CHROME_EXTENSION", IntentOrigin.CHROME_EXTENSION):
                data["origin"] = IntentOrigin.INDIRECT_WEB_CONTENT.value
        return data

    @field_validator("action")
    @classmethod
    def enforce_upper_snake_case(cls, value: str) -> str:
        if not re.match(r"^[A-Z][A-Z0-9_]*$", value):
            raise ValueError("Action must be in UPPER_SNAKE_CASE")
        return value

    @field_validator("confidence")
    @classmethod
    def enforce_confidence_bounds(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError("Confidence must be between 0.0 and 1.0")
        return value


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def intent_from_llm_json(raw: dict[str, Any]) -> StructuredIntent:
    """
    Parse and validate raw LLM JSON into a StructuredIntent.

    Raises pydantic.ValidationError on any schema violation.
    Callers should catch ValidationError and treat it as an automatic DENY.
    """
    return StructuredIntent.model_validate(raw)
