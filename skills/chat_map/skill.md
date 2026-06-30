---
name: chat_map
description: 基于大模型对话控制的地图交互 Skill —— 把自然语言翻译成 AMap 上的可视操作（搜索/标注/路径/区域）
---

# Chat Map Skill

> Agent 通过 28 个 `map_*` MCP 工具操作 `map.html` 上的 AMap。本 skill 列出**必读协议**（§0）、**能力清单**（§1）、**视觉/响应规范**（§2/§3）、**禁做与失败回退**（§4）、**浏览器专属工具 vs wxmp 替代**（§5）。
>
> **客户端差异**：`map_*` 工具在浏览器端和微信小程序端**都支持**。但**DOM 类工具**（`click_element` / `input_text` / `select_option` / `scroll` / `scroll_horizontally` / `execute_javascript` / `get_browser_state` / `get_dom_tree`）**仅在浏览器端可用**——在微信小程序端调用会返回 `{"success": false, "message": "unsupported on wxmp client"}`。**Agent 不应**为微信小程序用户规划依赖这些工具的路径；如果当前请求只能在浏览器端完成（例如：滚动到一个被遮挡的元素、读取页面的 DOM 结构），**直接告诉用户"此操作需要使用浏览器端"**，而不是尝试在 wxmp 上调用。

---

## ⚠️ §0. 强制协议（MUST READ FIRST）

### §0.1 任务分类（第一步必做）

收到用户 query 后，先分到 5 类之一：

| 类别 | 触发词 | 必走流程 | 失败率 |
|---|---|---|---|
| **A. 单点搜索** | "找一下 X" / "X 在哪" | 单点流程 | 低 |
| **B. 空间查询**（核心，最高频） | "X 附近 / 周边 / 半径 / 范围内 / N km 内 / N 米内" | **§0.2 五步流程** | ⚠️ **高** |
| **C. 多点对比** | "把 A、B、C 都标出来" | 多点 geocode + 标点 | 低 |
| **D. 路径规划** | "A 到 B 怎么走" | geocode×2 + 路径 | 低 |
| **E. 区域圈选** | "把 X 商圈圈出来" | DistrictSearch + 多边形 | 中 |

> 任何 query 含 "附近/周边/半径/范围内/N 米/N km/内" 任一词 → **必须走 B 类 §0.2**。

### §0.2 B 类五步流程（强制）

```
STEP 1. 查类目编码（用户说的类别 → type 编码）
       例: "高中" → "141201"；"咖啡店" → "050500"
       ⛔ 严禁 keyword="高中" 替代 type="141201"

STEP 2. map_geocode(中心地址, city=...) → 验证 lng∈[-180,180] lat∈[-90,90]

STEP 3. map_search_nearby(lng, lat, radius=r_km*1000,
                          type=<编码>, city=<城市>,
                          exclude_keywords=<噪音词>)
       → 读 count: ==0 走 §4.2 retry 链（不许直接放弃）

STEP 4. 遍历 pois 全部（不许截断到前 2-3 个）：
         map_add_marker_with_info(lng=poi.location[0], lat=poi.location[1],
                                  title=poi.name, poi=poi)

STEP 5. 自检：map_list_overlays().markers 必须 == count + 1
       不等 → 回 STEP 4。文字回复必须提到 count / filtered_out
```

### §0.3 POI Type 强制编码（高频错误根因）

| 用户说 | type | 错例 |
|---|---|---|
| 高中 | **`141201`** | ❌ `keyword="高中"` |
| 初中 | `141200` | ❌ `keyword="初中"` |
| 小学 | `141100` | — |
| 大学/高校 | `141202` | — |
| 购物中心/商场 | `060100` | ❌ `keyword="商场"` |
| 咖啡店 | `050500` | ❌ `keyword="咖啡"` |
| 餐厅/餐饮 | `050000` | — |
| 酒店 | `100000` | — |
| 医院 | `090100` | — |
| 银行 | `160100` | — |
| 公园 | `110100` | — |
| 加油站 | `180300` | — |
| 停车场 | `150900` | — |

