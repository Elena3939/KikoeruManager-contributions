# AI 标题翻译「reasoning_content 被当成答案去解析 JSON」缺陷——根因与修复方案

日期：2026-10-10
触发：用户实测日志（14:49 / 14:52 两次）
文件：`backend/app/core/ai_title_translation_service.py`
关联：`backend/app/core/ai_subtitle_match_service.py`（`extract_litellm_stream_delta_verbose`）

---

## 1. 现象（用户日志原文摘要）

```
14:49:12 info  [AI标题] 构建翻译 prompt: work_name=ごめんね、マネージャーくん ～イケメス特撮女優の肉体改造アクメ地獄堕ち報告～
14:49:12 info  [AI标题] prompt 替换后内容: 下面我让你来充当翻译家…请翻译下面这段话，输出 JSON 格式，key 为原文，value 为翻译结果：\nごめんね、マネージ
14:49:45 info  [AI标题] 标题翻译[0ef8e086] 流式响应汇总: chunks=6673 finish_reason=stop
              usage={'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
              字段长度={'reasoning_content': 10200}          ← ★ 只有 reasoning_content，没有 content
14:49:45 info  [AI标题] 标题翻译[0ef8e086] 响应字段[reasoning_content]（10200 字符，思考/其他）:
              我们需要回答用户。用户要求：充当翻译家…要求翻译下面这段话，输出 JSON 格式，key 为原文，value 为翻译结果：
14:49:45 error [AI标题] 标题翻译[0ef8e086] 确定性错误，不再重试: 模型返回的 JSON 无法解析或不完整:
              Extra data: line 4 column 1 (char 84)
14:49:45 warn  [AI标题] 翻译失败: work_name=ごめんね、マネージャーくん …
14:52:32 （同一作品重试，同样路径）
14:53:00 error 标题翻译[2ca49449] 确定性错误，不再重试: 模型返回的 JSON 无法解析或不完整:
              Extra data: line 1 column 20 (char 19)
```

**关键观察**：
1. `字段长度={'reasoning_content': 10200}` —— **模型只输出了 `reasoning_content`（思考），`content` 字段完全没有出现**。
2. 日志里**没有**「正文为空，回退使用字段[...]」这条 warning（`ai_title_translation_service.py:548`）——说明代码**认为 `content` 非空**，没有走回退分支。
3. 报错是 `Extra data`（不是 `Expecting...`）：说明 `json.loads` **确实解析出了合法 JSON**，但「第一个 `{` 到最后一个 `}`」的区间里**还有多余内容**。
4. 两次报错偏移不同（char 84 / char 19），且第二次只有 19 字符——说明**解析对象是模型的思考文本**，其内容每次不同。

---

## 2. 根因链（代码级确认）

### 2.1 `reasoning_content` 被当作「主文本」拼进了 `content`

`ai_subtitle_match_service.py:155`：

```python
_PRIMARY_TEXT_KEYS = ("content", "reasoning_content", "reasoning", "thinking", "text")
```

`extract_litellm_stream_delta_verbose`（:235）选主文本的逻辑（:284-292）：

```python
primary = ""
for key in _PRIMARY_TEXT_KEYS:
    if collected.get(key):
        primary = collected[key]
        break
if not primary and collected:
    primary = next(iter(collected.values()))
```

由于 `reasoning_content` **就在** `_PRIMARY_TEXT_KEYS` 里，且 `content` 为空：
→ `primary = reasoning_content` 的文本。

于是 `_consume_litellm_stream`（`ai_title_translation_service.py:334-335`）：

```python
if delta:
    parts.append(delta)      # ← 把「思考」当成正文追加
...
content = "".join(parts)     # ← content = 10200 字的思考文本
```

**所以 `content` 非空**，`:541` 的 `if not content and collected_fields` 回退分支**不会触发**（与日志中缺少该 warning 完全吻合）。

> 这是 **v2.6.45「推理模型正文为空」修复的副作用**：当时为了不让「模型有输出却被判空」，
> 把 `reasoning_content` 提升为可用的主文本。方向没错，但**没有区分「正文」与「思考」**——
> 思考文本被直接当成翻译结果送进了后续校验。

### 2.2 后续 JSON 提取跑在「思考文本」上 → `Extra data`

`ai_title_translation_service.py:564-574`：

