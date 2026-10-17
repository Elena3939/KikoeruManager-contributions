# 预览名称解析（规范输入 + RJ 号推导下载名）方案 v2

> 目标：网盘分享里的压缩包常被上传者改名成 `解压码****.zip` / `解压码****`，导致预览里既看不出是什么作品、又每次都要右键手动设密码和改名。
>
> v2 思路（替代 v1 的纯正则方案）：**输入侧规范格式携带 RJ 号与解压密码，预览侧从文件名兜底补密码，起任务侧复用现有 `custom_name` / `custom_extract_password` 落盘**。
>
> 代码定位全部经 GitNexus 索引（repo `prekikoeru`，2026-10-17）。

---

## 一、输入格式

### 1.1 标准四段

```
RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 提取码: xywz | 解压码: abc123
```

### 1.2 允许的变体

| 场景 | 写法 |
| --- | --- |
| 链接自带提取码 | `RJ01234567 \| https://pan.baidu.com/s/1AbCdEf?pwd=xywz \| 解压码: abc123` |
| 链接自带提取码 + 冗余提取码段 | `RJ01234567 \| https://pan.baidu.com/s/1AbCdEf?pwd=xywz \| 提取码: xywz \| 解压码: abc123` |
| 没有解压密码 | `RJ01234567 \| https://pan.baidu.com/s/1AbCdEf \| 提取码: xywz` |
| 两个都没有 | `RJ01234567 \| https://pan.baidu.com/s/1AbCdEf` |
| 首段不是 RJ 号 | 按“不符合规范格式”处理，整行退回现有识别逻辑 |

**`?pwd=` 与 `提取码:` 段同时存在是合法写法**，不是矛盾：前者是链接携带的提取码，后者是冗余的显式标签。新解析器取两者中先出现且合法的一个，不一致时以 `?pwd=` 为准并记 `pass_code_conflict` 警告。

### 1.3 分隔符

- 主分隔符 `|`，兼容 `｜`（全角）、TAB、`----`。
- **顿号 `、` 仅在首段是 `RJ\d{5,8}` 时兼容**（中文标题里顿号太常见，不能做通用分隔符）。
- 第 3、4 段**必须带标签**（`提取码:` / `解压码:`），因此顺序无关、可整体省略，不会出现“空位对不齐”。

### 1.4 判定规则

一行命中**任意一条**即视为规范格式：

1. 首段匹配 `^\s*(?:\[)?RJ\d{5,8}`；或
2. 行内出现 `解压码|解压密码|解壓碼|解壓密碼|压缩包密码` 标签。

第 2 条是为了在首段不是 RJ 时也能拆掉密码段（见 2.2 的解析坑），此时**只解析密码、不改名**。

---

## 二、解析与回退

### 2.1 新格式优先，不命中零改动

```
routes.py: baidu_netdisk_preview (:19904) / baidu_netdisk_start (:19927)
  ├─ 有 source_text 且命中规范格式
  │    → 用新解析器产出的 urls + hints
  └─ 否则
       → 原样走 _baidu_netdisk_urls_from_payload(request.urls)（当前行为，字节级不变）
```

新解析器是**独立模块**，不改 `extract_share_inputs`（`netdisk_link_parser.py:298`，GitNexus impact = **14 / CRITICAL**，上游含 `import_passwords_from_text`，任何正则行为变化都可能把 RJ 号或解压码灌进密码库）。

### 2.2 必须先拆掉的解析坑（v1 未覆盖）

`netdisk_link_parser.py:215-235` `_extract_pass_code_from_segment`：

```python
archive_context = _segment_has_archive_context(segment)   # 含「解压」即为 True
label_pattern = (提码标签表, archive 时不含「密码」)       # 防误判
explicit = re.search(label_pattern, segment)
if not explicit and allow_bare:
    bare = _PASS_CODE_RE.search(segment)                  # ← 退化成「段内第一个 4~12 位字母数字」
```

而分享链接**后面那段**正是 `allow_bare=True`（`:323`）。用真实解析器实测六种写法（直接跑 `extract_share_inputs` + `extract_archive_passwords`）：

