import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from kalshi_trader.agents.base import BaseAgent


def _make_end_turn_response(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "tu_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    block.id = tool_use_id
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_base_agent_end_turn_returns_text():
    agent = BaseAgent(
        tools=[],
        handlers={},
        system_prompt="You are a test agent.",
    )
    mock_create = AsyncMock(return_value=_make_end_turn_response("hello world"))
    with patch.object(agent._client.messages, "create", mock_create):
        result = await agent.run("say hello")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_base_agent_dispatches_tool_call():
    called_with = {}

    async def my_tool(x: int):
        called_with["x"] = x
        return {"result": x * 2}

    agent = BaseAgent(
        tools=[{"name": "my_tool", "description": "...", "input_schema": {}}],
        handlers={"my_tool": my_tool},
        system_prompt="Use tools.",
    )
    tool_resp = _make_tool_use_response("my_tool", {"x": 5})
    end_resp = _make_end_turn_response("done")

    mock_create = AsyncMock(side_effect=[tool_resp, end_resp])
    with patch.object(agent._client.messages, "create", mock_create):
        result = await agent.run("call my_tool")

    assert called_with["x"] == 5
    assert result == "done"


@pytest.mark.asyncio
async def test_base_agent_unknown_tool_returns_error():
    agent = BaseAgent(tools=[], handlers={}, system_prompt="test")
    tool_resp = _make_tool_use_response("nonexistent", {})
    end_resp = _make_end_turn_response("ok")

    mock_create = AsyncMock(side_effect=[tool_resp, end_resp])
    with patch.object(agent._client.messages, "create", mock_create):
        await agent.run("go")

    # Verify the tool_result message sent back contains an error
    second_call_messages = mock_create.call_args_list[1][1]["messages"]
    last_message = second_call_messages[-1]
    assert last_message["role"] == "user"
    content = last_message["content"][0]
    result_data = json.loads(content["content"])
    assert "error" in result_data
