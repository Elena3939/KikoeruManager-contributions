// 日志「模块（功能类别）」解析。
//
// 后端日志格式：`日期 时间 [级别] 模块名 - [功能标签] 正文`
//   例：2026-09-07 14:32:17 [INFO] app.core.library_index.local_scanner - [LocalScanner] 子树扫描完成 ...
//
// ── 历史实现（v2.6.45 及以前）的三个问题 ──────────────────────────────
//   1) 用 /\[([^\]]+)\]/ 取「第一个」方括号 —— 真实日志里那恒为 `[INFO]`（级别），
//      于是标签分支永不命中，100% 落到中文关键词猜测。实测 `app.log` 17869 行：
//      非 null 结果只有 7 个取值（扫描 5363 / 元数据 4655 / 解压 1394 / 分类 348
//      / 清理 235 / 密码 43），这正是模块下拉「只有元数据/分类/解压/扫描这 4 项」的根因。
//   2) 白名单只认 4 项（KikoeruManager/CONFIG/RENAME/RJ字幕），真实标签全被拒。
//   3) 关键词顺序把泛词放前，`[元数据] …准备分类` 会被误判成「分类」。
//
// ── 现策略（v2.6.46，三级判定）──────────────────────────────────────
//   ① **正向白名单**：行内方括号出现「已知功能标签」时精确采用（最准）。
//   ② **logger 兜底映射**：无已知标签时，按 `模块名` 推导类别
//      （如 app.core.extract_service → 解压）。这是覆盖率主力（实测约 60% 的行走这条）。
//   ③ **关键词兜底**：最后才用正文关键词猜（兼容无标签旧日志）。
//
//   为什么用「正向白名单」而不是「负向排除伪标签」：
//   实测日志里正文首方括号共有 89 种取值，负向过滤永远有漏网（销售标题、
//   社团名、缩略名 list/cache、Errno 28、规则正则片段 な無 …）。
//   负向方案实测产出 185 个下拉项且大量噪声；正向方案产出 46 个干净项、覆盖率 98.4%。
//   新增功能时只需往 KNOWN_FUNCTION_TAGS / LOGGER_CATEGORY_MAP 补一条。

export const LOG_LEVEL_TAG_SET = new Set([
  'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'NOTSET', 'WARN',
])

// ── ① 已知功能标签白名单（实测确认，语义明确、可由 logger 名对应）──────
export const KNOWN_FUNCTION_TAGS = new Set([
  // 业务功能（主体）
  'DLsite', 'PikPak', 'PikPak诊断', 'AI标题', 'AI字幕',
  'KIKOERU-SCAN', 'KIKOERU-DB', 'RENAME',
  '索引维护', '索引 watcher', '索引完整性', '索引追赶',
  '字幕同步', '字幕补配', '繁简转换',
  '社团补全', '社团补全/封面缓存', '社团补全·外部搜索',
  '分类', '元数据', '解压', '重命名', '去重', '清理', '归档',
  '移库', '库存', '下载', 'HTTP下载', '百度网盘', '文件处理',
  // 系统/基础
  'API请求', '通知', '任务', '数据库', '事件循环', '启动', '更新检测',
  '邮件监听', '延后归档', '资源预算', '重试调度器', '等待重试',
  '操作记录', '任务阶段指标', '任务中心物化', '通知清理', '批量 API重命名',
  'zip快路径', '未知',
])