| # | 输入 | `pass_code` | 结果 |
| --- | --- | --- | --- |
| A | `RJ… \| …/s/1AbCdEf \| 解压码: abc123` | **abc123** ✗ | 误判 |
| B | `RJ… \| …/s/1AbCdEf?pwd=xywz \| 解压码: abc123` | xywz ✓ | 安全 |
| C | `RJ… \| …/s/1AbCdEf?pwd=xywz \| 提取码: xywz \| 解压码: abc123` | xywz ✓ | 安全 |
| D | `RJ… \| …/s/1AbCdEf \| 提取码: xywz \| 解压码: abc123` | xywz ✓ | 安全 |
| E | `RJ… \| …/s/1AbCdEf?pwd=xywz\|解压码: abc123` | **abc123** ✗ | `share_url` 变 `…?pwd=xywz\|&pwd=abc123` |
| F | `RJ… \| …/s/1AbCdEf\|解压码: abc123` | **abc123** ✗ | `share_url` 变 `…/s/1AbCdEf\|?pwd=abc123` |

结论：

- **显式写了 `提取码:` 段（C/D）不会被误判**——窄标签表命中，压根走不到 `allow_bare`。所以“写了标签还被当提取码”只发生在写了**解压码**标签时。
- 原因：archive context 分支的标签表（`:221`）**故意不含「解压码」**，本意是防止把解压密码当提取码；但排除之后代码没有「这是解压密码 → 直接放弃」的分支，而是继续落到 `allow_bare` 裸取段内第一个字母数字串，防护被从后面绕过去了。
- **无空格写法（E/F）比 A 更糟**：`_URL_RE`（`:51`）排除类不含 `|`，URL 把 `|` 一起吃掉；`_clean_url_candidate` 尾部清理（`:117`）也不含 `|`，于是竖线留在 URL 里，E 还产生**双 `pwd` 参数**。

**处理**：

1. 新解析器先按 `|`（含全角 `｜`）切段，干净 URL 单独喂既有解析器；
2. `解压码:` 段整段摘除后单独保管，**绝不留在交给 `extract_share_inputs` 的文本里**；
3. 摘除后若已无提取码来源，允许 `allow_bare` 按原语义工作（此时裸串本来就是提取码）。

**摘段之后 A 场景自然消失**：交给现有解析器的只剩「RJ + 干净 URL」，段内没有裸字母数字串可抓，不会拼出 `?pwd=解压码`。所以不需要为 A 增加任何检测或兜底分支——**这一点是方案能收敛到“只做三件事”的原因，不必额外写防御**。

补充确认：真正无提取码的公开分享，URL 不带 `pwd=` 时 `_likely_requires_pass_code`（`baidu_netdisk_service.py:1203`）返回 True，但 `missing_code` 还要求 `not detail_files`（`:1085-1090`）；详情能正常拿到文件时 `detail_files` 非空，不会误标“缺提取码”。

### 2.3 防污染：按分享身份绑定，不按行号

| 措施 | 说明 |
| --- | --- |
| 绑定粒度 | **链接级**：`share_url → (rj, password)`，该分享返回的所有文件统一套用。不做文件级绑定（同一链接多文件用不同解压码是极少数情况，不值得为它增加复杂度） |
| 绑定键 | `_share_identity(share_url)`（`netdisk_link_parser.py:248`，去掉 `pwd` 等查询参数后的 URL） |
| 多行多条 | 每行的 hint 只服务该行对应的分享，**绝不整批套用** |
| 同一分享重复出现 | 取第一条带 RJ 的、密码取第一个非空；**静默取首条，不告警**——两行本身都符合标准格式（不会回退旧版），但这是自相矛盾的输入，为它加 `rj_conflict` 分支只是噪音 |
| 找不到对应分享 | 该分享无 hint（不继承上一条） |

实测确认（两行标准格式）：`1AAAAAA → passAAA`、`1BBBBBB → passBBB` 各自独立，`pass_code` 也分别正确。

item 与 share 的对应关系现成：`_preview_item_from_share`（`baidu_netdisk_service.py:1100/1104`）产出的 item 自带 `share_url` / `url` 字段，直接用它做键即可。

单测必须覆盖：两行不同 RJ + 不同密码 → 各自只拿到自己的。

