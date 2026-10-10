# Transfer.it 提取码 + Gofile 预览失败 + 正确密码解压失败 —— 根因与修复方案

> 排查日期：2026-10-10（Issue 3 依据 10-08 与 10-10 远端 `app.log` 实证；Issue 2 依据用户纠正重诊）
> 排查方式：GitNexus 索引（`context` / 符号定位）+ 关键文件精读 + 第三方库源码核对 + **远端实例 `10.10.10.10:5555` 全量日志（17MB app.log）实证**
> 结论：三个问题**根因互不相同**，且 Issue 2 / Issue 3 的初版判断已被用户纠正，本文为**修正版**。

---

## 问题 1：Transfer.it 需要提取码时预览失败

### 现象
- 链接：`https://transfer.it/t/k7C4An4zWRST`，提取码 `AOA`
- 前端：`Transfer.it 预览失败 this transfer is password-protected — pass a password`
- 日志：前端无对应后端日志（异常在解析阶段被 `failed_items` 吞掉、按业务错误返回，未落 error 级日志）

### 证据链（索引定位 + 源码 + 实测）

**(1) 后端**已经**支持密码**，只是没拿到密码。

`http_download_service.py:3094`（`_collect_transferit_files`）：
```python
password = self._transferit_password(raw_url) or None
...
info = client.info(url, password=password) if password else client.info(url)
```

`_transferit_password`（:2887）从 URL 的 **query**（`password/pwd/pass/code`）或 **fragment** 中取密码，**无长度限制**。

transferit-py 的 `_translate_protected` 在**无 token**时产出 `"this transfer is password-protected — pass a password"`，**有 token 但错**时产出 `"wrong transfer password"`。→ 当前报错文案 = 后端确实拿到了「无密码」。

**(2) 密码丢失发生在「前端解析 URL」这一步，两处独立缺陷。**

`frontend/src/components/asmr/httpDownloadInput.js`：

- **缺陷 A：提取码长度下限过高。**
  ```js
  const PASS_CODE_FULL = /^[A-Za-z0-9]{4,12}$/   // 第 3 行
  ```
  `AOA` 只有 **3 个字符** → `validPassCode('AOA')` 返回 `''` → 密码被丢弃。
  实测（Node）：`AOA`→false，`AOA1`→true，`abc`→false。
  同一个正则还出现在 `extractPassCodeFromSegment`（:110）与 `cleanUrlCandidate`（:27/32/37）的 `{4,12}` 量词里，需一并放宽。

- **缺陷 B：中文标签紧贴提取码时切分失败。**
  `cleanUrlCandidate` 的 `trailingLabel` 正则 `(?:提取码|...)\s*[:：=]?\s*([A-Za-z0-9]{4,12})$` **本可**匹配 `提取码AOA`（`\s*` 允许 0 空格），但被 A 的 `{4,12}` 卡住返回 `null`；放宽 A 后即可命中。**要点：`AOA` 必须走「有中文标签」通道放行，不能因长度短被丢。**

**(3) 后端无「需要提取码」的可交互回退。**

- `requires_pass_code` / `pass_code` 语义**只在百度模块存在**（`baidu_netdisk_service.py:1085/1115/1203...`），Transfer.it 分支没有。
- 前端 `passCodeEditable` 被硬编码为 `isBaidu.value && item?.requires_pass_code`（`HttpDownloadPanel.vue:1334/1354/1463`）→ **Transfer.it 行既无提取码输入框，也无补码重试入口**。
- 后果：首次未识别到密码后，整条链路无任何补救手段。

### 影响面
- 所有受密码保护的 Transfer.it 分享（提取码 <4 位、或紧贴中文标签）**100% 失败**。
- 反证：手动在 URL 后加 `&pwd=AOA` 即可读到（`_transferit_password` 无长度限制）→ **后端是好的、前端是断的**。

### 方案