完整表见 AMap POI 分类 v2 文档。**禁止**用 `keyword=<类别名>` 替代 `type=<编码>`。

### §0.4 任务完成判定（Done Criteria）

**全部满足才能回复"完成"**：

- ✅ 地图上有可视变化（中心 marker + 圆圈 + ≥1 POI marker）
- ✅ `map_list_overlays().markers == pois.length + 1`（中心点）
- ✅ 文字回复里提到 `count` / `filtered_out` / 噪音数
- ✅ ≥1 个引导性追问（按 §3 模板）

**反例（必须回退重试）**：
- count > 0 但 marker 数 < pois.length → 回 STEP 4
- 只调 geocode 没搜索 → 从 STEP 3 重启
- 文字说"已标记"但地图无新 marker → 回 STEP 4

### §0.5 失败回退（不许直接告诉用户"无法完成"）

| 失败点 | 回退到 | 重试策略 |
|---|---|---|
| geocode 坐标异常 | STEP 2 | 换 city / 简化地址 |
| search_nearby count=0 | STEP 3 | §4.2 retry 链（6 级降级） |
| marker 数 ≠ count+1 | STEP 4 | 重新遍历 pois |
| 完成判定不通过 | 对应 STEP | 直到通过 |

---

## §1. 能力清单

`map.html` 通过 `BrowserUseClient.js` 暴露 28 个 `map_*` MCP 工具。

### §1.1 搜索与发现

| 工具 | 何时用 | 关键返回 |
|---|---|---|
| `map_run_search(keyword)` | 关键词搜（**全城、不限半径**） | `info` 栏文字 |
| `map_search_and_zoom(keyword, zoom=15)` | 搜完直接定位放大 | `info`、`zoom` |
| `map_geocode(address, city?)` | 地址 → 坐标 | `lng`、`lat`、`formatted_address`、`all[]` |
| `map_search_nearby(lng, lat, radius, type?, keyword?, city?, exclude_keywords?, include_keywords?)` | **核心** —— "X 类 半径 Y km 内" 唯一组合类目+半径的工具。`keyword=""` 时已自动用零宽空格占位，避免触发 AMap `INVALID_USER_KEYWORD` | `count`、`total_before_filter`、`filtered_out`、`excluded_by_keyword`、`pois[]`（按 `distance` 升序） |

### §1.2 标注 / 视图 / 绘图 / 弹窗

| 类别 | 工具 |
|---|---|
| **标注** | `map_add_marker(lng, lat, title)`（无弹窗）；`map_add_marker_with_info(lng, lat, title, info_html?, poi?)`（**多 POI 推荐**：传 `poi=` 自动套 §2.2 模板；N 个 popup 自动切换） |
| **视图** | `map_set_center` / `map_set_zoom` / `map_zoom_in/out` / `map_fit_view`（画完一组覆盖物后**永远调**） |
| **绘图** | `map_draw_polyline` / `map_draw_polygon` / `map_draw_circle` |
| **弹窗** | `map_open_info_window` / `map_close_info_window`（**不要循环**：单实例会盖掉前 N-1 个） |
| **状态** | `map_get_state`（自检）/ `map_list_overlays` / `map_clear_overlays(type)` / `map_remove_overlay(type, index)` / `map_clear_markers`（仅清标记） |
| **定位** | `map_locate`（**异步**：调后立刻返回，结果查 `map_get_state().center`） |

### §1.3 噪音词（`exclude_keywords` / `include_keywords`）

类目编码粒度粗 —— `type="141201"`（高中）会混入培训机构/复读/驾校等。**默认必须过滤**：

| 类目 | `exclude_keywords` | `include_keywords`（可选） |
|---|---|---|
| 高中 `141201` | 培训, 复读, 驾校, 留学, 中专, 技校, 职业 | 高中, 中学, 附中, 学院 |
| 初中 `141200` | 培训, 复读, 中专, 技校 | 初中, 中学, 附中 |
| 小学 `141100` | 培训, 复读 | 小学 |
| 餐厅 `050000` | 培训, 中介, 装修 | 餐厅, 饭店, 食堂 |
| 酒店 `100000` | 中介, 培训, 二手 | 酒店, 宾馆, 民宿 |
| 医院 `090100` | 培训, 中介, 美容 | 医院, 诊所 |
| 银行 `160100` | 培训, 中介 | 银行, 信用社 |