```python
content = content.strip()
json_start = content.find('{')
json_end = content.rfind('}') + 1
if json_start >= 0 and json_end > json_start:
    try:
        parsed = _json.loads(content[json_start:json_end])   # ← 对思考文本做「首{到末}」切片
    except _json.JSONDecodeError as exc:
        raise ValueError(f"模型返回的 JSON 无法解析或不完整: {exc}") from exc
```

「首 `{` 到末 `}`」这个取法是给**正常 JSON 答案**设计的。一旦输入是**思考文本**，
思考里往往会出现多个 `{}` 片段（复述任务、给示例、分段推理），
首 `{` 与末 `}` 之间就会跨越**多个不连续对象** → `Extra data`。

实测复现（本机 Python 3.13 验证）：

```python
frag = '我们需要回答用户。…输出 JSON 格式：\n{"a":"b"}\n综上，翻译完成。{补充}'
js, je = frag.find('{'), frag.rfind('}')+1
json.loads(frag[js:je])
# → json.decoder.JSONDecodeError: Extra data: line 2 column 1 (char 10)
```

**char 19 的含义**：思考文本里第一个完整 `{...}` 只有 19 字符（例如 `{"key": "value"}` 这类
任务复述中的示例片段），其后还有内容 → `Extra data: line 1 column 20 (char 19)`。
**char 84** 则是另一个更长的片段形态。两者都指向同一件事：**解析目标错了**。

### 2.3 为什么被归为「确定性错误」

`_NON_RETRYABLE_MARKERS`（:187）含 `"无法解析或不完整"`，因此该异常被 `_is_non_retryable_error`
判为确定性、**不再重试**（日志 `确定性错误，不再重试`）。这一点本身是合理的（重试同样会拿到
推理文本），但**当前错误分类是错怪的**——不是「模型返回了坏 JSON」，而是「解析对象张冠李戴」。

---

## 3. 影响范围

| 场景 | 影响 |
|---|---|
| 使用**推理模型**（输出只在 `reasoning_content`）做标题翻译 | **必失败**（本 case） |
| 使用普通模型（正文在 `content`） | 不受影响（`content` 优先） |
| 推理模型但 `content` 也有内容 | 不受影响（`content` 优先，`reasoning_content` 不入主文本） |
| AI 字幕配对 / 连接测试 | **共用** `extract_litellm_stream_delta_verbose`，但两者不跑 JSON 首末切片校验，故不触发本 bug；不过「思考被当正文」的语义混淆同样存在 |

引入时间：v2.6.45（`reasoning_content` 提升为主文本）起。

---

## 4. 修复方案

### 核心原则
**「思考」与「正文」必须分开**：`reasoning_content` 的用途是**诊断与兜底**，
不能被当作**翻译答案**直接进入 JSON 校验与写库。

### 方案 A（推荐，最小且治本）：`_consume_litellm_stream` 分离正文与思考

`_consume_litellm_stream` 改为**只把真正的正文（`content`/`text`）拼进返回值 `content`**，
`reasoning_content` / `reasoning` / `thinking` 只进 `collected_fields`（继续打印诊断日志）。

```python
# ai_title_translation_service.py:327-340
_ANSWER_KEYS = ("content", "text")          # 可作答案的字段（含未知自定义字段的兜底见下）
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")

async for chunk in stream:
    chunk_count += 1
    delta, collected, chunk_usage, chunk_finish = extract_litellm_stream_delta_verbose(chunk)
    if collected:
        saw_any_fields = True
        for key, text in collected.items():
            accumulated[key] = accumulated.get(key, "") + text
    # ★ 只有「答案类」字段进正文；思考类字段不拼
    answer_piece = delta if delta and <delta 来自答案类字段> else ...
```

但 `delta` 已经是「选过 primary」的结果，无法反推来源。**更稳的做法**：
在 `_consume_litellm_stream` 里**直接用 `collected` 自己选答案字段**，不再依赖 `delta`：

```python
answer_keys_seen = set()
...
for key, text in collected.items():
    accumulated[key] = accumulated.get(key, "") + text
    if key in _REASONING_KEYS:
        continue                      # 思考字段不参与正文
    if key == "content" or key == "text" or key not in _NON_ANSWER_KEYS:
        answer_keys_seen.add(key)
        parts.append(text)            # 正文按到达顺序拼接
```