| 子项 | 位置 | 改动 | 风险 |
| --- | --- | --- | --- |
| A. 放宽提取码长度 | `httpDownloadInput.js:3` 及 :27/:32/:37/:110 | `{4,12}` → `{2,32}`（最小 2 位；上限放宽覆盖长口令） | 低。需配合 B 收紧上下文以避免误抓 URL 片段 |
| B. 修正中文标签提取 | `httpDownloadInput.js:110-128` | 保证 `提取码AOA`（紧贴、无分隔符）可被切出；有明确中文标签时对 2-3 位短码放行 | 低 |
| C. 后端补「需要提取码」语义 | `http_download_service.py` `_collect_transferit_files` / `_transferit_metadata_row` | 捕获 transferit-py `MegaAPIError(code=-14)`（或 message 命中 `password-protected`/`pass a password`），产出 `{"ok": False, "source": "transferit", "requires_pass_code": True, "share_url": ...}`；`metadata()` 的 `password_protected` 分支同步透传 | 中。需覆盖 metadata 兜底分支 |
| D. 前端补码 UI 泛化 | `HttpDownloadPanel.vue:1330-1463` | `passCodeEditable` 由 `isBaidu.value && ...` 放宽为 `(isBaidu.value || source==='transferit') && item?.requires_pass_code`；补码回写走 `appendSharePassCode`（`?pwd=`）——`pwd` 在 `_transferit_password` 查询键列表中，OK | 中 |
| E. 可观测性 | `_collect_transferit_files` 异常分支 | 补 `logger.warning` 带 `share_id` 与错误类型 | 低 |

**建议最小可用批次**：A + B + D（前端闭环，最快生效）+ C（后端兜底）。

**验证**：`AOA` 分享能预览出文件列表；`pytest -k transferit` 全绿；新增「3 位提取码 + 中文标签紧贴」的 `httpDownloadInput` 单测。

---

## 问题 2：Gofile 预览失败（`Cannot connect to host api.gofile.io:443 ssl:default [None]`）

### 现象
```
源站 API 请求失败: Cannot connect to host api.gofile.io:443 ssl:default [None]
```
链接：`https://gofile.io/d/YaqspDuU`。

### 用户纠正（关键，推翻初版结论）
> 「gofile 我浏览器浏览都十分正常，后端前端与本设备都是一个网络环境」
> 「我使用的是**路由器代理**，与 windows 的系统代理无关」

**初版判断（`trust_env` / 系统代理未走）被推翻**：

1. **`trust_env` 只读 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量，不读 Windows 系统代理**——但用户根本不用系统代理，用的是**路由器层面代理**（透明 / 网关级）。
2. 路由器代理对**同一网络内所有设备透明生效**（浏览器、后端容器、本机同网），因此「浏览器行、后端不行」**不能用代理归属解释**。
3. 上一版把 `trust_env=True` 当成治本点是**误判**——它只解决「env 代理未读」场景，对路由器透明代理**无影响**。

### 重新诊断（待定，但排除法已收窄）

`_fetch_json:1977` 在 3 次重试后抛 `源站 API 请求失败: {last_error}`。`Cannot connect to host ... ssl:default [None]` 是 aiohttp 对 **DNS / TCP / TLS 握手**任一失败的封装。排除代理归属后，剩余候选：

| # | 假设 | 支持证据 | 反证 / 待查 |
| --- | --- | --- | --- |
| H1 | **GeoDNS 解析到不可达的 edge IP**。`api.gofile.io` 是多 IP anycast；后端解析到的 IP 与浏览器可能不同 | 本机实测 IP 在 `202.165.70.13` ↔ `103.62.49.91` 间漂移；`_sequential_scan` 无关。历史日志有 `Connection timeout to host https://api.gofile.io/...`（2026-10-02） | 需在后端容器内 `getent hosts api.gofile.io` 对比浏览器解析结果 |
| H2 | **TLS 握手被链路设备中断**（代理/路由器/防火墙对 `*.gofile.io` 的 SNI 干预）。suffix `ssl:default` 且错误为 `None` 指向握手期 EOF/RST | 若代理对 SNI 有分流规则，`api.gofile.io` 可能被路由到黑洞 | 需抓包 / 在容器内 `curl -v https://api.gofile.io/accounts` 复现 |
| H3 | **DNS 解析失败或超时**（容器内 resolver 与宿主不同） | Docker 容器若用内网 DNS 而宿主用路由器 DNS，结果可不同 | 若真失败，`Cannot connect to host` 前段应为 `Name or service not known`，与 `ssl:default` 不符 → **弱** |
| H4 | **IPv6 优先导致黑洞**。容器/宿主启用 IPv6 但出口无 v6 路由 | aiohttp/uvloop 默认 happy-eyeballs 在部分配置下会先试 v6 | 需在容器内测 `curl -6` |