---

## 三、预览呈现（回答“是不是右键那套”）

**是的，完全同一套字段。**

| 动作 | 写入字段 | 位置 |
| --- | --- | --- |
| 右键「重命名文件/连续分卷」 | `custom_name`（分卷写每条 file） | `HttpDownloadPanel.vue:1933 applyTreeRowRename` |
| 右键「设置解压密码」 | `custom_extract_password` | `:1949 applyTreeRowPassword` |
| 连续分卷整组 | 两者 + `custom_group_folder` | `:1965 applyVolumeGroupNaming` |

前端**已经有**“原名称 + 预计下载后名称”的展示：

```
:291  {{ row.name }}                      ← 原文件名，永不改动
:293  {{ row.customPreview }}             ← 预计下载名（.baidu-custom-preview）
:1778 customPreviewForTreeRow(row)        ← 由 custom_name + custom_extract_password 计算
```

所以原文件名会一直保留，右后方显示预计名。自动解析只是**预填这两个字段**，UI 无需新逻辑。

### 3.1 一个必须绕开的既有约束

`sanitize_baidu_netdisk_item`（`baidu_netdisk_service.py:179`）在返回前端前会**主动剥掉密码**：

```python
out.pop("custom_extract_password", None)   # :193
clean_file.pop("custom_extract_password", None)  # :226
out["has_extract_password"] = True         # 只告诉前端“有密码”，不告诉是什么
```

后果：服务端在**预览阶段**填的密码到不了前端，`customPreviewForTreeRow` 就渲染不出 `(密码)`。

但**内存里的原始 preview 是带密码的**，不需要起任务时再解析一遍：

```
preview_urls()                       →  _cache_raw_preview(key, preview)   # :1301，缓存的是未 sanitize 的副本
baidu_netdisk_start()                →  get_cached_raw_preview(key)        # routes.py:19949 → 拿回带 custom_extract_password 的 item
public_preview = sanitize_baidu_netdisk_preview(preview)   # routes.py:19963，只影响回给前端的副本
```

所以正确处理是：

1. **预览阶段**在 `preview_urls` 内部把 `custom_extract_password` / `custom_name` 写进 item → `_cache_raw_preview` 天然带上 → 起任务从缓存取回即可生效，**零重复解析**。
2. 额外下发**只读展示串** `name_hint`（如 `RJ01234567(abc123).zip`），前端 `customPreviewForTreeRow` 优先用它，没有则回退现有计算。密码本来就在同一请求的 `source_text` 里，`name_hint` 不构成新的暴露面。

**缓存键的坑（必须补）**：`raw_preview_cache_key`（`baidu_netdisk_service.py:365-384`）只含 `urls / target_subdir / conflict_policy / output_folder_name / account_uk / cookie_digest`，**不含 `source_text`**。密码来自 `source_text` 时会出现「同 URL 换了新解压码，预览仍返回旧密码」。

处理：preview 与 start 两端都用同一个 helper 解析 `source_text` 得到 hints，把 hints 摘要追加进 `raw_preview_cache_key` 的 payload。两端都能拿到 `source_text`（`BaiduNetdiskPreviewRequest` / `BaiduNetdiskStartRequest` 都有该字段），算法一致即可持续命中。

### 3.1.1 密码的三个来源与各自生效时机（澄清）

`extract_archive_passwords`（`netdisk_link_parser.py:385`）和 `extract_share_inputs`（`:298`）是**两个独立函数，各自扫描同一段文本**，产出互不阻塞。同一条 `解压码: abc123` 会同时被两个函数收到：

| 来源 | 解析函数 | 预览时 | 起任务时 | 落点 |
| --- | --- | --- | --- | --- |
| 输入文本里的 `解压码:` | `extract_archive_passwords` | 只回前端 chip（`routes.py:19920`），**不写库** | `_import_archive_passwords_from_request`（`routes.py:20018`）→ `upsert_generic_passwords` | 全局密码库 |
| 文件名正则兜底（新增） | 新模块 | 写 `custom_extract_password` → 进 raw preview 缓存 | 从缓存复用 | 单行密码 |
| 右键手动设置 | 前端 | — | 请求里回传 | 单行密码（最高优先级） |

