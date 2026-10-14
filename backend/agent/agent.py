"""Agent turn loop with tool calling (spec §59–61, §99).

Each step streams the model's reply; visible text goes straight to the client
(and to TTS), tool calls are collected, run, and their results fed back for
the next step. Independent reads run in parallel. Approval-level tools pause
inside their own implementation until the user decides.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from backend.agent.prompts import stamp, system_prompt
from backend.agent.sessions import Conversation
from backend.agent.toolcalls import OPEN, ReplyFilter
from backend.agent.tools import (
    ApprovalRequest,
    Permission,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from backend.voice.llm import GenerationStats, LLMProvider, Message

log = structlog.get_logger(__name__)

Emit = Callable[..., Awaitable[None]]
RequestApproval = Callable[[ApprovalRequest], Awaitable[bool]]

MAX_STEPS = 6
# Tool output from earlier turns is shortened so history doesn't crowd the context.
OLD_TOOL_RESULT_CHARS = 1_200
# "Let me check your email." with no call after it: the model announced an
# action and stopped. Its turn is then continued, forced into a tool call.
_ANNOUNCE = re.compile(
    r"^(let me|i'?ll|i will|i'?m going to|one (moment|sec)|checking|looking|give me a (moment|sec))\b", re.I
)


async def _deny(_: ApprovalRequest) -> bool:
    return False


class Agent:
    def __init__(
        self,
        llm: LLMProvider,
        tools: ToolRegistry | None = None,
        profile: Callable[[], dict[str, str]] = dict,
    ) -> None:
        self.llm = llm
        self.tools = tools or ToolRegistry()
        # Who the user is (name, email), for signing drafts and the like.
        self.profile = profile

    async def run_turn(
        self,
        conversation: Conversation,
        user_text: str,
        emit: Emit,
        cancel: threading.Event,
        request_approval: RequestApproval = _deny,
    ) -> None:
        turn_start = len(conversation.messages)
        conversation.add({"role": "user", "content": stamp(user_text)})
        ctx = ToolContext(emit, request_approval, cancel)
        await emit("agent.thinking")

        visible: list[str] = []
        activity: list[dict[str, Any]] = []
        first_token_ms: int | None = None
        total_ms = tokens = 0
        cancelled = False

        for step in range(MAX_STEPS):
            # On the last step, withhold tools so the model has to answer.
            schemas = self.tools.schemas() if len(self.tools) and step < MAX_STEPS - 1 else None
            messages = self._prompt(conversation, turn_start)
            stats = GenerationStats()
            reply = ReplyFilter()
            # Text from an earlier step (e.g. "Let me check.") ends a sentence.
            separator = "\n" if visible and visible[-1] else ""
            async for delta in self.llm.stream(messages, cancel, stats, tools=schemas):
                if new := reply.feed(delta):
                    await emit("assistant.text.delta", delta=separator + new)
                    separator = ""
            text, calls = reply.finish()
            calls += [
                ToolCall(c["name"], c["arguments"], thought_signature=c["thought_signature"])
                for c in stats.tool_calls
            ]
            native = getattr(self.llm, "native_tools", False)
            if (
                not calls
                and not native
                and schemas
                and not cancel.is_set()
                and len(text) < 120
                and _ANNOUNCE.match(text)
            ):
                calls = await self._force_call(messages, text, schemas, cancel, stats)

            if first_token_ms is None and stats.first_token_s is not None:
                first_token_ms = round(stats.first_token_s * 1000)
            total_ms += round(stats.total_s * 1000)
            tokens += stats.tokens
            visible.append(text)
            cancelled = stats.cancelled or cancel.is_set()

            if cancelled or not calls:
                # A cancelled turn keeps what was said so far, so context stays truthful.
                if text:
                    conversation.add({"role": "assistant", "content": text})
                break

            conversation.add(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "function": {"name": c.name, "arguments": c.arguments},
                            **({"thought_signature": c.thought_signature} if c.thought_signature else {}),
                        }
                        for c in calls
                    ],
                }
            )
            for result in await self._run_calls(calls, ctx, emit):
                conversation.add({"role": "tool", "content": result.content})
                activity.append(
                    {"tool": result.call.name, "ok": result.ok, "summary": result.summary, "ms": result.ms}
                )
            if cancel.is_set():
                cancelled = True
                break

        final = "\n".join(t for t in visible if t).strip()
        metrics = {"first_token_ms": first_token_ms, "total_ms": total_ms, "tokens": tokens}
        await emit("assistant.text.final", text=final, cancelled=cancelled, metrics=metrics, tools=activity)
        log.info(
            "agent.turn",
            conversation_id=conversation.id,
            model=self.llm.name,
            cancelled=cancelled,
            tools=[(a["tool"], a["ok"], a["ms"]) for a in activity],
            **metrics,
        )

    async def warm(self) -> None:
        """Load the model and cache the system prompt + tool list."""
        prime = getattr(self.llm, "prime", None)
        if prime is None:
            await self.llm.load()
            return
        messages = [{"role": "system", "content": self._system()}, {"role": "user", "content": "hi"}]
        await prime(messages, self.tools.schemas() if len(self.tools) else None)

    async def _force_call(
        self,
        messages: list[Message],
        text: str,
        schemas: list[dict[str, Any]],
        cancel: threading.Event,
        stats: GenerationStats,
    ) -> list[ToolCall]:
        forced = f"{OPEN}\n<function="
        reply = ReplyFilter()
        reply.feed(forced)
        extra = GenerationStats()
        async for delta in self.llm.stream(
            messages, cancel, extra, tools=schemas, prefix=f"{text}\n\n{forced}"
        ):
            reply.feed(delta)
        stats.tokens += extra.tokens
        stats.total_s += extra.total_s
        _, calls = reply.finish()
        log.info("agent.forced_call", tools=[c.name for c in calls])
        return calls

    def _prompt(self, conversation: Conversation, turn_start: int) -> list[Message]:
        history: list[Message] = []
        for i, m in enumerate(conversation.messages):
            content = m.get("content", "")
            if m["role"] == "tool" and i < turn_start and len(content) > OLD_TOOL_RESULT_CHARS:
                m = {
                    **m,
                    "content": content[:OLD_TOOL_RESULT_CHARS]
                    + " …[truncated; call the tool again for details]",
                }
            history.append(m)
        return [{"role": "system", "content": self._system()}, *history]

    def _system(self) -> str:
        return system_prompt(tools=len(self.tools) > 0, profile=self.profile())

    async def _run_calls(self, calls: list[ToolCall], ctx: ToolContext, emit: Emit) -> list[ToolResult]:
        async def run(call: ToolCall) -> ToolResult:
            tool = self.tools.get(call.name)
            await emit(
                "tool.started", call_id=call.id, tool=call.name, label=tool.label if tool else call.name
            )
            result = await self.tools.run(call, ctx)
            await emit(
                "tool.completed",
                call_id=call.id,
                tool=call.name,
                ok=result.ok,
                summary=result.summary,
                ms=result.ms,
            )
            return result

        reads = all(
            (t := self.tools.get(c.name)) is not None and t.permission == Permission.READ for c in calls
        )
        if reads:
            return list(await asyncio.gather(*(run(c) for c in calls)))
        return [await run(c) for c in calls]  # anything with side effects runs in order
