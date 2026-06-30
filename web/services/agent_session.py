"""Per-WebSocket-connection `RemoteBrowserUseAgent` wrapper.

Holds a single agent instance for the lifetime of one chat WebSocket
connection, and serialises turn submission so that:

  * a new user message **cancels** the in-flight turn and starts
    a new one (matches ChatGPT / Claude.ai behavior — selected in
    the plan);
  * a ``cancel`` control frame just cancels the in-flight turn;
  * the underlying `agent.run()` is an `async def ... yield event`
    generator, which is naturally cancellable at the next `await`.

The `submit` method is itself an `async def` returning an async
iterator; the WS handler drives that iterator and forwards each
event to the client (via `event_bridge.stream_agent_events`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Dict, Optional

from web.config import Settings

logger = logging.getLogger("web.agent_session")


class AgentSession:
    """One agent per WebSocket connection, with cancel-and-replace semantics."""

    def __init__(self, cfg: Settings, user_token: str, page_url: str, client_type: str = None):
        self._cfg = cfg
        self._user_token = user_token
        self._page_url = page_url
        # Defaults to "browser" so legacy code that doesn't pass it
        # keeps working. The wxmp client passes "wxmp" so the
        # agent's MCP meta carries the right value.
        self._client_type = client_type or "browser"
        self._conn_id = uuid.uuid4().hex[:8]

        # Late import — the agent pulls in agentscope / mcp, which is
        # slow to import. Keep it inside __init__ so cold-start of the
        # web server doesn't pay that cost until the first user shows up.
        from agent.RemoteBrowserUseAgent import RemoteBrowserUseAgent

        self._agent = RemoteBrowserUseAgent(
            mcp_server_url=f"http://{cfg.mcp_host}:{cfg.mcp_port}/mcp",
            browser_use_skill_dir=self._resolve_skill_dir(),
            model_name=cfg.llm_model_name,
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            session_id=f"web_{self._conn_id}",
            reasoning_effort=cfg.llm_reasoning_effort,
        )
        # The currently running submit-task, if any. Stored so we can
        # cancel it on a new submit() or on explicit cancel().
        self._current: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        logger.info(
            "AgentSession[%s] created for client_type=%s user_token=%s page_url=%s",
            self._conn_id, self._client_type, user_token, page_url,
        )

    def _resolve_skill_dir(self) -> str:
        """Locate skills/chat_map relative to the project root.

        web/ is one level under the project root, so ../skills/chat_map
        is the canonical path. We resolve to absolute so the agent
        doesn't depend on CWD.
        """
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent.parent
        return str(project_root / "skills" / "chat_map")

    async def submit(
        self,
        user_input: str,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Run the agent on a user turn, cancelling any prior in-flight turn.

        Yields JSON-serializable dicts (the WS protocol frames defined
        in `event_bridge`). The caller is expected to drain the entire
        iterator; if the caller breaks out early, the underlying task
        is cancelled.
        """
        # Cancel any in-flight turn first. We do this OUTSIDE the lock
        # to avoid holding the lock across the await on cancellation,
        # but the lock still serialises the "decide to start a new
        # turn" part so we can't race two new turns.
        async with self._lock:
            if self._current and not self._current.done():
                logger.info(
                    "AgentSession[%s] cancelling prior turn (task=%s)",
                    self._conn_id, id(self._current),
                )
                self._current.cancel()
                try:
                    await self._current
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            # The submit task is responsible for draining agent.run()
            # and pushing the JSON frames into an asyncio.Queue that
            # this method's caller reads from.
            queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=512)
            sentinel = object()  # marks "stream finished"

            async def _pump() -> None:
                try:
                    # Late import to avoid pulling in agentscope at module load.
                    from web.services.event_bridge import stream_agent_events

                    async for frame in stream_agent_events(
                        self._agent.run(
                            user_token=self._user_token,
                            page_url=self._page_url,
                            user_input=user_input,
                            client_type=self._client_type,
                        ),
                    ):
                        await queue.put(frame)
                except asyncio.CancelledError:
                    # Make sure the consumer sees a terminal event.
                    await queue.put({"type": "reply_end"})
                    raise
                finally:
                    await queue.put(sentinel)  # type: ignore[arg-type]

            self._current = asyncio.create_task(_pump(), name=f"agent-{self._conn_id}")

        # Drain the queue and yield to the caller. We don't hold the
        # lock here — the new turn is in flight, and a subsequent
        # submit() will acquire the lock and cancel this one.
        while True:
            item = await queue.get()
            if item is sentinel:
                return
            yield item  # type: ignore[misc]

    async def cancel(self) -> None:
        """Cancel the in-flight turn (if any) without starting a new one."""
        if self._current and not self._current.done():
            logger.info(
                "AgentSession[%s] cancel() — cancelling task %s",
                self._conn_id, id(self._current),
            )
            self._current.cancel()
            try:
                await self._current
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
