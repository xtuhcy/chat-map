"""Translate agentscope reply events to chat WebSocket JSON frames.

The chat WebSocket protocol (C↔S) lives here too — keeping it next
to the translator makes it easy to keep both sides in sync.

Server → Client event types:
  reply_start         — turn boundary (LLM started)
  reply_end           — turn boundary (LLM finished, normal or cancelled)
  text                — streamed token delta
  thinking            — reasoning delta (collapsed/hidden by default)
  tool_start          — tool call started
  tool_call_delta     — tool call argument streaming
  tool_call_end       — tool call closed
  tool_result_text    — streamed tool result text
  tool_result_data    — streamed tool result data (image/file)
  tool_result_end     — tool result closed
  model_call_end      — usage stats (input/output tokens)
  error               — exception
  pong                — heartbeat reply

Client → Server:
  {"type": "user",   "content": "..."}  — submit a turn
  {"type": "cancel"}                     — cancel the in-flight turn
  {"type": "ping"}                       — heartbeat
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional

from agentscope.event import (
    DataBlockDeltaEvent,
    ModelCallEndEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)

logger = logging.getLogger("web.event_bridge")


async def stream_agent_events(
    agent_iter: AsyncIterator[Any],
) -> AsyncIterator[Dict[str, Any]]:
    """Yield JSON-safe dicts from a `RemoteBrowserUseAgent.run()` iterator.

    The iterator comes from `agent.run(...)` (an `async def ... yield event`).
    We map each event class to one of the WS protocol types above; unknown
    events are silently dropped (with a debug log) so the protocol stays
    stable when agentscope adds new event types.
    """
    reply_started = False
    try:
        async for ev in agent_iter:
            if isinstance(ev, ReplyStartEvent):
                reply_started = True
                yield {"type": "reply_start"}
            elif isinstance(ev, ReplyEndEvent):
                yield {"type": "reply_end"}
                reply_started = False
            elif isinstance(ev, TextBlockDeltaEvent):
                yield {"type": "text", "delta": ev.delta}
            elif isinstance(ev, ThinkingBlockDeltaEvent):
                # Reasoning — collapsed by default in the UI.
                yield {"type": "thinking", "delta": ev.delta}
            elif isinstance(ev, ToolCallStartEvent):
                yield {
                    "type": "tool_start",
                    "id": getattr(ev, "tool_call_id", None),
                    "name": getattr(ev, "tool_call_name", None),
                }
            elif isinstance(ev, ToolCallDeltaEvent):
                yield {
                    "type": "tool_call_delta",
                    "id": getattr(ev, "tool_call_id", None),
                    "delta": ev.delta,
                }
            elif isinstance(ev, ToolCallEndEvent):
                yield {
                    "type": "tool_call_end",
                    "id": getattr(ev, "tool_call_id", None),
                }
            elif isinstance(ev, ToolResultStartEvent):
                yield {
                    "type": "tool_result_start",
                    "id": getattr(ev, "tool_call_id", None),
                    "name": getattr(ev, "tool_call_name", None),
                }
            elif isinstance(ev, ToolResultTextDeltaEvent):
                yield {
                    "type": "tool_result_text",
                    "id": getattr(ev, "tool_call_id", None),
                    "delta": ev.delta,
                }
            elif isinstance(ev, ToolResultDataDeltaEvent):
                yield {
                    "type": "tool_result_data",
                    "id": getattr(ev, "tool_call_id", None),
                    "data": getattr(ev, "data", None),
                    "url": getattr(ev, "url", None),
                    "media_type": getattr(ev, "media_type", None),
                }
            elif isinstance(ev, ToolResultEndEvent):
                # state is a ToolResultState enum; serialise as its string
                # value (the model has use_enum_values=True so the str()
                # gives the same shape over the wire).
                state_val = ev.state
                yield {
                    "type": "tool_result_end",
                    "id": getattr(ev, "tool_call_id", None),
                    "state": state_val.value if hasattr(state_val, "value") else str(state_val),
                }
            elif isinstance(ev, ModelCallEndEvent):
                yield {
                    "type": "model_call_end",
                    "input_tokens": getattr(ev, "input_tokens", None),
                    "output_tokens": getattr(ev, "output_tokens", None),
                }
            else:
                logger.debug("Ignoring unknown event: %r", ev)
    except Exception as e:  # noqa: BLE001 — surface any error to the UI
        logger.exception("agent stream failed")
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
    finally:
        # Make sure the client always sees a terminal event so the UI
        # can stop the spinner / "thinking…" indicator even if the
        # upstream iterator was cancelled or short-circuited.
        if reply_started:
            yield {"type": "reply_end"}