**共同点**：H1/H2/H4 都表现为「浏览器通、后端不通」，且**与 `trust_env` 无关**。

### 方案（治法按假设分层，先诊断后改码）

| 子项 | 说明 | 优先级 |
| --- | --- | --- |
| **Z（先做）诊断增强** | 在 `_fetch_json_once` 超时/连接失败分支，**分类型**记录并抛：`ClientConnectorDNSError` / `ClientConnectorError`（TCP）/ `ClientConnectorSSLError`（TLS）/ `ServerTimeoutError`，并把**实际解析到的 IP** 与目标 host 写进异常文案与日志 | **P0**。没有分类前，任何改码都是猜 |
| A. 强制 IPv4 | 若 H4 成立：`aiohttp.TCPConnector(family=socket.AF_INET, happy_eyeballs_delay=None)`；或统一收敛 session 工厂 | P1（诊断后决定） |
| B. Gofile 显式走已配置代理 | 若 H1/H2 成立且用户在「HTTP 下载设置」配了 `proxy_url`：让 Gofile API **不受 `proxy_platforms` 白名单剔除**（`_proxy_url` 对 gofile 特判）；aria2 的 `store*.gofile.io` 同步 | P1（诊断后决定） |
| C. 错误归类与用户提示 | 连接类错误抛可读文案（如「Gofile API 无法连接（DNS/TCP/TLS），请检查网络出口或代理」），与 `error-notPremium`（盐值）区分 | P1 |
| D. `trust_env=True` 补全 | **降级为「顺手补齐一致性」**，不再作为治本点：9 处 `ClientSession` 未加 `trust_env`，若用户某天改用 env 代理才生效 | P3（可选） |
| E. 盐值保鲜 | 仅当出现 `error-notPremium` 才需更新 `_GOFILE_WEBSITE_TOKEN_SALT` | 保留现状 |

**结论**：Issue 2 **不能先改码**——需先在**远端容器内**拿到分层诊断结果（DNS 解析 IP / `curl -v` / IPv6 行为）。请确认后我给出 Z 的补丁，拿到证据再决定 A/B。

### 需用户提供（Issue 2 阻塞项）
1. 远端实例**是 Docker 容器**，请提供容器内执行结果：
   - `getent hosts api.gofile.io`（或 `nslookup`）
   - `curl -v --max-time 20 https://api.gofile.io/contents/YaqspDuU`（看卡在哪一步）
   - `curl -4 -v` 与 `curl -6 -v` 对比（验证 H4）
2. 「HTTP 下载设置」里的 `proxy_url` 与 `proxy_platforms` 当前值。
> 若不方便手工执行，我可先出 **Z 诊断补丁**（纯日志/异常分类，零行为变更），下次复现即可自动拿到分层结论。

### 为什么「当时不行、现在又好了」——对网络层波动的解释
Gofile 是「前端预览 1 次 API + 下载开始再预览 1 次 API」的双调用结构（见 2b）。两次调用之间隔 10~20 秒，任一次踩中 H1/H2/H4 的抖动就会失败；抖动过去后重试即成功。**这不是缓存或代理配置变化，而是链路瞬时不可达**——正好印证「网络出口层间歇性失败」，与浏览器（有重试 + 备选 IP 竞争）表现不同。