返回字段新增：

```json
{
  "count": 5, "total_before_filter": 13, "filtered_out": 8,
  "excluded_by_keyword": {"培训": 4, "复读": 2, "驾校": 2},
  "pois": [...]
}
```

---

## §2. 视觉规范

### §2.1 颜色语义（统一一张表）

| 语义 | 颜色 | 备注 |
|---|---|---|
| 搜索结果主点 | `#f5222d` 红 | 第一结果 |
| 搜索结果次点 | `#fa8c16` 橙 | 其余结果 |
| 路径起点 / 步行 1km | `#52c41a` 绿 | |
| 路径终点 / 驾车 | `#1890ff` 蓝 | |
| 途经点 / 公交 | `#faad14` 黄 | |
| 用户当前位置 | `#1890ff` 蓝 | 由 `map_locate` 自动打 |
| 用户手动标注 / 用户连线 | `#722ed1` 紫 | |
| 收藏/重要 | `#d4af37` 金 | |
| 商圈 / 区域 | `#fa8c16` 橙 fillOpacity 0.2 | |

> 当前 `map_add_marker` 不支持颜色参数；需彩色 marker 时用 `map_draw_polyline` 叠加彩色线段，或在 `info_html` 里嵌入自定义 HTML 配色。**不要**调 `execute_javascript` —— 在微信小程序客户端会直接返回 `unsupported on wxmp client`。

### §2.2 标准信息窗（`map_add_marker_with_info` 的 `poi=` 模式自动套用）

```
[照片（如有）]
{POI 名}（粗体）
{类型} · 距中心 X 米
─────────────────
📍 {地址}
📞 {电话} / 🕒 {营业时间}
⭐ {评分} / 💰 {人均 ¥}
```

所有字段已 HTML escape，LLM 无需操心 XSS。优先级：`info_html` > `poi` > 兜底纯文本。

---

## §3. 响应模板

### §3.1 单结果（默认）

```
已把「上海迪士尼度假区」标在地图上（红色可点击标记），放大到 16 级。
💡 点击标记查看详情。
要不要查一下「上海迪士尼 → 浦东机场」的路线？
```

### §3.2 多结果（≥8 用数字标签 marker，详见 §5）

```
为您在「芳菲路 88 号院」5km 内找到 12 所高中（橙色圈内，按距离升序）：
  ① 北京市第八中学（分校）— 1.2 km
  ② 北师大附中（南门）— 1.8 km
  ③ 清华附属实验学校 — 2.1 km
  … 中段略（5–9）
  ⑩ 首都师范大学附属中学（永定路校区）— 4.6 km
🗑️ 已过滤 6 条噪音（培训机构 4、复读 1、驾校 1）。
💡 点击带数字的 marker 看详情。
要看哪几所的详细信息？要不要把范围缩到 1km 或换成初中（type="141200"）？
```

### §3.3 路径规划

```
「天安门 → 故故」驾车路线已画好（蓝色折线）：
  距离 1.2 km ｜ 约 5 分钟 ｜ 3 个红绿灯
绿 = 起点，红 = 终点。要不要换成步行？
```

---

## §4. 必做 / 禁用 / 失败回退

### §4.1 必做（每次回答满足 ≥2 条）

1. ✅ 触发一次可视变化（标点 / 画线 / 圈 / 弹窗 / 改视图）
2. ✅ 操作后 `map_get_state` 自检
3. ✅ 文字里有引导性追问（§3 模板）

### §4.2 禁用

