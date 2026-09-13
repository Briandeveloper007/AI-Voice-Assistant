"""
security/permissions.py — Permission Gatekeeper & Taint Analysis Engine
=======================================================================
Implements the 4-level permission evaluation matrix and taint analysis
described in docs/specs/02_security.md.

Architectural invariants (CLAUDE.md):
  • Only Python Executors run actions — this gatekeeper decides if they may.
  • For UI popups, strictly use `windows-toasts` with async callbacks.
  • Never use print() — log to stderr / logging only.

Usage (from the Orchestrator):
    from security.permissions import PermissionGatekeeper, GatekeeperVerdict
    from commands.schema import StructuredIntent

    gatekeeper = PermissionGatekeeper()
    verdict = await gatekeeper.evaluate(intent)

    if verdict.level == PermissionLevel.ALLOW:
        executor.run(intent)
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from dataclasses import dataclass
from typing import Any, Final

# Re-export from schema so callers only need one import
from commands.schema import IntentOrigin, PermissionLevel, StructuredIntent
# Toast UI layer — owns all windows-toasts interaction (CLAUDE.md §2)
from ui.toasts import ConfirmationResult, request_action_confirmation

log = logging.getLogger("harley.security.permissions")

# ---------------------------------------------------------------------------
# Default Action → Permission mapping
# ---------------------------------------------------------------------------
# Format:  "ACTION_NAME": PermissionLevel
#
# Executors not listed here fall back to DEFAULT_LEVEL.
# Override at runtime via PermissionGatekeeper.set_override().
#
# Rationale for defaults:
#   ALLOW   — low-risk read-only or reversible actions
#   CONFIRM — irreversible, destructive, or privacy-sensitive actions
#   DENY    — actions Harley has no business performing
#   BLOCK   — known attack patterns / prompt injection vectors

_DEFAULT_PERMISSION_TABLE: Final[dict[str, PermissionLevel]] = {
    # ── Safe / read-only ─────────────────────────────────────────────
    "NO_OP":            PermissionLevel.ALLOW,
    "WEB_SEARCH":       PermissionLevel.ALLOW,
    "GET_TIME":         PermissionLevel.ALLOW,
    "GET_WEATHER":      PermissionLevel.ALLOW,
    "READ_CLIPBOARD":   PermissionLevel.ALLOW,
    "PLAY_MUSIC":       PermissionLevel.ALLOW,
    "PAUSE_MUSIC":      PermissionLevel.ALLOW,
    "SET_VOLUME":       PermissionLevel.ALLOW,
    "OPEN_APP":         PermissionLevel.ALLOW,
    "CLOSE_APP":        PermissionLevel.ALLOW,
    "SEND_TOAST":       PermissionLevel.ALLOW,
    # ── Irreversible / privacy-sensitive (need explicit confirmation) ─
    "SEND_EMAIL":       PermissionLevel.CONFIRM,
    "SEND_MESSAGE":     PermissionLevel.CONFIRM,
    "DELETE_FILE":      PermissionLevel.CONFIRM,
    "WRITE_FILE":       PermissionLevel.CONFIRM,
    "RUN_SCRIPT":       PermissionLevel.CONFIRM,
    "OPEN_URL":         PermissionLevel.CONFIRM,
    "WRITE_CLIPBOARD":  PermissionLevel.CONFIRM,
    "PURCHASE":         PermissionLevel.CONFIRM,
    "SUBMIT_FORM":      PermissionLevel.CONFIRM,
    "CHANGE_SETTING":   PermissionLevel.CONFIRM,
    # ── Explicitly prohibited ─────────────────────────────────────────
    "EXEC_SHELL":       PermissionLevel.DENY,
    "INSTALL_PACKAGE":  PermissionLevel.DENY,
    "MODIFY_REGISTRY":  PermissionLevel.DENY,
    # ── Hard blocks — known injection / escalation patterns ───────────
    "ELEVATE_PRIVILEGE": PermissionLevel.BLOCK,
    "DISABLE_SECURITY":  PermissionLevel.BLOCK,
    "EXFILTRATE_DATA":   PermissionLevel.BLOCK,
}

DEFAULT_LEVEL: Final[PermissionLevel] = PermissionLevel.CONFIRM  # fail-safe default

# Low-confidence threshold: intents below this are auto-DENY
CONFIDENCE_THRESHOLD: Final[float] = 0.50

# Origins that trigger taint analysis
_TAINTED_ORIGINS: Final[frozenset[IntentOrigin]] = frozenset(
    {IntentOrigin.INDIRECT_WEB_CONTENT}
)


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GatekeeperVerdict:
    """
    The final decision for a StructuredIntent.

    Attributes
    ----------
    level : PermissionLevel
        The resolved permission level after all transformations.
    intent : StructuredIntent
        The original (immutable) intent.
    taint_applied : bool
        True if the taint downgrade (ALLOW → CONFIRM) was applied.
    reason : str
        Human-readable explanation for logging / audit.
    """
    level:         PermissionLevel
    intent:        StructuredIntent
    taint_applied: bool
    reason:        str


# (Toast implementation lives in ui/toasts.py — imported above)


# ---------------------------------------------------------------------------
# Gatekeeper
# ---------------------------------------------------------------------------

class PermissionGatekeeper:
    """
    Evaluates a StructuredIntent against the 4-level permission matrix,
    applies taint analysis, and — when required — blocks on async user
    confirmation via Windows Toast.

    Thread-safety: evaluate() is an async coroutine and must be called from
    the Orchestrator's asyncio event loop (Process 2).  The permission table
    itself is dict-based and protected only by the GIL; do not mutate it from
    multiple threads simultaneously.
    """

    def __init__(
        self,
        table: dict[str, PermissionLevel] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        table : optional custom permission table.
            If None, _DEFAULT_PERMISSION_TABLE is used.
        """
        self._table: dict[str, PermissionLevel] = dict(
            table if table is not None else _DEFAULT_PERMISSION_TABLE
        )
        # Runtime user overrides (set_override() / clear_override())
        self._overrides: dict[str, PermissionLevel] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_override(self, action: str, level: PermissionLevel) -> None:
        """Persistently override a single action's permission level at runtime."""
        self._overrides[action] = level
        log.info("Permission override set: %s → %s", action, level.value)

    def clear_override(self, action: str) -> None:
        """Remove a runtime override, falling back to the default table."""
        self._overrides.pop(action, None)
        log.info("Permission override cleared: %s", action)

    def resolve_base_level(self, action: str) -> PermissionLevel:
        """
        Look up the base permission level for *action* (before taint analysis).
        Override table takes precedence over the default table.
        """
        return self._overrides.get(action) or self._table.get(action, DEFAULT_LEVEL)

    # ------------------------------------------------------------------
    # Taint Analysis
    # ------------------------------------------------------------------

    @staticmethod
    def apply_taint(
        level: PermissionLevel,
        origin: IntentOrigin,
    ) -> tuple[PermissionLevel, bool]:
        """
        Rule (docs/specs/02_security.md §3):
            If origin is INDIRECT_WEB_CONTENT, instantly downgrade ALLOW → CONFIRM.

        DENY and BLOCK are never upgraded by taint; they can only stay or
        become more restrictive.

        Returns
        -------
        (new_level, taint_applied)
        """
        if origin not in _TAINTED_ORIGINS:
            return level, False

        if level == PermissionLevel.ALLOW:
            log.warning(
                "Taint analysis: downgrading ALLOW → CONFIRM "
                "(origin=%s is untrusted web content).",
                origin.value,
            )
            return PermissionLevel.CONFIRM, True

        # CONFIRM, DENY, BLOCK remain unchanged — already restrictive enough
        return level, False

    # ------------------------------------------------------------------
    # Main evaluation pipeline
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        intent: StructuredIntent,
        event_queue: "multiprocessing.Queue[dict[str, Any]] | None" = None,
    ) -> GatekeeperVerdict:
        """
        Full evaluation pipeline:

        1. Low-confidence guard     → auto-DENY below threshold
        2. Base level lookup        → override table → default table
        3. Taint analysis           → INDIRECT_WEB_CONTENT downgrades ALLOW
        4. BLOCK / DENY short-circuit → return immediately
        5. CONFIRM → async Toast via ui.toasts.request_action_confirmation
                     (pushes CONFIRMATION_RESULT into event_queue)
        6. ALLOW → approved

        Parameters
        ----------
        intent : StructuredIntent
            The validated intent to evaluate.
        event_queue : multiprocessing.Queue | None
            The shared IPC queue.  When provided, CONFIRMATION_RESULT dicts
            from Toast callbacks are pushed into it so the Orchestrator dispatch
            loop can audit-log them independently of the await chain.
            Pass None only in unit tests (the Toast is mocked anyway).

        This coroutine may suspend at step 5 waiting for the user's Toast
        response (up to 30 seconds).
        """

        # ── 1. Confidence guard ────────────────────────────────────────
        if intent.confidence < CONFIDENCE_THRESHOLD:
            reason = (
                f"Confidence {intent.confidence:.2f} < threshold "
                f"{CONFIDENCE_THRESHOLD:.2f} — auto-DENY."
            )
            log.info("Gatekeeper DENY [low-confidence]: %s", reason)
            return GatekeeperVerdict(
                level=PermissionLevel.DENY,
                intent=intent,
                taint_applied=False,
                reason=reason,
            )

        # ── 2. Base level lookup ───────────────────────────────────────
        base_level = self.resolve_base_level(intent.action)
        log.debug(
            "Gatekeeper base level: action=%r → %s", intent.action, base_level.value
        )

        # ── 3. Taint analysis ──────────────────────────────────────────
        effective_level, taint_applied = self.apply_taint(base_level, intent.origin)

        # ── 4. Hard stops ──────────────────────────────────────────────
        if effective_level == PermissionLevel.BLOCK:
            reason = (
                f"action={intent.action!r} is hard-BLOCKED. "
                "Suspicion counter incremented."
            )
            log.critical("Gatekeeper BLOCK: %s", reason)
            return GatekeeperVerdict(
                level=PermissionLevel.BLOCK,
                intent=intent,
                taint_applied=taint_applied,
                reason=reason,
            )

        if effective_level == PermissionLevel.DENY:
            reason = f"action={intent.action!r} is DENY in permission table."
            log.info("Gatekeeper DENY: %s", reason)
            return GatekeeperVerdict(
                level=PermissionLevel.DENY,
                intent=intent,
                taint_applied=taint_applied,
                reason=reason,
            )

        # ── 5. CONFIRM — show Toast and wait for user ──────────────────
        if effective_level == PermissionLevel.CONFIRM:
            log.info(
                "Gatekeeper CONFIRM: action=%r origin=%s — awaiting Toast response.",
                intent.action,
                intent.origin.value,
            )

            # Delegate entirely to ui.toasts which owns the dual-bridge
            # (asyncio.Future + multiprocessing.Queue.put_nowait).
            # A sentinel no-op queue is used when event_queue is not provided
            # (test context only — never in production).
            _queue = event_queue if event_queue is not None else multiprocessing.Queue()
            confirmation: ConfirmationResult = await request_action_confirmation(
                intent=intent,
                event_queue=_queue,
            )

            if confirmation.approved:
                reason = "User approved via Toast."
                log.info("Toast approved: action=%r intent_id=%s", intent.action, confirmation.intent_id)
                return GatekeeperVerdict(
                    level=PermissionLevel.ALLOW,
                    intent=intent,
                    taint_applied=taint_applied,
                    reason=reason,
                )
            else:
                reason = (
                    "Toast timed out — DENY."
                    if confirmation.timed_out
                    else "User denied via Toast."
                )
                log.info("Toast not approved: action=%r reason=%r", intent.action, reason)
                return GatekeeperVerdict(
                    level=PermissionLevel.DENY,
                    intent=intent,
                    taint_applied=taint_applied,
                    reason=reason,
                )

        # ── 6. ALLOW ───────────────────────────────────────────────────
        reason = f"action={intent.action!r} is ALLOW — executing directly."
        log.debug("Gatekeeper ALLOW: %s", reason)
        return GatekeeperVerdict(
            level=PermissionLevel.ALLOW,
            intent=intent,
            taint_applied=taint_applied,
            reason=reason,
        )

class PermissionManager:
    def __init__(self):
        self.matrix = {
            "spotify.play": "ALLOW",
            "spotify.delete_playlist": "CONFIRM",
            "gmail.read": "ALLOW",
            "gmail.send": "CONFIRM",
            "filesystem.delete": "CONFIRM",
            "windows.format_drive": "DENY"
        }

    def evaluate(self, intent) -> str:
        key = f"{intent.application}.{intent.action}"
        base_permission = self.matrix.get(key, "DENY")
        
        if intent.origin == "INDIRECT_WEB_CONTENT" and base_permission == "ALLOW":
            return "CONFIRM"
            
        return base_permission