配套：`:541-556` 的空正文回退逻辑保留（在**真的**没有任何答案类字段时才回退用思考文本），
但回退时要**明确记 warning**，并且——关键——回退得到的思考文本**不应再走 JSON 强解析**，
而是按「纯文本标题」处理（见方案 C）。

### 方案 B（配合 A）：思考文本回退时降低 JSON 校验强度
当 `content` 是从 `reasoning_content` 回退得来（打标记 `used_reasoning_fallback=True`）时：
- 跳过「首 `{` 到末 `}` 整体 `json.loads`」的严格校验；
- 改用**宽松提取**：逐个尝试 `json.JSONDecoder().raw_decode` 扫描，取**第一个可解析的对象**；
- 若仍失败，则**不报「JSON 无法解析」**，而是走纯文本清洗（strip 引号/括号）后作为候选。

### 方案 C（治本补充）：JSON 提取改为「扫描式」而非「首末切片」
把 `:567-585` 的 `find('{')/rfind('}')` 换成 **`raw_decode` 扫描**：

```python
decoder = _json.JSONDecoder()
candidates = []
idx = 0
while True:
    start = content.find('{', idx)
    if start < 0:
        break
    try:
        obj, end = decoder.raw_decode(content, start)
    except _json.JSONDecodeError:
        idx = start + 1
        continue
    if isinstance(obj, dict):
        candidates.append(obj)
    idx = end
```

- 取**第一个**能解析的 dict 作为结果（对「思考里先给示例、再给答案」也能取到示例——
  有误取风险，故**仅在回退路径**或**答案里出现多个对象**时启用，正常路径保持严格）。
- 正常路径（正文就是 JSON）保持不变，避免「思考里的示例 JSON 被误当答案」。
- 若扫描无果且有 `{` 无 `}` → 仍报截断（保留 v2.6.45 的既有保护）。
- 若扫描出多个对象 → 记 warning 但**不报错**，取最后一个（答案通常在最后）。

### 方案 D（可诊断）：错误信息不再误导
- JSON 解析失败时，日志/返回**带上解析对象来源**（`content` vs `reasoning_content` 回退）
  与**前 200 字预览**，便于一眼看出「解析的是思考而非答案」。
- 补一条 `_NON_RETRYABLE_MARKERS` 语义修正：`"无法解析或不完整"` 保留（重试确实无益），
  但错误文案改为「模型未返回可用 JSON（当前解析对象=思考文本）」之类，不再暗示模型输出坏了。

### 方案 E（可选，产品层）：提示词层面抑制
默认 `prompt_template`（`settings.py:567-574`）末尾可加一句「**直接输出 JSON，不要输出分析过程**」。
但这依赖模型自觉，**不能替代 A~D**，只作为降低触发率的辅助。（默认模板改动会影响
已有用户配置的一致性判断，需评估是否只改「新建配置」的默认值。）

---

## 5. 方案取舍

| 方案 | 性质 | 是否必须 |
|---|---|---|
| A 正文/思考分离 | **治本**，消除「思考被当答案」 | ✅ 必须 |
| B 回退时弱校验 | 防「回退路径再触发同一 bug」 | ✅ 必须（A 的配套） |
| C JSON 扫描式提取 | 提升对「思考夹示例」的鲁棒性 | ✅ 建议（与 B 一起） |
| D 可诊断 | 排障提速 | ✅ 建议 |
| E 提示词抑制 | 降低触发率 | ⭕ 可选 |

**推荐组合：A + B + C + D**（E 另议）。

---

## 6. 回归测试计划（新增）

1. `test_reasoning_only_does_not_get_json_parsed`
   流式只回 `reasoning_content`（含多个 `{}` 片段）→ **不得**报「JSON 无法解析」，
   应按纯文本候选成功返回（或明确失败但不带误导性 JSON 文案）。
2. `test_content_json_still_parsed_strictly`
   正文为正常 JSON → 行为与现在完全一致（`content` 优先、正常解析）。
3. `test_reasoning_with_multiple_json_fragments_takes_answer`
   思考里先有示例 `{"key":"value"}` 再有真答案 → 取到真答案。
4. `test_content_and_reasoning_both_present_reasoning_excluded`
   `content` 与 `reasoning_content` 同时有 → 正文只含 `content`，思考只进诊断字段。
