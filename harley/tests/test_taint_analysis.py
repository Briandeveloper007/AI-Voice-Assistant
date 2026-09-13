"""
tests/test_taint_analysis.py - Pytest suite for the Permission Gatekeeper
Verifies taint analysis, the 4-level matrix, and confidence gating.
Run with: .venv\Scripts\pytest tests/test_taint_analysis.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from commands.schema import (
    IntentOrigin,
    PermissionLevel,
    StructuredIntent,
    intent_from_llm_json,
)
from security.permissions import (
    CONFIDENCE_THRESHOLD,
    DEFAULT_LEVEL,
    GatekeeperVerdict,
    PermissionGatekeeper,
)
from ui.toasts import ConfirmationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_intent(
    action: str = "WEB_SEARCH",
    intent: str = "SEARCH",
    origin: IntentOrigin = IntentOrigin.DIRECT_USER,
    confidence: float = 1.0,
    parameters: dict[str, Any] | None = None,
) -> StructuredIntent:
    return StructuredIntent(
        intent=intent,
        application="Harley",
        action=action,
        origin=origin,
        confidence=confidence,
        parameters=parameters or {},
    )


def _make_result(
    intent: StructuredIntent,
    approved: bool = True,
    timed_out: bool = False,
) -> ConfirmationResult:
    return ConfirmationResult(
        approved=approved,
        timed_out=timed_out,
        intent_id="test-uuid",
        action=intent.action,
        origin=intent.origin.value,
    )


# ===========================================================================
# 1. StructuredIntent schema
# ===========================================================================

class TestStructuredIntentSchema:

    def test_valid_intent_constructs(self) -> None:
        intent = _make_intent()
        assert intent.action == "WEB_SEARCH"
        assert intent.origin == IntentOrigin.DIRECT_USER

    def test_action_must_be_upper_snake(self) -> None:
        with pytest.raises(Exception, match="UPPER_SNAKE_CASE"):
            _make_intent(action="web_search")

    def test_action_with_spaces_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_intent(action="OPEN FILE")

    def test_action_leading_digit_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_intent(action="1OPEN_APP")

    def test_confidence_bounds(self) -> None:
        with pytest.raises(Exception):
            _make_intent(confidence=1.5)
        with pytest.raises(Exception):
            _make_intent(confidence=-0.1)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(Exception):
            StructuredIntent(
                intent="SEARCH",
                application="Harley",
                action="WEB_SEARCH",
                parameters={},
                origin=IntentOrigin.DIRECT_USER,
                malicious_field="rm -rf /",  # type: ignore
            )

    def test_intent_is_frozen(self) -> None:
        intent = _make_intent()
        with pytest.raises(Exception):
            intent.action = "TAMPERED"  # type: ignore[misc]

    def test_chrome_extension_auto_retags_to_indirect(self) -> None:
        intent = _make_intent(origin=IntentOrigin.CHROME_EXTENSION)
        assert intent.origin == IntentOrigin.INDIRECT_WEB_CONTENT

    def test_intent_from_llm_json_valid(self) -> None:
        raw = {"intent": "SEARCH", "action": "WEB_SEARCH",
               "origin": "DIRECT_USER", "confidence": 0.95}
        assert intent_from_llm_json(raw).action == "WEB_SEARCH"

    def test_intent_from_llm_json_invalid_origin(self) -> None:
        with pytest.raises(Exception):
            intent_from_llm_json({"intent": "S", "action": "WEB_SEARCH",
                                  "origin": "UNKNOWN_ORIGIN"})


# ===========================================================================
# 2. Taint Analysis (pure static method)
# ===========================================================================

class TestTaintAnalysis:

    def test_taint_downgrades_allow_to_confirm(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.ALLOW, IntentOrigin.INDIRECT_WEB_CONTENT)
        assert level == PermissionLevel.CONFIRM
        assert applied is True

    def test_taint_does_not_upgrade_confirm(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.CONFIRM, IntentOrigin.INDIRECT_WEB_CONTENT)
        assert level == PermissionLevel.CONFIRM
        assert applied is False

    def test_taint_does_not_upgrade_deny(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.DENY, IntentOrigin.INDIRECT_WEB_CONTENT)
        assert level == PermissionLevel.DENY
        assert applied is False

    def test_taint_does_not_affect_block(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.BLOCK, IntentOrigin.INDIRECT_WEB_CONTENT)
        assert level == PermissionLevel.BLOCK
        assert applied is False

    def test_no_taint_for_direct_user(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.ALLOW, IntentOrigin.DIRECT_USER)
        assert level == PermissionLevel.ALLOW
        assert applied is False

    def test_no_taint_for_system_origin(self) -> None:
        level, applied = PermissionGatekeeper.apply_taint(
            PermissionLevel.ALLOW, IntentOrigin.SYSTEM)
        assert level == PermissionLevel.ALLOW
        assert applied is False


# ===========================================================================
# 3. Gatekeeper.evaluate() - full async pipeline
# ===========================================================================

@pytest.fixture
def gatekeeper() -> PermissionGatekeeper:
    return PermissionGatekeeper()


@pytest.fixture(autouse=True)
def _mock_toast(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch both import sites so no real Toast ever fires in tests."""
    mock = AsyncMock(
        return_value=ConfirmationResult(
            approved=True, timed_out=False,
            intent_id="test-uuid", action="PLACEHOLDER", origin="DIRECT_USER",
        )
    )
    monkeypatch.setattr("ui.toasts.request_action_confirmation", mock)
    monkeypatch.setattr("security.permissions.request_action_confirmation", mock)
    return mock