---

## 问题 2b：Gofile「预览成功但下载失败」——`没有通过校验的下载项`（**新，2026-10-10 13:15 实证**）

### 现象
用户新报：Gofile 预览已恢复（13:14:49 preview 200 OK），但点下载后失败：
```
13:15:02 任务提交 93c3a97e..., 源: file-eu-par-5.gofile.io
13:15:04 已提交 1 个 aria2 下载 (1%)
13:15:07 任务失败: 没有通过校验的下载项          ← 无任何明细
```
**注意：报错文案没有冒号后的明细**（对比 2026-10-02 那次是 `没有通过校验的下载项: https://gofile.io/d/...: 源站 API 请求失败: ...`）。**「无明细」本身就是关键线索。**

### 根因：下载开始阶段「二次预览」在选择过滤时**静默丢弃**了 gofile 条目

> **2026-10-10 13:20 补充实证（用户提供第二次尝试日志）**：同一任务 13:15:40 自动重试 → 13:17:01 再次失败，**仍然是「无明细」**。第二次跑满 **81 秒**、中间**无任何日志**（无「已提交」、无「下载中」），证明这是**在 `_start_download_task_inner` 的下载前预览阶段卡住后失败**，而非下载中途。**可复现 = 不是网络抖动**，是确定性的选择键失配。

**关键补充：失败发生在 task_engine 的「自动重试重建选择」之后**（`task_engine.py` `_process_http_download` :6346-6360 循环）：
- 首次失败后，`build_retry_selection_for_task(task)`（:4528）从上一轮 `download_files` / `failed_files` **重建** `retry_items` 与 `retry_keys`（:4532-4536 用 `_preview_item_selection_key` 现算）。
- `_normalize_retry_source_item`（:4539）**只对 pikpak 做字段清理**，gofile 原样返回。
- 而 gofile 的 `download_files` 行（:6725-6749）只存 `file_id` / `download_file_id` / `name` / `relative_path` / `size`，**没有 `content_id`**；可 `_preview_item_selection_key`（:942-948）对 gofile 的 `stable_source_id` **优先取 `content_id`** → 重建行的 key 必然与「二次预览（带 content_id）」对不上 → `_share_item_matches_selection` 全 False → **静默 `continue`** → `items` 空 → 无明细报错。**这是一个确定性 bug，每次自动重试都必然复现**。

**双预览结构**（`_start_download_task_inner` :6489）：
1. UI 预览：`preview_urls(...)` → 返回文件列表 → 用户勾选 → 存 `selected_keys` / `selected_items`。
2. 下载开始：**再次** `preview_urls(..., materialize_sources=True, selected_items=selected_items)`（:6529），随后 `filter_preview_selection`（:6536）按选择键过滤。

**失败点**在 `resolve_source_urls` 的 gofile 分支（:4590-4609）：

```python
if selection_filter:
    files = [item for item in files if self._share_item_matches_selection("gofile", item, selection_filter)]
    if not files:
        continue          # ★ 静默 continue：既不加 failed，也不加 item
```

当二次预览的 gofile 条目**与首次预览的 selection_key 无法匹配**时（gofile 的 key = `sha1(source\nshare_url\ncontent_id\nurl\nrelative_path\nrelative_dir\nfilename\nsize)`，见 `_preview_item_selection_key` :934 —— **gofile 不在 `_SHARE_PREVIEW_ONLY_SOURCES`**，故 `filename`/`size`/`relative_path` 全部参与哈希，任一项漂移即不匹配）：
- `files` 变空 → `continue` → **既不进 `source_items` 也不进 `failed`**；
- 最终 `preview["items"]` 中该 gofile 链接**两个条目都没有**；
- `_start_download_task_inner` 的 `items=[]`、`failed_items=[]`（:6553）→ `detail=""`（:6592-6598）→ **抛出无明细的 `没有通过校验的下载项`**（:6616）。

