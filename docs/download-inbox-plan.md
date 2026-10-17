# 插件直推下载输入（Download Inbox）方案

> 目标：浏览器插件抓到「RJ 号 + 网盘链接 + 提取码 + 解压码」后，直接推进 KikoeruManager；
> 用户在软件页面上点一次「预览」即可处理全部，不再来回切标签页复制粘贴。
>
> 版本计划：2.6.53（2.6.52 为预览名称解析，未发）。

---

## 0. 先说结论（含一处对现有认知的纠正）

| 问题 | 结论 |
| --- | --- |
| 现有输入框缓存能否复用？ | **不能直接复用存储介质，但能复用整条数据结构与注入链路。** |
| 为什么不能复用 | 它存在 `window.sessionStorage`（`ASMRSync.vue:824/845`，键 `kikoerumanager.asmrSync.baiduNetdiskDraft`），是**标签页级**的：插件（扩展进程）写不到网页的 sessionStorage；换标签页读不到；关标签页就消失。 |
| 能复用什么 | `normalizeDownloadDraft` 的结构（`ASMRSync.vue:787`）、`httpDownloadDraft` / `baiduNetdiskDraft` 两个 ref（`:939` / `:948`）、面板的 `v-model:draft` 注入路径。**面板是 `v-else-if` 挂载**（`:114` / `:119`），所以必须写父 ref，面板重新挂载时自然读到。 |
| 顺带纠正 | 这个缓存**防不住"误关标签页"**——sessionStorage 随标签页销毁。它防的是页面内切换与刷新。这一点正好由本方案的服务端暂存补上。 |

---

## 1. 架构

```
浏览器插件（MV3 service worker）
   │  POST /api/download-inbox/push      Header: X-Inbox-Token
   ▼
backend/app/core/download_inbox_service.py
   │  内存队列 + JSON 落盘（与 config.yaml 同目录）
   │  行级去重（归一化哈希）
   ▼
前端 ASMRSync.vue
   │  进入页面 / 切回标签页 / keep-alive 激活 → GET /api/download-inbox/pending
   │  合并进 baiduNetdiskDraft.urlText（追加 + 去重，不覆盖手输内容）
   │  用户点「预览」→ 勾选 → 点「开始下载」
   │  任务创建成功 + 状态确认非 failed → POST /api/download-inbox/ack（按链接 identity）
   ▼
既有预览链路（完全不改）：preview() → /api/baidu-netdisk/preview → preview_name_hints 旁路
```

**关键设计**：Inbox 只负责「文本搬运」，不参与解析、不触发预览、不碰下载。
解析仍由上一版（2.6.52）的 `preview_name_hints` 旁路完成，因此在 Inbox 上没有任何新解析逻辑。

---

## 2. 接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/download-inbox/push` | `X-Inbox-Token`（外部） | 插件写入，支持批量 |
| GET | `/api/download-inbox/pending?provider=baidu` | 门禁 cookie | 前端取待处理条目 |
| POST | `/api/download-inbox/ack` | 门禁 cookie | 预览成功后确认消费 |
| POST | `/api/download-inbox/clear` | 门禁 cookie | 设置页手动清空 |
| GET | `/api/download-inbox/status` | 门禁 cookie | 设置页显示队列长度 / 最近写入时间 |

### 2.1 push 请求体

```json
{
  "provider": "baidu",
  "text": "RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz | 解压码: abc123",
  "source": "chrome-ext",
  "client_id": "ext-9f3c"
}
```

- `provider`：`baidu`（默认）或 `http`，决定并入哪个面板。
- `text`：可含多行；服务端按行拆分并逐行去重。
- 限制：单次 ≤ 200 行、正文 ≤ 256 KB；超出返回 413/400。

### 2.2 pending 响应

```json
{
  "items": [
    {
      "id": "cmp7x1a2",
      "provider": "baidu",
      "line": "RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz | 解压码: abc123",
      "score": 3,
      "created_at": 1760673000
    }
  ],
  "total": 1
}
```

---

## 3. 数据与生命周期

### 3.1 存储

- 位置：`Path(get_config_file_path()).resolve().parent / "download_inbox.json"`（与 `config.yaml` 同目录，Docker 即 `/app/data`）。
- 写法参照 `circle_image_cache_service.py:77` 的目录推导范式；**原子写**（temp + `os.replace`）。
- 内存持有队列，写盘节流（同一秒内多次 push 合并一次落盘）。

