"""
RemoteBrowserUseAgent2.py

Native-MCP-call-meta variant of RemoteBrowserUseAgent.py.

Differences from the header-based variant (previous v2):
  * No `PerRequestHeaderMCPClient` subclass, no `contextvars` — both
    removed entirely.
  * `page_url` is carried in `UserMsg.metadata["tool_call_meta"]`.
    `Toolkit.call_tool` reads it from the most recent user message
    and injects it as `kwargs["_meta"]`; `MCPTool.__call__` pops it
    and passes it as the `meta` kwarg of `session.call_tool(...)`.
    The MCP server reads it from `CallToolRequestParams.meta`.
  * `user_token` is still a static HTTP header (`X-User-Token`)
    embedded at construction time — it's per-instance, not
    per-request, so it has no business in the meta field.
  * The MCP client is a plain `MCPClient` — no subclassing needed.

Pair with a server that reads `meta` from the MCP request
(`ctx.request_context.meta` on `CallToolRequestParams`), instead of
from HTTP headers.
"""

import os
import asyncio
from agentscope.credential import OpenAICredential
from agentscope.event import *
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from dotenv import load_dotenv
from agentscope.agent import Agent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.tool import MCP_CALL_META_KEY, Toolkit

load_dotenv()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8766/mcp")

BROWSER_USE_SKILL = os.path.join(
    os.path.dirname(__file__), "..", "skills", "chat_map"
)

# Reasoning-model knobs. `Parameters.reasoning_effort` is a pydantic
# `Literal[...]` — pydantic raises a clear ValidationError at agent
# construction if the value isn't in the allowed set, so we don't have
# to pre-validate. We only need to coerce env-var strings into "off"
# vs. "one of the valid levels".
_REASONING_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
_REASONING_OFF_TOKENS = {"", "off", "false", "0", "none"}