**对照**：Transfer.it（:4679-4685）匹配失败时**不丢弃、保留全部 files 交给上层**；PikPak 更是在 `filter_preview_selection` 里专门补了「已选文件无法匹配」的可读失败行（:1071-1099）。**唯独 gofile（与 Google Drive 文件夹 :4634-4640）用了静默 `continue`** —— 这是本次「无明细失败」的直接成因。

### 证据链
| 证据 | 说明 |
| --- | --- |
| 报错**无冒号明细** | 排除 `_collect_gofile_files` 抛异常（那会产生带 reason 的 failed 条目 → 有明细）；唯一无明细路径 = `items` 与 `failed_items` 同时为空 |
| **两次尝试都无明细，第二次耗时 81s** | 第一次提交后失败（3s），自动重试第二次跑 81s 仍失败 → **确定性复现**，排除一次性网络抖动 |
| **重试行缺 `content_id`** | `download_files` gofile 行（:6725-6749）只有 `file_id`/`download_file_id`，无 `content_id`；而 key 生成优先用 `content_id`（:942-948）→ 重建 key 必与二次预览 key 不同 |
| `_normalize_retry_source_item` 只处理 pikpak | :4541 `if source != "pikpak": return normalized` → gofile 无任何补全 |
| gofile key 含 filename/size | `_preview_item_selection_key` :963 的分支只对 `{pikpak, transferit}` 剥离 filename；gofile 不剥离 |
| gofile 分支静默 continue | `resolve_source_urls` :4600-4601，匹配为空时不落 failed、不落 item |
| 与 2 的关系 | 报错形态（无明细 vs 有明细）证明 2b 是**选择过滤静默丢弃**，非网络异常；网络层（问题 2）是「当时不行现在又好了」的那部分底色 |

### 方案

| 子项 | 位置 | 改动 | 风险 |
| --- | --- | --- | --- |
| **A（P0·治本）消灭静默丢弃** | `resolve_source_urls` gofile 分支 :4595-4601（及 Google Drive 文件夹 :4634-4640） | 匹配为空时**不要 `continue`**，改为 `failed.append({"ok": False, "source": "gofile", "url": ..., "reason": "Gofile 已选文件与分享当前内容不匹配（可能分享已变更或解析漂移），请重新预览选择"})`，再 `continue` | 低。仅把「静默」变「显式失败」，不改变成功路径 |
| **B（P0·治本）gofile 选择键稳健化** | `_preview_item_selection_key` :942-963 | ①让 gofile 的 `stable_source_id` **不依赖可能缺失的 `content_id`**——优先用 `share_url` + `relative_path`/`name`（稳定字段）计算；②或让 gofile 也走 `_SHARE_PREVIEW_ONLY_SOURCES` 的剥离逻辑（剥离 `size`/`filename` 抖动项） | 中。需回归 5 个 selection 相关测试 |
| **B2（P0·治本）重试行为 gofile 补齐字段** | `_normalize_retry_source_item` :4539 | 现只处理 pikpak。给 gofile 也补：把 `download_files` 行里缺的 `content_id` 等键字段从 `source_items`/`selected_items` 回填，保证重建 key 与二次预览一致 | 中。是本次**确定性复现**的直接解 |
| **B3（P0）gofile 下载行补写 content_id** | `download_files` 行构造 :6725-6749 | 给 gofile 行补 `"content_id": item.get("content_id", "")`，使重试重建时有稳定键 | 低。一个字段 |
| **C（P1）选择失配回退整份下载** | `_start_download_task_inner` :6513-6535 | 下载开始阶段若 gofile 有 `selected_keys` 但二次预览全部失配，参照 PikPak 做法：**清空选择改成整份分享下载**（gofile 分享通常就是同一作品的文件），而不是报错 | 中。需确认用户语义 |
| **D（P1）可诊断** | `_start_download_task_inner` :6592 | `failed_items` 为空却抛错时，日志补一行带 `raw_urls` / `selected_keys` 数量 / 二次预览 items 数 | 低 |