// ── ② logger 模块名 → 功能类别（兜底映射）────────────────────────────
// 顺序敏感：更具体的键必须排在其前缀之前（如 library_index.watcher 先于 library_index）。
const LOGGER_CATEGORY_MAP = [
  ['library_index.watcher', '索引 watcher'],
  ['library_index', '索引维护'],
  ['library_manager', '库存'],
  ['dlsite', 'DLsite'],
  ['kikoeru_scan', 'KIKOERU-SCAN'],
  ['kikoeru_db', '数据库'],
  ['kikoeru_duplicate', '去重'],
  ['subtitle_sync', '字幕同步'],
  ['ai_title', 'AI标题'],
  ['ai_subtitle', 'AI字幕'],
  ['extract_service', '解压'],
  ['filter_service', '分类'],
  ['classifier', '分类'],
  ['metadata_service', '元数据'],
  ['rename_service', '重命名'],
  ['version_dedup', '去重'],
  ['task_engine', '任务'],
  ['task_notification', '通知'],
  ['circle_completion', '社团补全'],
  ['circle_image_cache', '社团补全/封面缓存'],
  ['circle_external_search', '社团补全·外部搜索'],
  ['http_download', '下载'],
  ['email_watcher', '邮件监听'],
  ['password_cleanup', '清理'],
  ['processed_archive_cleanup', '清理'],
  ['deferred_archive', '延后归档'],
  ['resource_budget', '资源预算'],
  ['update_check', '更新检测'],
  ['file_processor', '文件处理'],
  ['asmr_resource', 'ASMR'],
  ['database', '数据库'],
  ['routes', 'API请求'],
]

// 伪标签（不作为功能类别展示，仅在 logger 兜底前的快速排除中用到）
// 注：正向或负向判据都已由 KNOWN_FUNCTION_TAGS 取代，这里仅保留 RJ 号识别用于日志去噪。
const RJ_TAG_RE = /^RJ\d{6,}$/i
const MAX_TAG_LENGTH = 32

/** 从原始行中提取「已知功能标签」（跳过级别标记，只认白名单）。 */
export function extractKnownTag (rawLine) {
  const raw = String(rawLine || '')
  if (!raw) return null
  for (const match of raw.matchAll(/\[([^[\]]+)\]/g)) {
    const tag = match[1].trim()
    if (!tag || tag.length > MAX_TAG_LENGTH) continue
    if (LOG_LEVEL_TAG_SET.has(tag.toUpperCase())) continue // 跳过 [INFO]
    if (KNOWN_FUNCTION_TAGS.has(tag)) return tag
  }
  return null
}

/** 按 logger 模块名推导功能类别；无法识别返回 null。 */
export function moduleFromLogger (rawLine) {
  const raw = String(rawLine || '')
  if (!raw) return null
  // 行格式：`日期 时间 [级别] 模块名 - 正文`，模块名是级别方括号之后、` - ` 之前的 token
  const match = raw.match(/\]\s+(\S+)\s+-\s/)
  if (!match) return null
  const loggerName = match[1]
  for (const [key, category] of LOGGER_CATEGORY_MAP) {
    if (loggerName.includes(key)) return category
  }
  return null
}

/** 关键词兜底（仅用于既无已知标签、logger 也无法识别的旧日志）。 */
function keywordFallback (message) {
  const text = String(message || '')
  if (text.includes('解压') || text.includes('压缩')) return '解压'
  if (text.includes('元数据')) return '元数据'
  if (text.includes('扫描')) return '扫描'
  if (text.includes('分类') || text.includes('规则')) return '分类'
  if (text.includes('密码')) return '密码'
  if (text.includes('清理') || text.includes('删除')) return '清理'
  return null
}

/**
 * 解析日志行的功能类别（模块）。
 * @param {string} message 正文
 * @param {string} rawLine 原始整行（用于取标签与模块名）
 * @returns {string|null} 类别；null 表示无法归类（UI 侧显示为「未分类」）
 */
export function parseModule (message, rawLine) {
  // ① 正向白名单标签（最准）
  const tag = extractKnownTag(rawLine)
  if (tag) return tag
  // ② logger 模块名兜底（覆盖率主力）
  const fromLogger = moduleFromLogger(rawLine)
  if (fromLogger) return fromLogger
  // ③ 关键词兜底
  return keywordFallback(message)
}

export { RJ_TAG_RE, LOGGER_CATEGORY_MAP }
export default parseModule