### 3.2 去重

按「归一化后的整行」做键：

```
trim → 全角竖线/｜统一为 | → 连续空格压成 1 → 提取码/解压码标签统一为 "标签: 值"
```

- 相同键重复 push → 不新增，只更新 `last_seen_at`（幂等，插件重复抓同一页不会堆积）。
- 不同 provider 分别入队（同一行不会被同时并入两个面板）。

### 3.3 过期

- `ttl_hours` 默认 72；`max_items` 默认 500。超出时按 `last_seen_at` 淘汰最旧。

### 3.4 ack = 确认消费并从待处理队列移除

`ack` 是 acknowledge 的缩写，**不是「删除文件」，只是「这条待处理条目已经被用掉了，可以从队列里删掉」**。它只影响 `download_inbox.json` 里的队列，不碰已有下载任务、不碰本地文件。

按你的要求，移除时机定为**「预览完成 + 下载任务确认正常进行」**，分两阶段，避免误删：

| 阶段 | 触发点 | 动作 |
| --- | --- | --- |
| ① 任务创建成功 | `start()` 返回 `tasks[]`（`HttpDownloadPanel.vue:1011`），且已完成 `clearStartedInputUrls`（`:1022`） | 前端把「本次消费的链接 identity + taskId」暂存到 `pendingAckQueue`，**服务端不动** |
| ② 状态确认 | 首次 `refreshBaiduNetdiskWorkbench` / `refreshHttpDownloadWorkbench` 拿到任务状态 | 状态 ∈ `{pending, processing, paused, waiting_retry, completed}` → 发 ack 删除；状态 = `failed` → 不发，pending 保留等用户重试 |
| 放弃 | 用户清空输入框 / 点「清空队列」 | 手动清，不做自动 |

**为什么这个落点最自然**：`start()` 成功后本来就会调 `clearStartedInputUrls`（`:756`），把已启动的输入行从 `urlText` 移除——这正是「已消费」的既有语义，ack 只是把同一件事同步到服务端队列。

**匹配方式：按链接 identity，不按行号、不按 pending id。**
- 服务端 push 时算 `share_identity(line)`（复用 `preview_name_hints.share_identity`，`:73`，去掉 `pwd` 等参数）；
- ack 请求带上「本次 start 的 `sourceText` 快照」，服务端对每行算 identity 后删除匹配项；
- 因此**前端不需要维护 pending id ↔ 行 的映射**，关掉页面再回来也不会重复消费（服务端已删）；
- ack 幂等：重复调用无副作用。

**`sourceText` 必须是 start 那一刻的快照**，不能用 ack 时再读 `urlText`——期间可能又注入了新条目，会误删。

| 情况 | 结果 |
| --- | --- |
| 合并后关掉标签页（未预览） | pending 保留，下次进来重新合并（行级去重） |
| 预览成功但没点下载 | pending 保留 |
| 点下载、任务创建成功且未失败 | ack，服务端移除 |
| 点下载但任务 failed | 不 ack，pending 保留，可重试 |
| 同一链接重复 push | 幂等去重，不会堆积 |

---

## 4. 鉴权与安全（含安全门禁的准确影响）

### 4.0 必须先处理的安全门禁冲突

`security_gate_middleware`（`routes.py:2320`）在启用时对所有 `/api/` 返回 401（`SECURITY_GATE_PUBLIC_API_PATHS` 之外，`routes.py:2167`）。插件没有门禁 cookie，所以：

**`/api/download-inbox/push` 必须加入 `SECURITY_GATE_PUBLIC_API_PATHS`**，否则所有启用了门禁的部署（恰恰是安全意识最强的用户）插件必然 401。

代价必须说清楚：**加白名单 = 该路径不再受门禁保护，对所有来源开放**。所以 handler 内部必须自校验（见 4.1）。四个读/管理接口（`pending` / `ack` / `clear` / `status`）**不加白名单**，仍走门禁 cookie。

### 4.1 鉴权策略：局域网免 token（已定）

`download_inbox_lan_only = true` 为默认值，此时**局域网内不需要 token**，插件只填地址即可。token 保留为可选加固（填了就必须带）。