**建议：A + B3 + B2 + B + D 一起做**（A 消灭「无明细」，B3/B2 修「重试 key 失配」这条确定性路径，D 兜可诊断）；C 作为产品决策项。

> **最小止血**：若只做一处，优先 **A**（让失败可见且可读，用户能立刻看到真实原因）——但它只治「症状可见性」；**根治确定性失败必须做 B3+B2**（重试路径的字段回填）。

### 验证
- 新增单测：模拟 gofile **自动重试**（`download_files` 行无 `content_id`）→ 断言重建 key 与二次预览 key 一致、下载成功（B2/B3 的效果）。
- 新增单测：模拟 gofile 二次预览 key 漂移 → 断言不再抛「无明细」、而是带 reason 的失败行（A 的效果）。
- 新增单测：gofile 两次预览 `_preview_item_selection_key` 在 filename/size 变化时仍相等（B 的效果）。
- 真机：用 `https://gofile.io/d/YaqspDuU`（用户实际链接）走完整「预览→勾选→下载→自动重试」链路，确认第二次不再报无明细错误。

---

## 问题 3：正确解压密码仍解压失败（**前缀壳分卷被误判为 polyglot 并剥成孤立视图**）

### 现象
- 任务：`[RJ01625284]`，源路径 `/input/百度网盘_20261008_130543/RJ01625284.part1.rar`（**2147483648 bytes**）
- 密码：用户**手动解压验证过正确**
- 最终失败：

```
ERRORS:
Headers Error

ERROR: Wrong password : RJ01625284.z1p
```

### 用户纠正（关键，推翻初版「polyglot 多卷 RAR」假设）
> 「有些压缩包为了防止被扫描内部文件，后缀名其实是乱改的，像现在这个，压缩包提供者给的是 3x3 改 exe；z1p 改 zip；r4r 改 rar。这也是本项目最开始为何要求**看实际压缩包结构而不是后缀**」

### 真实根因（10-10 远端 app.log 实证，**已 100% 复现**）

**决定性证据序列**（同一次任务，用户已把文件名手工改成标准 `RJ01625284.part1.rar` / `part2.rar`）：

```
11:41:02 [FileProcessor] 检测到分卷组: RJ01625284, 共 2 个分卷      ← file_processor 认出来了
11:41:16 7zz l -ba /input/.../RJ01625284.part1.rar
11:41:16 7zz 命令完成                                                ← ★ 原始 part1.rar 能被 7zz 直接列出！
11:41:16 [7z] 输出解码: utf-8
11:41:16 成功读取压缩包内容，密码来源: 无，密码状态: 无密码          ← ★ 无需密码即可读取
11:41:24 [Polyglot] 顺序扫描找到嵌入 RAR: .../RJ01625284.part1.rar (偏移: 444416)
11:41:38 [Extract] 剥离 RAR 前缀生成临时视图:
           view=/temp/kikoerumanager_embedded_archive_k3skj_0w.rar size=2147039232
11:41:38 7zz l -ba /temp/kikoerumanager_embedded_archive_k3skj_0w.rar
11:41:38 [ERROR] 7z命令执行失败，返回码: 2  ← 剥壳后立刻全挂
```

> `2147039232 = 2147483648 − 444416`，**精确吻合**「砍掉首卷前 444416 字节的壳」。

**根因链（三重）**：

**① 致命：`extract()` 里 polyglot 剥壳跑在分卷探测之前，且剥壳成功后完全跳过后续所有分卷逻辑。**

`extract_service.py:2499-2521`（**改动前**）：

```python
embedded_zip_direct_path = await self._prepare_embedded_archive(archive_path, task, materialize=True)
if embedded_zip_direct_path:
    archive_path = embedded_zip_direct_path          # ← 换成 /temp/xxx.rar，跳过 else 分支
else:
    archive_path = await self._repair_extension(archive_path)
    ...
volume_set = self._detect_volume_set(archive_path)   # ← 此时已是孤立临时视图
```