def _normalize_reasoning_effort(value: str | None) -> str | None:
    """Coerce env-var / CLI strings into the form `Parameters` expects.

    "" / "off" / "false" / "0" / "none" → None (thinking stays off;
    `reasoning_effort` is NOT sent on the wire, so behaviour is
    identical to before this knob existed).
    "minimal" / "low" / "medium" / "high" / "xhigh" → pass through,
    the model turns on reasoning at the requested depth.
    Anything else → pass through; pydantic will reject with a clear
    ValidationError pointing at the bad value.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v in _REASONING_OFF_TOKENS:
        return None
    return v

SYSTEM_PROMPT = (
    "You are a helpful assistant controlling an AMap-based map page (map.html) "
    "via MCP tools. Use the provided map_* tools to interact with the map. "
    "The current page URL and user identity are pre-bound to this request — "
    "do NOT ask for them, and do NOT include them in tool calls.\n\n"
    "════════════════════════════════════════════════════════════════════\n"
    "**MANDATORY EXECUTION PROTOCOL** (read first; do NOT skip)\n"
    "════════════════════════════════════════════════════════════════════\n\n"
    "1. **Task Classifier** — On every user query, classify FIRST into:\n"
    "   A. 单点搜索 (e.g. \"找一下 X\")\n"
    "   B. **空间查询** (e.g. \"X 附近 N km 的 Y\", \"X 半径 N 内 Z 类\") — HIGHEST FREQUENCY, HIGHEST FAILURE RATE\n"
    "   C. 多点对比 (e.g. \"把 A、B、C 都标出来\")\n"
    "   D. 路径规划 (e.g. \"A 到 B 怎么走\")\n"
    "   E. 区域圈选 (e.g. \"把 X 商圈圈出来\")\n"
    "   → ANY query with \"附近/周边/半径/范围内/X 米/X km/内\" → MUST be B.\n\n"
    "2. **Spatial Query Standard Procedure (B 类强制)** — 5 steps, every one mandatory:\n"
    "   STEP 1: 查 §3.8 POI Type 编码表 — \"高中\" → type=\"141201\". **绝对禁止** keyword=\"高中\"。\n"
    "   STEP 2: map_geocode(中心地址, city) → 验证 lng/lat 范围\n"
    "   STEP 3: map_search_nearby(lng, lat, radius*1000, type=<编码>, city=<城市>,\n"
    "                              exclude_keywords=<§3.9 噪音词>)\n"
    "          → 读 count; ==0 走 §10.3 retry 链（6 级降级），不许直接放弃\n"
    "   STEP 4: 遍历 pois[] **全部**（不许截断到前 2-3 个），每个调\n"
    "          map_add_marker_with_info(lng=poi.location[0], lat=poi.location[1],\n"
    "                                    title=poi.name, poi=poi)\n"
    "   STEP 5: 任务完成判定 — map_list_overlays().markers 必须 == pois.length + 1，\n"
    "          不等 → 回到 STEP 4 重试。文字回复必须提到 count / filtered_out。\n\n"
    "3. **POI Type 强制规则** — 任何 \"X 类 / X 馆 / X 店\" 类查询：\n"
    "   - 先查 §3.8 类目编码表得到 type 编码\n"
    "   - **绝对禁止** 用 keyword=<类别中文名> 替代 type=<编码>\n"
    "   - 错误示范: keyword=\"高中\" 会返回\"清华高中部\"、\"新东方高中培训\"等噪音\n"
    "   - 正确做法: type=\"141201\" + keyword=\"\" + exclude_keywords=[\"培训\",\"复读\",\"驾校\",...]\n\n"
    "4. **任务完成判定** — 满足以下全部才能告诉用户\"完成\"：\n"
    "   □ 地图有可视变化（中心 marker + 圆圈 + 至少 1 个 POI marker）\n"
    "   □ map_list_overlays().markers == pois.length + 1\n"
    "   □ 文字回复提到 count / filtered_out / 噪音\n"
    "   □ 至少 1 个引导性追问\n\n"
    "════════════════════════════════════════════════════════════════════\n\n"
    "**Core verbs (when to use which)**:\n"
    "- map_geocode(address, city?) → resolve address to (lng, lat). Always pass "
    "`city` when the user mentions one; for ambiguous addresses prefer the "
    "city implied by context or the map's current center.\n"
    "- map_search_nearby(lng, lat, radius, type?, keyword?, city?, "
    "exclude_keywords?, include_keywords?) → POI search within a radius "
    "(meters). Use this for any 'X 类地点 半径 Y km 内' query — it's the "
    "ONLY tool that combines category/radius. Pass `type` as the AMap POI "
    "category code (e.g. '141201'=高中, '060100'=购物中心, '050500'=咖啡店). "
    "Pass `keyword=''` to search by category alone. **ALWAYS pass "
    "`exclude_keywords`** to strip noise (see 'Filter before mark' below).\n"
    "- map_run_search(keyword) → city-wide keyword search; does NOT honor radius. "
    "Use only for '找一下 X' without a range.\n"
    "- map_add_marker(lng, lat, title='') → plain marker (no popup). For search "
    "POIs that need detail view, prefer map_add_marker_with_info instead.\n"
    "- map_add_marker_with_info(lng, lat, title, info_html=None, poi=None) → "
    "**PREFERRED** for listing N POIs. The marker stays on the map and the "
    "info window opens on click. Pass `poi=` with the raw object from "
    "map_search_nearby's `pois[]` and the helper formats a rich card "
    "(address / tel / business hours / rating / cost / photo). Pass "
    "`info_html=` for fully custom HTML. Only one popup is open at a time.\n"
    "- map_open_info_window(lng, lat, html) → use ONLY for a single static "
    "popup at a fixed point. NEVER loop this for N POIs — it's single-instance "
    "and earlier popups get auto-closed.\n"
    "- map_draw_circle / map_draw_polygon / map_draw_polyline → visualize.\n"
    "- map_set_center / map_set_zoom / map_fit_view / map_get_state → view control "
    "and self-check (always call map_get_state once after multi-step ops).\n"
    "- map_clear_overlays(type='all'|'shape'|'marker'|'polyline'|'polygon'|'circle') "
    "→ selective cleanup.\n\n"
    "**Filter before mark (CRITICAL)**:\n"
    "AMap category codes are coarse — `type='141201'` (高中) returns POIs "
    "including 培训机构, 复读学校, 驾校, 留学中介 etc. Never blindly mark "
    "every result. ALWAYS pass `exclude_keywords` (and optionally "
    "`include_keywords`) to map_search_nearby. Only the filtered POIs get "
    "marked — drop the rest, but mention them in the summary as "
    "'已过滤 N 条噪音'.\n"
    "Recommended noise-word lists by category:\n"
    "  学校类（高中/初中/小学/大学）: exclude=['培训','复读','驾校','留学','职业','技校','中专']\n"
    "  餐饮/购物/酒店/医院/银行:    exclude=['培训','中介']\n"
    "  加油站/停车场:                exclude=['培训','中介']\n"
    "If unsure, prefer exclude (drops noise) over include (may drop legitimate "
    "results).\n\n"
    "Standard recipes:\n"
    "- 'X 附近 N km 的 Y 类' (B 类强制流程):\n"
    "  STEP 1 查 type 编码 → STEP 2 map_geocode →\n"
    "  STEP 3 map_search_nearby(type=<编码>, exclude_keywords=<噪音词>) →\n"
    "  STEP 4 遍历 pois 全部 → map_add_marker_with_info(poi=...) →\n"
    "  STEP 5 map_list_overlays 校验 marker 数 == count + 1 → map_fit_view.\n"
    "  count==0 → §10.3 retry 链（6 级降级），不许直接放弃。\n"
    "- 'A 到 B 怎么走' → map_geocode(A) + map_geocode(B) → 两个端点加 marker → "
    "execute_javascript 调用 AMap.Driving/Walking 画路线 → map_fit_view.\n\n"
    "Visual rules: blue = user/current, red = search-result-1, orange = "
    "secondary POIs, green = start, gold = saved, purple = user-placed. "
    "Use circle color by radius: 1km 步行=绿, 3km 骑行=蓝, 5km 驾车=橙.\n\n"
    "Respond in Chinese. Keep summaries short (1–2 sentences) plus a follow-up."
)


class RemoteBrowserUseAgent:
    def __init__(
        self,
        mcp_server_url: str | None = None,
        browser_use_skill_dir: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str | None = None,
        reasoning_effort: str | None = None,
    ):
        # Each parameter: explicit value wins, otherwise fall back to env var
        # (or hard-coded default). This keeps the CLI REPL runnable with
        # bare `python -m agent.RemoteBrowserUseAgent` while letting the
        # web app inject pydantic-settings-loaded values explicitly.
        mcp_server_url = mcp_server_url or os.getenv(
            "MCP_SERVER_URL", MCP_SERVER_URL,
        )
        browser_use_skill_dir = browser_use_skill_dir or os.getenv(
            "BROWSER_USE_SKILL_DIR", BROWSER_USE_SKILL,
        )
        model_name = model_name or os.getenv("LLM_MODEL_NAME", "MiniMax-M2.7")
        api_key = api_key or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        # Reasoning-mode knob. Env-var form is `LLM_REASONING_EFFORT`;
        # an empty / "off" / "none" string keeps the previous behaviour
        # (no `reasoning_effort` on the wire → no thinking). A real
        # level (minimal/low/medium/high/xhigh) flips
        # `Parameters.thinking_enable=True` so the upstream actually
        # streams `reasoning_content` deltas, which the chat UI already
        # renders (see `web/event_bridge.py` + `web/static/chat.js`).
        reasoning_effort = _normalize_reasoning_effort(
            reasoning_effort or os.getenv("LLM_REASONING_EFFORT"),
        )
        # Per-instance session_id is useful for log correlation when the
        # web app spins up one agent per WebSocket connection.
        session_id = session_id or f"browser_use_session_{os.getpid()}"

        browser_use_mcp_client = MCPClient(
            name="browser_use",
            is_stateful=False,
            mcp_config=HttpMCPConfig(
                url=mcp_server_url,
            ),
        )
        toolkit = Toolkit(
            mcps=[browser_use_mcp_client],
            skills_or_loaders=[browser_use_skill_dir],
        )
        # `OpenAIChatModel.Parameters` defaults to `thinking_enable=False`
        # and `reasoning_effort=None`, in which case agentscope does NOT
        # add `reasoning_effort` to the request — the upstream stays in
        # non-reasoning mode and no `ThinkingBlockDeltaEvent` ever fires.
        # Setting `reasoning_effort` flips both knobs together; pydantic
        # validates the value against the Literal enum at this point.
        model_kwargs: dict = dict(
            model=model_name,
            credential=OpenAICredential(
                api_key=api_key,
                base_url=base_url,
            ),
            formatter=OpenAIChatFormatter(),
            stream=True,
        )
        if reasoning_effort is not None:
            model_kwargs["parameters"] = OpenAIChatModel.Parameters(
                thinking_enable=True,
                reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
            )
        model = OpenAIChatModel(**model_kwargs)
        session_state = AgentState(
            session_id=session_id,
            permission_context=PermissionContext(
                mode=PermissionMode.BYPASS,
            ),
        )
        self.agent = Agent(
            name="BrowserControlAgent",
            system_prompt=SYSTEM_PROMPT,
            model=model,
            toolkit=toolkit,
            state=session_state,
        )

    def get_agent(self):
        return self.agent

    async def run(
        self,
        user_token: str,
        page_url: str,
        user_input: str,
        client_type: str = None,
    ):
        """
        Bind `page_url` and (when supplied) `client_type` into
        `UserMsg.metadata["tool_call_meta"]`. The framework's
        `Toolkit.call_tool` reads it from the most recent user
        message and injects it as `kwargs["_meta"]`, which
        `MCPTool.__call__` pops and forwards as the `meta` kwarg of
        `session.call_tool(...)`. The MCP server reads it from
        `CallToolRequestParams.meta`.

        `client_type` is "browser" (the default) for the regular
        web client and "wxmp" for the WeChat Mini Program client.
        The MCP server uses it to (a) compute a per-client session
        id that doesn't collide and (b) route the resulting
        `map_*` commands back to the correct client registration
        in BrowserUseServer.

        `user_token` was set at construction time and rides the
        static `X-User-Token` header.
        """
        meta = {"page_url": page_url, "X-User-Token": user_token}
        if client_type:
            meta["client_type"] = client_type
        task = UserMsg(
            name="user",
            content=user_input,
            metadata={MCP_CALL_META_KEY: meta},
        )
        async for event in self.agent.reply_stream(task):
            yield event


async def _console():
    agent = RemoteBrowserUseAgent()
    print(f"Agent ready. You can now ask questions.")
    print("-" * 60)
    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["exit", "quit", "q"]:
                print("Goodbye!")
                break
            print("-" * 60)
            # Run agent
            async for event in agent.run(
                page_url="file:///D:/work/src/chat_map/map.html",
                user_input=user_input,
                user_token="test-user-token",
            ):
                if isinstance(event, TextBlockDeltaEvent):
                    print(event.delta, end="", flush=True)
                elif isinstance(event, ToolCallStartEvent):
                    print(f"<tool:{event.tool_call_name}>")
                elif isinstance(event, ToolCallDeltaEvent):
                    print(event.delta, end="\n", flush=True)
                elif isinstance(event, ToolResultTextDeltaEvent):
                    print(event.delta, end="", flush=True)
                elif isinstance(event, ToolResultEndEvent):
                    print(f"\n</tool:{event.state}>")
        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break
        except Exception as e:
            print(f"\n[MCP Connection Error] {type(e).__name__}: {e}")
            print("You can try again or check if the MCP server is running.")


if __name__ == "__main__":
    asyncio.run(_console())