| 条件 | 行为 |
| --- | --- |
| `download_inbox_enabled = false` | 403（默认关闭，需在设置页打开） |
| 来源 IP 属于私网段，且 `lan_only = true`，且 token 为空 | ✅ 放行（你要的免 token） |
| 来源 IP 属于私网段，但 token 已配置 | 必须带匹配的 `X-Inbox-Token`，否则 401 |
| 来源 IP **不在**私网段 | 403（即使 lan_only 关闭，也要求 token） |
| 来源 IP 不在私网段且 token 为空 | 403 |
| 正文超限 | 413 |

私网判定用 `ipaddress.ip_address(host).is_private`，并单独放行 `127.0.0.1` / `::1`。来源取 `request.client.host`，**尊重既有 `security_gate.trust_proxy_headers` 设置**，不要自创一套。

⚠️ 必须知道的失效场景：
- 若把 5555 端口通过反向代理暴露到公网，`request.client.host` 会是反代的内网地址 → `lan_only` **形同虚设**，任何外网人只要知道 URL 就能写入。
- 因此**只要有一次公网暴露的可能性，就必须设置 token**。设置页在 `lan_only = true` 且 token 为空时显示黄色提示条说明这一点。
- token 比对用 `hmac.compare_digest`；失败记一条审计日志（IP + 时间），不写库。

### 4.2 Token 的配置与展示

`GET /api/config` 走 `_mask_http_downloader_config`（`routes.py:3661`），该函数是**逐字段白名单脱敏**，新增字段默认会明文返回。因此：

- 在该函数里补一行 `download_inbox_token = '********'`；
- 设置页提供「生成随机令牌 / 重新生成 / 复制」，明文只在生成时返回一次（复用既有 `reveal-secret` 模式，见 `routes.py:4012`）。

### 4.3 跨域

插件在 `manifest.json` 声明 `host_permissions: ["http://10.10.10.10:5555/*"]` 后，**service worker 的 fetch 不受 CORS 限制**，无需改 `CORS_ALLOWED_ORIGINS`（`routes.py:2159`）。

⚠️ 不要在 content script 里 fetch：content script 用的是**宿主页面的 origin**，跨域到 `:5555` 会被 CORS 拦（`host_permissions` 只作用于扩展自身的请求上下文，不给页面上下文授权）。必须由 background / service worker 发。

---

## 5. 配置项

放 `http_downloader` 段（`settings.py:373`），**不新开顶层段**：

```python
download_inbox_enabled: bool = False
download_inbox_token: str = ""
download_inbox_max_items: int = 500
download_inbox_ttl_hours: int = 72
download_inbox_lan_only: bool = True
```

理由：`ConfigResponse.http_downloader` 是 `Optional[dict]`（整段透传），前端 `useSettingsDraft.js:615` hydrate / `:674` serialize 也是整段；新增顶层段要补 5 处管线，历史上有连发三版才补全的先例。

唯一必须同步的是 `_mask_http_downloader_config`（脱敏）与前端 `defaultConfig.http_downloader`（`:249`）的默认值。

---

## 6. 前端改动

| 位置 | 改动 |
| --- | --- |
| `api/index.js` | 新增 `downloadInboxApi`：`pending / ack / clear / status` |
| `ASMRSync.vue:2881 initializeASMRSyncPage` | 初始化末尾 `await hydrateDownloadInbox()` |
| `ASMRSync.vue:2874 handleASMRSyncVisibilityChange` | 切回标签页时 `hydrateDownloadInbox()`（轻量，失败静默） |
| `ASMRSync.vue:2909 onActivated` | 同上 |
| 新增 `hydrateDownloadInbox()` | 拉 pending → 按 provider 合并进 `baiduNetdiskDraft` / `httpDownloadDraft` 的 `urlText`；行级去重；会话内记一份「已合并 identity」集合，避免同一条重复追加 |
| `HttpDownloadPanel.vue:756 clearStartedInputUrls` | 改为返回「本次移除的行」（供 start 上报）；逻辑不变 |
| `HttpDownloadPanel.vue:1012 start()` | `emit('started', ids, { provider, sourceText: 启动时的快照 })`；超时接回分支（`:1029`）同样带 |
| `BaiduNetdiskPanel.vue:6` | `@started="$emit('started', $event, $event2)"` → 改为透传全部参数（`v-on="$attrs"` 或显式两参） |
| `ASMRSync.vue:2063 handleBaiduNetdiskStarted` / HTTP 同名处理 | 收到 sourceText 快照后放入 `pendingAckQueue`（不立刻 ack） |
| `ASMRSync.vue:refreshBaiduNetdiskWorkbench` / `refreshHttpDownloadWorkbench` 成功分支 | 检查 `pendingAckQueue` 里 taskId 的状态：非 `failed` → `ack(sourceText)` 并移出队列；`failed` → 保留 |
| `HttpDownloadPanel.vue:620` / `normalizeBaiduInputRows:627` | 先按 `\|` 切段再交 `extractBaiduShareUrls`，消除「无 pwd + 解压码」被拼成 `?pwd=` 的前端误判 |
| `HttpDownloadSettingsPanel.vue` | 开关 / Token 生成复制 / 队列状态 / 清空按钮 / 插件端点地址展示 / 局域网免 token 提示条 |
| `ASMRSync.vue` 页头 | 有 pending 时显示徽标 `待处理 N`，点击定位到对应 tab（不静默切 tab） |