- `part1.rar` 的真实结构 = **444416 字节前缀壳 + RAR 数据**（提供者刻意伪装）。
- `polyglot_detector.find_embedded_archive` 的 8 字节文件头守卫（`:53-59`）只匹配裸签名；头部是壳 → 不命中 → 继续顺序扫描 → 在偏移 444416 命中 RAR 签名 → **误判为「带前缀伪装的单体压缩包」**。
- `_prepare_embedded_archive` 因此把文件从 444416 起复制成 `/temp/…rar`，**返回非 None**。
- `extract()` 走进 `if` 分支，`archive_path` 被替换 → **`_repair_extension`、`_detect_volume_set`、`_maybe_raise_disguised_volume_set` 全部被跳过**（日志中整个任务窗口**没有任何 `[VolumeSet]` 行**，与代码路径完全一致）。

**② 临时视图脱离原目录，丢失 `part2.rar` 兄弟卷 → 7zz 报 `Headers Error` + `Wrong password`。**

多卷 RAR 的每一卷都记录「续卷」信息。首卷被剥成 `/temp/…rar` 后：
- `/temp/` 目录里**只有这一个文件**，没有 `part2.rar`；
- 视图名不含 `.part1.` 语义。
- 7zz 拿残缺单卷解压 → 找不到续卷 → 头结构校验失败 → `Headers Error`；`Wrong password : RJ01625284.z1p` 是 7zz 对**分卷不完整/头损坏**的通用兜底文案，**不是真的密码错** —— 这解释了「密码正确却报密码错误」。

**③ 附带：`part2.r4r` 被 `_normalize_filename` 规范化成 `RJ01625284.rar`，抹掉分卷序号。**

```
[RENAME] 文件名规范化: .../RJ01625284.part2.r4r -> .../RJ01625284.rar
```
`current_ext='.r4r'` 不在 `common_archive_extensions` → 走通用规范化 → `_normalize_filename` 只留 RJ 号 → `.part2.` 语义被删除。若该目录同时存在 `part2.r4r`，会与首卷断开。

> 注：缺陷 ③ 只在「伪装后缀的 partN」上触发；用户在 10-08 之后手工改名后 ③ 不再生效，但 ① 依然 100% 复现（10-10 三次重跑全部失败，`part1.rar` 每次都进剥壳路径）。

### 影响面
- **任何「首卷带前缀壳（SFX / 伪装壳）+ 多卷」的压缩包** 100% 失败，且报「密码错误」误导用户。
- 直接违反项目最初设计原则「**按实际压缩包结构判定，不看后缀**」。

### 已实施修复（2026-10-10）

| 子项 | 位置 | 改动 | 状态 |
| --- | --- | --- | --- |
| **A（P0）原始路径先做分卷探测** | `extract()` `detect_type` 段 | 在 `_prepare_embedded_archive` **之前**先 `_detect_volume_set(原始路径)`；命中则 `embedded_zip_direct_path=None`（跳过剥壳），并把结果透传给第 3 步 `volume_set = original_volume_set or self._detect_volume_set(archive_path)` | ✅ |
| **B（P0）剥壳闸门：分卷成员不得剥壳** | `_prepare_embedded_archive` 入口 + 新增 `_path_is_volume_member` | 命名层判定（`.partN.*` / `.7z.NNN` / `.zNN` / `.rNN` / 纯数字+兄弟）命中即直接 `return None`，绝不生成脱离目录的视图 | ✅ |
| **E（P0）规范化不破坏分卷语义** | `_repair_extension` + `normalize_archive_filename` | 新增 `.part\d+\.` 守卫：伪装后缀的 partN 分卷保持原名，不进通用规范化 | ✅ |
| **F（可诊断）错误文案纠正** | `_try_extract` 失败归集 | `volume_incomplete` 判据由「仅 SFX 临时视图」放宽为「SFX 临时视图 **或** `_path_is_volume_member`」，使分卷残缺不再被误报为「密码错误」 | ✅ |

