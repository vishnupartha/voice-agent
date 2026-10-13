"""Parse Qwen3.5's tool-call markup out of the streamed reply.

The model writes calls as

    <tool_call>
    <function=gmail.search>
    <parameter=query>
    from:professor
    </parameter>
    </function>
    </tool_call>

`ReplyFilter` passes ordinary text through as it streams (so it can be shown
and spoken immediately) and holds back anything that is, or might become, a
tool call.
"""

from __future__ import annotations

import re
from typing import Any

from backend.agent.tools import ToolCall

OPEN = "<tool_call>"
_CALL = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*(?:</tool_call>|$)", re.S)
_PARAM = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
# Thinking is disabled, but strip stray think tags if the model emits them.
_THINK = re.compile(r"</?think>")


class ReplyFilter:
    def __init__(self) -> None:
        self.text = ""  # visible text so far
        self._raw = ""
        self._in_call = False

    def feed(self, delta: str) -> str:
        """Returns the newly visible text for this delta (may be empty)."""
        self._raw += delta
        if self._in_call:
            return ""
        visible = _THINK.sub("", self._raw)
        if (i := visible.find(OPEN)) >= 0:
            self._in_call = True
            visible = visible[:i]
        else:
            # Hold back a trailing fragment that could be the start of "<tool_call>".
            for k in range(min(len(OPEN) - 1, len(visible)), 0, -1):
                if OPEN.startswith(visible[-k:]):
                    visible = visible[:-k]
                    break
        new = visible[len(self.text) :]
        self.text = visible
        return new

    def finish(self) -> tuple[str, list[ToolCall]]:
        raw = _THINK.sub("", self._raw)
        calls = [ToolCall(name, _parse_params(body)) for name, body in _CALL.findall(raw)]
        text = raw.split(OPEN, 1)[0].strip() if calls or OPEN in raw else raw.strip()
        return text, calls


def _parse_params(body: str) -> dict[str, Any]:
    # Values stay raw text here; ToolRegistry coerces them by the tool's schema.
    return {name: value.strip() for name, value in _PARAM.findall(body)}