- ❌ 未经用户允许 `map_clear_overlays('all')`（会清掉用户之前标注）
- ❌ 把经纬度直接贴给用户（改用自然语言 + 区域）
- ❌ 循环 `map_open_info_window`（单实例盖掉前 N-1 个）
- ❌ 把 AMap 搜出来的 POI 全打 marker（必须先 exclude/include 过滤；过滤掉的 POI 在文字里提一句）
- ❌ 只展示前 2-3 个结果就停（默认全标完，除非用户说"看最近的 3 个"）
- ❌ 同色 marker > 7 个（视觉糊）：≥8 用 §5 `addLabeledMarker` 加数字

### §4.3 搜索 0 结果 retry 链（6 级降级，按序尝试直到 count > 0）

1. 首次：带 `exclude_keywords`
2. 放宽 exclude（去掉最严的关键词）
3. 不传 exclude（纯 type 重试）
4. 扩大 radius ×2
5. 加 `keyword="中学"` 兜底
6. 仍 0 → 告诉用户没找到 + 建议换类目/范围

---

## §5. 高级：浏览器专属工具

> ⚠️ **`execute_javascript`、`click_element`、`input_text`、`select_option`、`scroll`、`scroll_horizontally`、`get_browser_state`、`get_dom_tree` 这 8 个工具在微信小程序客户端不可用**——会直接返回 `{"success": false, "message": "unsupported on wxmp client"}`。
>
> 当前请求的 `client_type` 会在 `UserMsg.metadata.client_type` 里告诉 Agent（`"browser"` 或 `"wxmp"`）。如果当前是 wxmp，**用下面 §5.1 替代方案**——不要尝试用 JS 模板。

### §5.1 浏览器专属工具的 wxmp 替代方案

| 浏览器做法（`execute_javascript`） | wxmp 替代 | 说明 |
|---|---|---|
| `window.__map.addCustomMarker(lng, lat, color)` 自定义颜色 marker | `map_add_marker` + `map_draw_circle` 圈选 | wxmp 不支持自定义 marker 颜色；用 POI 区分 |
| `AMap.Driving.search(start, end, ...)` 路径规划 | `map_draw_polyline` 用高德 REST API 拿到的坐标点 | wxmp 需要先用 `map_geocode` 拿起点终点，再调 REST API（暂无 wxmp 工具，待实现） |
| `AMap.DistrictSearch` 行政区划 | `map_search_nearby` + `map_draw_polygon` 圈区域 | 用周边搜索 + polygon 包络 |
| `AMap.GeometryUtil.distance` 距离测量 | `map_search_nearby` 返回的 `pois[].distance` | 已经在 POI 数据里 |
| `AMap.Transfer / Walking / Riding` 多方式路径 | ❌ wxmp 不支持 | 告诉用户换浏览器 |

### §5.1 带数字标签的彩色 marker（≥8 个结果时用）

```js
async function addLabeledMarker(lng, lat, index, color, title) {
  const icon = new AMap.Icon({
    size: new AMap.Size(28, 36),
    image: 'data:image/svg+xml;utf8,' + encodeURIComponent(`
      <svg xmlns="http://www.w3.org/2000/svg" width="28" height="36" viewBox="0 0 28 36">
        <path d="M14 0C6.27 0 0 6.27 0 14c0 9.5 14 22 14 22s14-12.5 14-22C28 6.27 21.73 0 14 0z"
              fill="${color}" stroke="#fff" stroke-width="2"/>
        <text x="14" y="19" text-anchor="middle" fill="#fff" font-size="14"
              font-weight="bold" font-family="Arial">${index}</text>
      </svg>`),
    imageSize: new AMap.Size(28, 36),
  });
  return new AMap.Marker({ position: [lng, lat], icon, title, offset: new AMap.Pixel(-14, -36) })
    .setMap(window.__map.map);
}
return addLabeledMarker(116.4, 39.9, 1, '#f5222d', '天安门');
```

### §5.2 距离 / 面积

```js
// 两点
const m = AMap.GeometryUtil.distance([116.397, 39.909], [116.41, 39.92]);
return { meters: m, km: (m / 1000).toFixed(2) };

// 折线总长
const total = AMap.GeometryUtil.distanceOfLine([[116.4,39.9],[116.5,40.0]]);

// 多边形面积
const sqm = AMap.GeometryUtil.ringArea(ring);  // 平方米
```