**关键**：`extract_archive_passwords` 抽到了 `abc123` **不会**让 `extract_share_inputs` 放弃把它当提取码——两条管道并行。误判发生在 `extract_share_inputs` 内部（见 2.2），不是「二选一」的问题。

---

### 3.2 多文件非卷组：只提示，不自动改

服务端为这类分享下发 `rj_name_suggested = "RJ01234567"`，右键菜单在该行多出一项「使用 RJ01234567 重命名」，点击即走 `applyTreeRowRename`（右键设置 `file.custom_name = RJ号`）。

---

## 四、起任务落盘

复用 `_custom_download_relative_path`（`baidu_netdisk_service.py:3403`）：

```
custom_name = RJ01234567 + custom_extract_password = abc123
  → _filename_with_extract_password
  → _render_filename_password_template（默认 {name}({password})）
  → RJ01234567(abc123).zip
```

分卷扩展名由 `_split_archive_filename` 保留：`RJ01234567(abc123).part1.rar` / `.7z.001`。

### 4.1 写入粒度

| 情形 | 写入位置 |
| --- | --- |
| 单文件分享 | `item.custom_name` + `item.custom_extract_password` |
| 完整卷组 | 每条 `preview_files[i].custom_name`（同基名）+ 每条密码 |
| 多文件非卷组 | **只写密码**（每条 `preview_files[i].custom_extract_password`）；`custom_name` 留空 |

多文件保护是现成的，不要重写：`_apply_custom_download_name_to_rows:3291`

```python
if len(rows) != 1:
    if item.get("custom_group_folder"): ...
    row["custom_rename_skip_reason"] = "多文件分享不自动套用单文件重命名"
    return rows
```

### 4.2 优先级

```
手动右键设置 > 输入格式里的解压码 > 文件名正则兜底
```

“自动只填空位”：字段已有值就不覆盖。这样用户在预览里改过的名字/密码，起任务时不会被覆盖。

### 4.3 已有 RJ 的文件跳过改名

判定：文件名或分享标题匹配 `\bRJ\d{5,8}\b`（忽略大小写）。

- **跳过改名**，但**密码照填**（密码与文件夹名称无关，用户明确要求）；
- 若输入 RJ 与文件 RJ 不一致，只提示不阻断。

---

## 五、行为矩阵

| 情形 | 密码 | 改名 | 预览显示 |
| --- | --- | --- | --- |
| 输入有 `解压码:` | 用输入值 | 无 RJ → `RJ号(密码).ext` | 原名 + `→ RJ号(密码).ext` |
| 输入无 `解压码:` | 规则从 `解压码****.zip` 抽 | 同上 | 同上 |
| 文件名已有 RJ | 照填 | **不改** | 原名（+ `(密码)` 预览） |
| 单文件分享 | 填 | 自动 | 自动 |
| 完整卷组 | 每条都填 | 整组自动同基名 | 自动 |
| 多文件非卷组 | 每条都填 | **不自动**，右键一键套用 | 原名 + `(密码)`；带“建议 RJ”标记 |
| 首段非 RJ | 填 | 不改 | 原名 + `(密码)` |

---

## 六、配置

挂 `extract.` 段（`settings.py:133 ExtractConfig`），**只 2 处管线**：

| 位置 | 改动 |
| --- | --- |
| `settings.py` `ExtractConfig` | 加 `preview_name_parse_enabled: bool = True`、`preview_name_parse_rules: list[str] = []` |
| `useSettingsDraft.js` `defaultConfig.extract` | 加同名字段默认值 |

不需要改 `ConfigResponse`：`routes.py` 里 `extract: Optional[dict] = None` 是整段透传；前端 hydrate（`{...defaultConfig.extract, ...data.extract}`）/ serialize（`extract: payload.extract`）也是整段。

规则表只在“输入没给密码”时使用，语法是普通 Python 正则，第一条捕获组即密码：

```
解压码\s*[:：]?\s*([A-Za-z0-9@!#$%&*+_.-]{3,64})
解压密码\s*[:：]?\s*([A-Za-z0-9@!#$%&*+_.-]{3,64})
```

