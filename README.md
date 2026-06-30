# Chat Map

对话式地图交互 —— 通过自然语言控制高德地图。

Agent 解析中文指令，翻译为地图上的可视操作：搜索、标注、路线、区域圈选。

**两套客户端**：
- **浏览器**（`web/`）—— 匿名 host-bucket token，单人本地使用
- **微信小程序**（`wxmp/`）—— 真实 WeChat `openid`，多用户多设备

```
┌─────────────────────────────────────────────────────────────┐
│  左侧：地图              │  右侧：对话面板                    │
│  （可交互地图）          │  （自然语言输入）                  │
│                         │                                    │
│                         │  "帮我找一下上海迪士尼"            │
│                         │  "从天安门到故宫怎么走"            │
│                         │  "我家附近1公里有什么咖啡店"       │
└─────────────────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
# 安装依赖
uv sync --extra web

# 配置环境变量
cp web/.env.example web/.env
# 编辑 web/.env，填入高德地图 key 和 LLM API key

# 启动服务
uv run --extra web uvicorn web.app:app --host 0.0.0.0 --port 8000
```

打开 http://localhost:8000/

---

## 架构

**组件说明：**

| 组件 | 端口 | 职责 |
|---|---|---|
| `web/app.py` | 8000 | FastAPI 网关，唯一对外端口 |
| `server/BrowserUseServerMCPController.py` | 8765/8766 | 浏览器自动化 MCP 控制器（BrowserUseServer + MCP Server） |
| `agent/RemoteBrowserUseAgent.py` | — | 解析指令的 LLM Agent（基于 agentscope） |

**安全设计：**
- LLM API key 仅存在于服务端，不下发浏览器
- 高德地图 JS key 由服务端注入（AMap SDK 必须在浏览器端加载）
- 后端服务绑定 `127.0.0.1`，不暴露在外网
- WebSocket 端点校验 `Origin` 请求头
- `user_token` 使用 HMAC 签名防篡改

---

## 功能

### 核心能力

| 意图 | 示例 | 操作 |
|---|---|---|
| 单点搜索 | "帮我找一下上海迪士尼" | 地理编码 → 带信息窗的标注 |
| 周边查询 | "我家附近1公里有什么咖啡店" | 定位 → 半径搜索 → 标注 |
| 路线规划 | "从天安门到故宫怎么走" | 地理编码×2 → 绘制路线折线 |
| 区域圈选 | "把陆家嘴商圈圈出来" | 行政区搜索 → 多边形覆盖 |
| 多点对比 | "把北京三里屯、国贸、西单都标出来" | 地理编码×3 → 多标注 |

### 支持的操作

- **搜索**：关键词搜索、地理编码、周边半径搜索（支持 POI 类目过滤）
- **标注**：点击查看详情的信息窗、按语义着色的标注点
- **路线**：驾车、步行、公交路线，含距离/时间
- **形状**：圆（半径范围）、折线（路径轨迹）、多边形（区域）
- **视图**：平移、缩放、自适应视野

### 周边查询标准流程

B 类查询（周边/半径/范围内）必须严格按 5 步执行：

1. **识别 POI Type** — 查类目编码表（禁止用 keyword 替代）
2. **Geocode 中心点** — 验证坐标合法性（lng ∈ [-180,180], lat ∈ [-90,90]）
3. **半径搜索** — 带 type + exclude_keywords 过滤噪音数据
4. **标记全部合格 POI** — 不许截断，默认标记全部结果
5. **任务完成自检** — 验证地图变化 + marker 数量吻合

详见 [skills/chat_map/skill.md](skills/chat_map/skill.md)

---

## 配置

`web/.env`：

| 变量 | 说明 |
|---|---|
| `AMAP_KEY` | 高德地图 JS API key（会下发到浏览器） |
| `LLM_API_KEY` | LLM API key（仅服务端使用） |
| `LLM_BASE_URL` | LLM API 基础地址 |
| `LLM_MODEL_NAME` | LLM 模型名称 |
| `WEB_PUBLIC_ORIGIN` | 公开访问地址，用于 CSRF 校验和 session 生成 |
| `USER_TOKEN_SECRET` | user_token 签名密钥（至少 16 字符） |
| `WX_APP_ID` | 微信小程序 AppID（启用 `/api/wx-login` 必填） |
| `WX_APP_SECRET` | 微信小程序 AppSecret（启用 `/api/wx-login` 必填） |
| `WX_LOGIN_TIMEOUT` | 微信 jscode2session 调用超时（秒，默认 5.0） |

---

## 暂不包含

- 语音 / 图片输入
- 生产环境加固（TLS、反向代理、认证）
- 浏览器端多标签页支持（**微信小程序端无此问题**——每个 `openid` 是独立身份）
- 非高德地图提供商（如 Google Maps）

---

## 微信小程序

`wxmp/` 是独立的小程序工程。详见 [wxmp/README.md](wxmp/README.md)。

- **真实用户身份**：`wx.login()` → POST `/api/wx-login` → 用 `openid` HMAC 签出 `user_token`，根治多用户/多设备 session 冲突
- **原生地图渲染**：用 `<map>` 组件 + `amap-wx.js`，不依赖浏览器 DOM
- **工具子集**：支持全部 `map_*` 工具；DOM 类工具（`click_element` / `scroll` / `execute_javascript` 等）在 MCP 入口处直接拒绝

后端无需多跑服务——`POST /api/wx-login` 与浏览器共用同一个 FastAPI 应用，`session_id` 通过 `client_type` 维度区分两端。

---

## 文档

- [Agent Skill 规范](skills/chat_map/skill.md) — LLM Agent 详细规范
- [微信小程序使用说明](wxmp/README.md) — 小程序端配置与运行