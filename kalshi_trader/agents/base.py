from __future__ import annotations
import json
from typing import Any, Callable, Coroutine
import anthropic


class BaseAgent:
    def __init__(
        self,
        tools: list[dict],
        handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]],
        system_prompt: str,
        model: str = "claude-sonnet-4-6",
        max_iterations: int = 30,
    ) -> None:
        self._client = anthropic.AsyncAnthropic()
        self._tools = tools
        self._handlers = handlers
        self._system = system_prompt
        self._model = model
        self._max_iterations = max_iterations

    async def run(self, user_message: str) -> str:
        messages: list[dict] = [{"role": "user", "content": user_message}]

        for _ in range(self._max_iterations):
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=self._system,
                tools=self._tools,
                messages=messages,
            )

            if resp.stop_reason == "end_turn":
                for block in resp.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if resp.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                handler = self._handlers.get(block.name)
                if handler is None:
                    payload = {"error": f"Unknown tool: {block.name}"}
                else:
                    try:
                        payload = await handler(**block.input)
                    except Exception as exc:
                        payload = {"error": str(exc)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, default=str),
                })
            messages.append({"role": "user", "content": tool_results})

        return ""