### 验证
- 新增 `TestVolumeMemberGuard` 6 例：`partN.rar` / `partN.r4r` / `7z.001` / `z01` / `r00` 命中；孤立 `.001` 不命中、有兄弟才命中；普通单体 `.rar` 不命中；**`_prepare_embedded_archive` 对 partN 成员返回 None**（用哨兵断言不进入剥壳流程）。全部通过。
- 远端真实包 `RJ01625284.part1.rar` / `part2.rar` 待用户在 10.10.10.10 复跑验证。
- `test_extract_service.py` 全量：**199 passed / 3 failed**，3 个失败经 `git stash` 验证与本次改动无关（存量基线：`test_repair_extension_keeps_pure_numeric_volume_set` / `test_extract_task` / `test_extract_nested_archives_repairs_disguised_sfx_rar_volumes`）。

---

## 三问题对比小结（修正版）

| 维度 | 问题 1 Transfer.it | 问题 2 Gofile 预览 | 问题 2b Gofile 下载 | 问题 3 解压 |
| --- | --- | --- | --- | --- |
| 失败层 | 前端提取码识别 + 无补码 UI | 网络出口（**非代理归属**） | 选择过滤**静默丢弃** | 解压顺序 + 伪装多卷识别 |
| 用户纠正 | — | **用路由器代理，与系统代理无关** | —（新报） | **后缀乱改，须看实际结构** |
| 初版判断 | ✅ 成立 | ❌ 推翻（`trust_env` 非治本） | — | ❌ 推翻（非 polyglot 多卷 RAR） |
| 修正后根因 | `{4,12}` 下限 + 中文标签切分 + 无 `requires_pass_code` | 待分层诊断（GeoDNS/TLS/IPv6 三选一） | gofile 分支 :4599 静默 `continue` + key 含 filename/size 易漂移 | polyglot 误判首卷 → 孤立视图丢兄弟 + `part2.r4r` 误规范化 |
| 治本手法 | 放宽正则 + 需提取码语义 + 前端补码 UI | 先出诊断补丁 Z，再按结论改 | 失配改显式失败 + gofile key 稳化 | **原始路径先探测分卷 + 分卷成员禁止剥壳 + partN 守卫 + 失败归类纠偏** |
| 与历史修复关系 | 新增能力 | 与 `bc1204c`(盐值) **无关** | 与 2 同一「间歇」底色但**报错形态不同** | **新发现**：与禁用 polyglot 的历史修复不冲突，但此前**未覆盖「首卷带前缀壳的多卷」** |

---

## 实施状态（2026-10-10）

| 问题 | 改动文件 | 状态 | 测试 |
| --- | --- | --- | --- |
| 1 Transfer.it 提取码 | `httpDownloadInput.js`（正则 `{2,32}`）、`HttpDownloadPanel.vue`（`supportsPassCodeEdit` + transferit 补码路径）、`http_download_service.py`（`_transferit_password_state` + `requires_pass_code` 失败行 + 不再重试密码错误） | ✅ 已实现 | transferit 24 passed（含新增 5） |
| 2 Gofile 预览 | — | ⏸ 待网络分层诊断 | — |
| 2b Gofile 下载 | `http_download_service.py`（A/B/B2/B3/D） | ✅ 已实现 | gofile/selection 8 passed + **全量 166 passed** |
| 3 解压失败 | `extract_service.py`（A/B/E/F + `_path_is_volume_member`） | ✅ 已实现 | extract_service **199 passed / 3 存量失败**（stash 验证无关） |

> 远端真实包复跑（`RJ01625284.part1.rar` / `part2.rar` @ 10.10.10.10）待用户验证。

## 待用户确认
1. **问题 2**：能否提供远端容器内 `getent hosts api.gofile.io` / `curl -v` / `curl -4|-6` 对比？或先出 **Z 诊断补丁**（零行为变更）等下次复现取证？
2. **问题 3**：请在 10.10.10.10 上重跑 `RJ01625284` 目录（保持 `part1.rar`/`part2.rar` 现名）验证是否已可正常解压。