默认规则表**为空**，设置页提供“插入示例规则”。坏正则必须 `try/except` 跳过并记 warning，不能让预览 500。

---

## 七、改动清单

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `backend/app/core/preview_name_hints.py`（新增） | 切段、摘解压码段、RJ / 密码 / 提取码抽取、纯文件名卷组分组、`name_hint`、规则兜底、`apply_hints_to_preview` |
| 2 | `backend/app/api/routes.py` 三个 helper | `_preview_name_parse_options` / `_resolve_preview_name_hints` / `_apply_preview_name_hints` |
| 3 | 同文件 `baidu_netdisk_preview` / `baidu_netdisk_start` | 命中规范格式时用新解析器的 urls + hints；预览后写 hint 并回写缓存；start 在 `filter_preview_selection` 前后各补一次（幂等） |
| 4 | `backend/app/core/baidu_netdisk_service.py` | 新增 `refresh_raw_preview_cache`：`preview_urls` 内部缓存的是后置处理前的副本，必须回写 |
| 5 | `backend/app/config/settings.py` `ExtractConfig` | `preview_name_parse_enabled` / `preview_name_parse_rules` |
| 6 | `frontend/src/composables/useSettingsDraft.js` | 同名字段默认值 |
| 7 | `frontend/src/components/asmr/HttpDownloadPanel.vue` | `customPreviewForTreeRow` 优先用 `name_hint`（+ `passwordFromNameHint` 复原分卷组密码）；右键「使用 RJ 号重命名」；`DOWNLOAD_PREVIEW_CACHE_VERSION` 4→5 |
| 8 | `frontend/src/components/settings/ProcessingSettingsPanel.vue` | 开关 + 规则表 + 「插入示例规则」按钮 |

**明确不改**：

- `netdisk_link_parser.extract_share_inputs`（impact 14 / CRITICAL）
- `_preview_item_from_share`、`_apply_custom_download_name_to_rows`、两个 `preview_urls`
- `extract_service` 解压核心、库存索引读路径、PikPak 链路、DB

---

## 八、影响面（GitNexus）

| 符号 | 上游 | 风险 | 结论 |
| --- | --- | --- | --- |
| `extract_share_inputs` | 14 | CRITICAL | **不动**，新模块旁路 |
| `BaiduNetdiskService.preview_urls` | 6 | HIGH / 共享轴 LOW | 不改签名，routes 后置处理 |
| `_preview_item_from_share` | 7 | HIGH | 不改 |
| `_apply_custom_download_name_to_rows` | 3 | LOW | 不改，只多接收上游字段 |
| `_apply_custom_download_name_to_item` | 4 | HIGH | 不改 |
| `get_configuration` | 0 | — | 仅新增字段透传 |

---

## 九、测试计划

**新增单测** `backend/tests/test_preview_name_hints.py`

1. 四段 / 三段（URL 带 pwd）/ 两段格式解析
2. 首段非 RJ 且无解压码 → 整段回退；首段非 RJ 但有解压码 → 只填密码、不改名
3. `解压码:` 段被摘除，不再被当提取码（回归 2.2）
4. 无空格写法 `url|解压码:` → URL 不带竖线、不带密码
5. 两行不同 RJ + 不同密码 → 各自绑定，不串
6. 文件名已有 `[RJ123456]` → 不改名、仍填密码
7. 卷组纯文件名分组（part / 7z.00N / z01 / r00）；多文件非卷组只给 `rj_name_suggested`
8. 坏正则不抛异常

实测 16 项全绿；`test_baidu_netdisk_service.py` + `test_http_download_input.py` 71 项全绿。

**回归**：`test_baidu_netdisk_service.py`（自定义命名、group folder、分卷）、`test_http_download_service.py`（`preview_urls` TypeError 回退兼容）。

**手工验证**：真实百度分享 → 预览原件名 + 预计名 → 起任务 → 落盘名 = `RJ号(密码).zip` → 解压能自动嗅探出密码。

---

## 十、发版

- 版本 `2.6.52`
- 默认只推 tag（GHCR），不推 main
- 特性默认关闭，无规则且无规范输入时零行为变化