5. `test_empty_answer_with_reasoning_falls_back_with_warning`
   正文真空、只有思考 → 回退 + warning（保留 v2.6.45 行为），且不走严格 JSON 校验。
6. **锁定 `test_stream_reasoning_only_content_is_used_not_failed`（现有 v2.6.45 用例）语义不变**：
   该用例的 `reasoning_content` 内容是「（思考）先分析日文标题」「圣职者姐妹」——
   **无 `{}`**，修复后仍应成功且含「圣职者姐妹」。这条是**防回归红线**：不能因为修本次 bug
   而把 v2.6.45 修好的「推理模型不被判空」打回去。

---

## 7. 待用户确认

1. 是否同意 **A+B+C+D** 组合（E 可另议）？
2. 回退用思考文本时，**允许它作为翻译结果写库**（现状行为，仅降低 JSON 校验强度），
   还是**宁可不写、明确失败**（更保守，避免把思考碎片当标题）？
   - 倾向：**先宽松提取，取不到明确答案再失败**；若能取到像「标题」的短文本则接受。
3. 是否需要顺带把 AI 字幕配对 / 连接测试的同一语义混淆一并梳理（本次不动，仅记录）？

---

## 8. 影响范围（GitNexus 索引确认，2026-10-10）

`gx impact extract_litellm_stream_delta_verbose` → **risk HIGH / impactedCount 5**，直接调用方 2 个：

| 符号 | 调用方 | 本次是否受影响 |
|---|---|---|
| `extract_litellm_stream_delta_verbose` (:235) | `_consume_litellm_stream`（标题翻译主链路，**用 delta 拼正文**） | ✅ **仅此处有 bug** |
| 同上 | `AITitleTranslationService.test_connection._stream_probe` | ❌ 仅作诊断用（正文走非 verbose 版） |
| `_extract_litellm_stream_delta` (:112) | `_stream_probe`（连接测试正文）、`_complete_streaming`（**AI 字幕配对**） | ❌ **只读 `content`，本就正确**，本次不动 |
| `_PRIMARY_TEXT_KEYS` (:155) | 上面两条共用 | ❌ **不得修改**（改它会把思考字段从「字幕配对」里也去掉，属过度扩大） |

**结论：修复必须限定在 `_consume_litellm_stream` 内部**（标题翻译的消费侧），
公共提取函数与字段优先级常量保持原样——这样字幕配对 / 连接测试零影响。
`test_connection` 的正文本就走 `_extract_litellm_stream_delta`（只取 content），
与本次无关。

---

## 9. 实施状态（2026-10-10，用户批准 A+B+C+D）

| 方案 | 落点 | 状态 |
|---|---|---|
| A 正文/思考分离 | 新增 `_pick_answer_piece()`；`_consume_litellm_stream` 改为从 `collected` 自行判定答案字段，**不再使用 verbose 选出的 `delta`**；新增常量 `_REASONING_FIELD_KEYS` / `_ANSWER_FIELD_KEYS` | ✅ |
| B 回退标记 + 弱校验 | `_call_model` 回退时置 `reasoning_fallback`（按 `fallback_key in _REASONING_FIELD_KEYS`），warning 文案加「该字段是思考文本…不做严格 JSON 校验」 | ✅ |
| C 扫描式 JSON 提取 | 新增 `_scan_first_json_object()`（`raw_decode` 逐 `{` 尝试，不跨界拼接）+ `_extract_translation_text()`（回退路径宽松、正常路径严格，严格失败再补一次扫描兜底） | ✅ |
| D 可诊断 | 严格路径解析失败时 `logger.error` 带 `解析对象=正文` 与 `sanitize_text_for_log` 预览；异常文案附预览 | ✅ |
| E 提示词抑制 | 未做（可选） | ⏸ |

**测试**：`tests/test_ai_title_translation_service.py` 新增 8 例（`_pick_answer_piece` 1 + `_scan_first_json_object` 1 + `_extract_translation_text` 2 + 端到端 4），
全量 **23 passed**（改前 15），3 例 `client` fixture error 为 `--noconftest` 预期。
**红线用例 `test_stream_reasoning_only_content_is_used_not_failed` 通过**，v2.6.45 语义未回归。

版本：**v2.6.50**。

