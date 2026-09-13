import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from commands.schema import IntentOrigin, StructuredIntent
from intelligence.ai_provider import plan_action, summarize_web_content

pytestmark = pytest.mark.asyncio


@patch("intelligence.ai_provider.AsyncOpenAI")
async def test_summarize_web_content(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "This is a clean summary."
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    
    untrusted = "Some web text with <script>alert(1)</script>"
    result = await summarize_web_content(untrusted)
    
    assert result == "This is a clean summary."
    mock_client.chat.completions.create.assert_called_once()
    
    # Verify tools were not provided to the API call
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "tools" not in call_kwargs
    assert "response_format" not in call_kwargs
    
    # Verify the input was wrapped properly
    messages = call_kwargs["messages"]
    assert any("<untrusted_web_content>" in msg["content"] and untrusted in msg["content"] for msg in messages if msg["role"] == "user")


@patch("intelligence.ai_provider.AsyncOpenAI")
async def test_plan_action_valid(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    
    valid_intent_json = {
        "intent": "WEB_SEARCH",
        "action": "WEB_SEARCH",
        "parameters": {"query": "test"},
        "origin": "DIRECT_USER",
        "confidence": 0.95
    }
    
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(valid_intent_json)
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    
    transcript = "search for test"
    result = await plan_action(transcript)
    
    assert isinstance(result, StructuredIntent)
    assert result.action == "WEB_SEARCH"
    assert result.origin == IntentOrigin.DIRECT_USER
    
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["response_format"] == {"type": "json_object"}
    
    # Check that schema was passed in system prompt
    messages = call_kwargs["messages"]
    sys_prompt = next(msg["content"] for msg in messages if msg["role"] == "system")
    assert "StructuredIntent" in sys_prompt


@patch("intelligence.ai_provider.AsyncOpenAI")
async def test_plan_action_invalid_raises_validation_error(mock_openai_class):
    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    
    # Invalid action (not UPPER_SNAKE_CASE)
    invalid_intent_json = {
        "intent": "bad intent",
        "action": "lower case action",
        "parameters": {},
        "origin": "DIRECT_USER"
    }
    
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(invalid_intent_json)
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    
    with pytest.raises(ValidationError):
        await plan_action("do something")