**合并规则**（与用户确认后固化）：

- 追加到 `urlText` 末尾，**不覆盖**用户已手输的内容；
- 按行去重（忽略首尾空白与全角空格）；
- 合并后**不自动触发预览**（百度预览可能 60s+，且需要用户选文件）；
- 若当前 tab 与 pending 的 provider 不一致 → 只提示徽标，不静默切 tab。

---

## 7. 影响面（GitNexus）

| 符号 | 方向 | 上游/规模 | 风险 | 说明 |
| --- | --- | --- | --- | --- |
| `security_gate_middleware` | 全局中间件 | 无调用边（`@app.middleware("http")` 装饰器，图谱不记录） | **MEDIUM**（需人工确认） | 白名单新增 1 条路径，是所有部署的安全边界；靠 handler 内 token 校验兜住 |
| `_mask_http_downloader_config` | upstream | **1**（`get_configuration`，LOW） | LOW | 补一行 token 脱敏 |
| `normalizeDownloadDraft` | upstream | **4**（direct 2 / depth 2-3） | LOW | 结构不变，不改签名 |
| `get_configuration` | upstream | 0（路由注册，传值调用） | LOW | 新增字段靠 pydantic 默认值，不改 `ConfigResponse` 类 |
| `preview_urls` | — | 不改 | **0** | Inbox 不触发预览 |
| `_preview_item_from_share` / `apply_hints_to_preview` | — | 不改 | **0** | 沿用 2.6.52 旁路 |
| `extract_service` / 解压核心 | — | 不改 | 0 | — |
| 库存索引读路径 / DB / PikPak | — | 不改 | 0 | — |

### 7.1 实测发现的一处前端隐患（本方案要一并处理）

前端 `parsedUrls`（`HttpDownloadPanel.vue:620` → `extractBaiduShareUrls`）对规范格式的实测结果：

| 输入 | 前端提取结果 |
| --- | --- |
| `RJ… \| …/1AbCdEf \| 提取码: xywz \| 解压码: abc123` | `…?pwd=xywz` ✅ |
| `RJ… \| …/1AbCdEf?pwd=xywz \| 解压码: abc123` | `…?pwd=xywz` ✅ |
| `RJ… \| …/1AbCdEf \| 解压码: abc123`（无提取码） | **`…?pwd=abc123` ❌** |

好在后端 `_resolve_preview_name_hints`（`routes.py:19247`）命中规范格式时会**整体替换** urls（`routes.py:19956`），前端拼错的值不会真正发给百度。但这依赖 `extract.preview_name_parse_enabled = true`。

**处理**：Inbox 开启时强制校验该开关，若关闭则在设置页给出警告；或（更彻底）在 `normalizeBaiduInputRows`（`:627`）加 8 行，先按 `|` 切段再喂 `extractBaiduShareUrls`。建议后者一并做，影响面 LOW。

---