class TestGatekeeperEvaluate:

    def test_allow_direct_user_web_search(self, gatekeeper: PermissionGatekeeper) -> None:
        verdict = asyncio.run(gatekeeper.evaluate(_make_intent()))
        assert verdict.level == PermissionLevel.ALLOW
        assert verdict.taint_applied is False

    def test_taint_fires_and_toast_approves(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(origin=IntentOrigin.INDIRECT_WEB_CONTENT)
        _mock_toast.return_value = _make_result(intent, approved=True)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.taint_applied is True
        assert verdict.level == PermissionLevel.ALLOW
        _mock_toast.assert_awaited_once()

    def test_taint_fires_and_toast_denies(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(origin=IntentOrigin.INDIRECT_WEB_CONTENT)
        _mock_toast.return_value = _make_result(intent, approved=False)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.taint_applied is True
        assert verdict.level == PermissionLevel.DENY

    def test_indirect_web_already_confirm_goes_to_toast(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(action="SEND_EMAIL",
                              origin=IntentOrigin.INDIRECT_WEB_CONTENT)
        _mock_toast.return_value = _make_result(intent, approved=True)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.taint_applied is False
        assert verdict.level == PermissionLevel.ALLOW

    def test_deny_action_immediately(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        verdict = asyncio.run(gatekeeper.evaluate(_make_intent(action="EXEC_SHELL")))
        assert verdict.level == PermissionLevel.DENY
        _mock_toast.assert_not_awaited()

    def test_deny_not_affected_by_taint(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(action="EXEC_SHELL",
                              origin=IntentOrigin.INDIRECT_WEB_CONTENT)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.level == PermissionLevel.DENY
        _mock_toast.assert_not_awaited()

    def test_block_action_immediately(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        verdict = asyncio.run(
            gatekeeper.evaluate(_make_intent(action="ELEVATE_PRIVILEGE")))
        assert verdict.level == PermissionLevel.BLOCK
        _mock_toast.assert_not_awaited()

    def test_block_from_indirect_web_still_blocked(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(action="ELEVATE_PRIVILEGE",
                              origin=IntentOrigin.INDIRECT_WEB_CONTENT)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.level == PermissionLevel.BLOCK
        _mock_toast.assert_not_awaited()

    def test_low_confidence_auto_deny(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(confidence=CONFIDENCE_THRESHOLD - 0.01)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.level == PermissionLevel.DENY
        _mock_toast.assert_not_awaited()

    def test_exactly_at_threshold_passes(self, gatekeeper: PermissionGatekeeper) -> None:
        verdict = asyncio.run(gatekeeper.evaluate(
            _make_intent(confidence=CONFIDENCE_THRESHOLD)))
        assert verdict.level == PermissionLevel.ALLOW

    def test_unknown_action_uses_default_confirm(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(action="UNKNOWN_FUTURE_ACTION")
        _mock_toast.return_value = _make_result(intent, approved=True)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert DEFAULT_LEVEL == PermissionLevel.CONFIRM
        _mock_toast.assert_awaited_once()
        assert verdict.level == PermissionLevel.ALLOW

    def test_override_changes_level(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        gatekeeper.set_override("WEB_SEARCH", PermissionLevel.CONFIRM)
        intent = _make_intent()
        _mock_toast.return_value = _make_result(intent, approved=True)
        asyncio.run(gatekeeper.evaluate(intent))
        _mock_toast.assert_awaited_once()
        gatekeeper.clear_override("WEB_SEARCH")

    def test_clear_override_restores_default(self, gatekeeper: PermissionGatekeeper) -> None:
        gatekeeper.set_override("WEB_SEARCH", PermissionLevel.DENY)
        gatekeeper.clear_override("WEB_SEARCH")
        assert gatekeeper.resolve_base_level("WEB_SEARCH") == PermissionLevel.ALLOW

    def test_timed_out_result_maps_to_deny(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(action="SEND_EMAIL")
        _mock_toast.return_value = _make_result(intent, approved=False, timed_out=True)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.level == PermissionLevel.DENY
        assert "timed out" in verdict.reason.lower()

    def test_chrome_extension_triggers_taint(
        self, gatekeeper: PermissionGatekeeper, _mock_toast: AsyncMock
    ) -> None:
        intent = _make_intent(origin=IntentOrigin.CHROME_EXTENSION)
        assert intent.origin == IntentOrigin.INDIRECT_WEB_CONTENT
        _mock_toast.return_value = _make_result(intent, approved=False)
        verdict = asyncio.run(gatekeeper.evaluate(intent))
        assert verdict.taint_applied is True
        assert verdict.level == PermissionLevel.DENY