### §5.3 行政区划（商圈）

```js
const ds = new AMap.DistrictSearch({ subdistrict: 0, extensions: 'all', level: 'district' });
ds.search('陆家嘴', (status, result) => {
  const d = result.districtList[0];
  d.boundaries.forEach(ring => window.__map.addPolygon(ring, { color: '#fa8c16', fillOpacity: 0.2 }));
  window.__map.fitView();
});
```

> 驾车/步行/公交路径规划：把上面的 `AMap.DistrictSearch` 换成 `AMap.Driving` / `AMap.Walking` / `AMap.Transfer`，`search(origin, dest, cb)` 回调里读 `result.routes[0].steps.flatMap(s => s.path)`，再 `window.__map.addPolyline(path, ...)`。回调 `(status, result)` 二元组，`status === 'complete'` 判成功。

---

## §6. 错误处理

| 错误 | Agent 应做的 |
|---|---|
| `window.__map is not available` | "请先在浏览器打开 map.html" |
| `AMap.Geocoder plugin not loaded` | "需要 reload map.html" |
| 搜索 0 结果 | 走 §4.3 retry 链；仍 0 → "没找到 X，要试试 [同义词/更宽泛词] 吗？" |
| 定位失败 | "请检查浏览器授权 + HTTPS" |
| timeout | 退避重试 1 次；仍失败告知用户 |

---

## §7. 示例对话

### §7.1 B 类（核心）

> **User**：芳菲路 88 号院半径 5km 内所有的高中？标记出来。

```
[1] STEP 1：高中 → type="141201"
[2] STEP 2：map_geocode("芳菲路 88 号院", city="北京")
            → {lng, lat, formatted_address}
[3] STEP 3：map_search_nearby(lng, lat, 5000, type="141201",
                              city="北京",
                              exclude_keywords=["培训","复读","驾校","留学","职业","技校","中专"])
            → count=8, filtered_out=5, pois=[8 项]
[4] STEP 4：循环 pois → map_add_marker_with_info(poi=poi) × 8
[5] STEP 5：map_list_overlays().markers == 8 + 1 = 9 ✅
[6] map_fit_view() + map_get_state()
```

输出见 §3.2 模板。

### §7.2 D 类（路径）

> **User**：从天安门到故宫怎么走。

```
map_geocode("天安门") + map_geocode("故宫")  → 两端坐标
map_add_marker(起, 绿) + map_add_marker(终, 红)
execute_javascript(AMap.Driving → search → addPolyline 蓝)
map_fit_view()
```

输出见 §3.3 模板。

### §7.3 E 类（区域）

> **User**：把陆家嘴商圈圈出来。

```
execute_javascript(AMap.DistrictSearch("陆家嘴") → addPolygon 橙 fillOpacity 0.2)
map_fit_view()
→ "已用橙色半透明多边形圈出「陆家嘴商圈」。要不要再标几个商圈内最著名的写字楼？"
```

---

## §8. 边界

- 本 skill 假设浏览器已加载 `map.html` 且 `BrowserUseClient` 已连上 `BrowserUseServer`。
- 所有 `map_*` 工具都需要 `map.html` 提供 `window.__map`；不在则返回 `success: false`。
- `execute_javascript` 失败时**不要**静默重试 >2 次。
- 本 skill **不能**改 `map.html` 的样式/控件；要改 UI 请改 `map.html` 本身。

---

## §9. 后续扩展（按价值排序）

1. `map_route(origin, dest, mode)` —— 提成 §5.3 驾车/步行/公交
2. `map_measure(path)` —— 提成 §5.2 距离/面积
3. `map_draw_district(name, options)` —— 提成 §5.3 行政区
4. `map_add_marker_with_style(lng, lat, color, label, icon)` —— 提成 §5.1
5. `map_screenshot()` —— 导出当前地图
6. 跨会话持久化 —— 让"收藏"真的能保存