## 8. 改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `backend/app/core/download_inbox_service.py`（新增） | 队列、JSON 落盘、行级去重、TTL 淘汰、token 校验 |
| 2 | `backend/app/config/settings.py:373` | `HttpDownloaderConfig` 加 5 个字段 |
| 3 | `backend/app/api/routes.py:2167` | `SECURITY_GATE_PUBLIC_API_PATHS` 加 push 路径 |
| 4 | `backend/app/api/routes.py:3661` | token 脱敏 |
| 5 | `backend/app/api/routes.py` 新路由段 | push / pending / ack / clear / status |
| 6 | `backend/tests/test_download_inbox_service.py`（新增） | 去重、TTL、token、超限、乱序 ack |
| 7 | `frontend/src/api/index.js` | `downloadInboxApi` |
| 8 | `frontend/src/views/ASMRSync.vue` | hydrate / 合并 / ack / 徽标 |
| 9 | `frontend/src/components/asmr/HttpDownloadPanel.vue` | 预览成功事件 + `normalizeBaiduInputRows` 切段 |
| 10 | `frontend/src/components/settings/HttpDownloadSettingsPanel.vue` | 开关 / token / 状态 / 清空 |
| 11 | `frontend/src/composables/useSettingsDraft.js:249` | 默认值 |

预计：后端 ~450 行（含测试）、前端 ~180 行。

---

## 9. 前端现在有没有"接收插件信息"的能力？

**没有。这是必须新增的一块。** 现状盘查：

| 可能的通道 | 现状 |
| --- | --- |
| 前端拉取型接口 | ❌ 无。没有任何 `pending` / `inbox` / `push` 类端点 |
| WebSocket / SSE | ⚠️ 只有 `/api/notifications/stream`（`routes.py:22752`），是通知中心专用，不承载下载输入 |
| 现有"缓存" | 只有 `sessionStorage` 本地草稿（`ASMRSync.vue:824`），**纯前端、页面级、外部不可写** |
| 剪贴板 | ❌ 无监听 |

所以 Inbox 的 `pending` 接口是**新增的唯一入口**，前端在以下三个时机主动拉：

1. 进入 ASMR 页面（`initializeASMRSyncPage`）；
2. 浏览器标签页切回来（`handleASMRSyncVisibilityChange`，已监听 `visibilitychange`）；
3. keep-alive 页面激活（`onActivated`）。

**没有常驻轮询**——省电，也符合"回到页面才处理"的用法。

### 9.1 插件侧调用

```js
// background service worker（不要写在 content script 里）
const ENDPOINT = 'http://10.10.10.10:5555/api/download-inbox/push'
const TOKEN = ''   // 局域网免 token 模式留空；公网暴露时必须填

async function pushToKikoeru(text, provider = 'baidu') {
  const headers = { 'Content-Type': 'application/json' }
  if (TOKEN) headers['X-Inbox-Token'] = TOKEN
  const resp = await fetch(ENDPOINT, {
    method: 'POST',
    headers,
    body: JSON.stringify({ provider, text, source: 'chrome-ext' })
  })
  if (!resp.ok) {
    const detail = await resp.text()
    throw new Error(`推送失败 ${resp.status}: ${detail}`)
  }
  return resp.json()   // { accepted: 1, duplicated: 0, total: 12 }
}
```

`manifest.json`：

```json
{
  "host_permissions": ["http://10.10.10.10:5555/*"]
}
```

### 9.2 插件编写要点

- **触发时机**：用户点插件按钮时推送；不要页面一加载就自动推（会产生大量重复，虽然后端去重，但会刷日志）。
- **一次推一行**或一次推整批都行；后端按行拆分。
- **不要自己判断"软件是否在线"**——失败就提示用户，重试由用户点击触发（避免后台无限重试）。
- **不要在 content script 里 fetch**（CORS 会拦，见 4.3）。
- HTTP 直链场景传 `provider: "http"`，百度分享传 `"baidu"`（或不传，默认就是 baidu）。

---

## 10. 已确认（本轮）

| 项 | 结论 |
| --- | --- |
| ack 语义 | = 确认消费并从队列移除，**不是删文件**；在「任务创建成功 + 状态非 failed」后按链接移除 |
| 鉴权 | **局域网免 token**（`lan_only = true` + token 空 → 放行）；token 保留为可选加固 |
| 前端 `\|` 切段 | **修**（`normalizeBaiduInputRows`，8 行） |

### 仍待你定 1 项

**2.6.52（预览名称解析，代码已完成、测试全绿）和 2.6.53（本次 Inbox）怎么发**：

- A：合并成一个版本 2.6.52 一起发（改动面大一次，但只发一次）；
- B：先发 2.6.52，再发 2.6.53（推荐：先让自动密码/改名跑起来，Inbox 单独验证）。
