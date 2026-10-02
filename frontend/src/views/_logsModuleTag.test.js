import { describe, expect, it } from 'vitest'

import {
  KNOWN_FUNCTION_TAGS,
  extractKnownTag,
  moduleFromLogger,
  parseModule,
} from './_logsModuleTag.js'

// 真实日志行取自 D:/program/prekikoeru/app (1).log（17869 行可解析样本）。
// 这些用例锁定的就是「模块下拉只有元数据/分类/解压/扫描 4 项」这个 bug 的修复。
describe('parseModule 功能类别解析', () => {
  const line = (level, loggerName, message) =>
    `2026-09-07 14:32:17 [${level}] ${loggerName} - ${message}`

  describe('① 正向白名单标签优先', () => {
    it('跳过 [INFO] 级别标记，取到真实功能标签 [DLsite]', () => {
      const raw = line('INFO', 'app.core.dlsite_service', '[DLsite] 正在请求 API: product.json')
      expect(parseModule('[DLsite] 正在请求 API: product.json', raw)).toBe('DLsite')
    })

    it('含连字符的全大写标记 [KIKOERU-SCAN] 被识别', () => {
      const raw = line('INFO', 'app.core.kikoeru_scan_listener', '[KIKOERU-SCAN] socket 断开，转入轮询兜底')
      expect(parseModule('[KIKOERU-SCAN] socket 断开，转入轮询兜底', raw)).toBe('KIKOERU-SCAN')
    })

    it('[RENAME] 被识别为真实标签', () => {
      const raw = line('INFO', 'app.core.rename_service', "[RENAME] 原始模板: '[{rjcode}][{work_name}]'")
      expect(parseModule('[RENAME] 原始模板', raw)).toBe('RENAME')
    })

    it('含空格的标签 [索引 watcher] 被识别', () => {
      const raw = line('INFO', 'app.core.library_index.watcher_driver', '[索引 watcher] 已启动 mode=watchdog')
      expect(parseModule('[索引 watcher] 已启动 mode=watchdog', raw)).toBe('索引 watcher')
    })

    it('带修饰的标签 [社团补全/封面缓存] 被识别', () => {
      const raw = line('INFO', 'app.core.circle_image_cache_service', '[社团补全/封面缓存] 使用缓存目录 path=/app/data/img')
      expect(parseModule('[社团补全/封面缓存] 使用缓存目录', raw)).toBe('社团补全/封面缓存')
    })
  })

  describe('② 伪标签/噪声绝不出现在下拉里', () => {
    // 这四项是实测「负向过滤会漏掉」的典型，白名单方案天然屏蔽
    it('[LocalScanner]（Python 类名派生，实测 3982 行）不被当作类别', () => {
      const raw = line('INFO', 'app.core.library_index.local_scanner',
        '[LocalScanner] 子树扫描完成 library=真库存 files=96 elapsed=0.02s')
      const result = parseModule('[LocalScanner] 子树扫描完成 library=真库存 files=96', raw)
      expect(result).not.toBe('LocalScanner')
      // 无已知标签 → logger 兜底为「索引维护」
      expect(result).toBe('索引维护')
    })

    it('[7z] / [VolumeSet] 等工具与前缀名不被当作类别', () => {
      expect(parseModule('[7z] 输出解码: utf-8', line('INFO', 'app.core.extract_service', '[7z] 输出解码: utf-8')))
        .toBe('解压')
      expect(parseModule('[VolumeSet] 检测到分卷模式', line('INFO', 'app.core.extract_service', '[VolumeSet] 检测到分卷模式')))
        .toBe('解压')
    })

    it('动态 RJ 号 [RJ01627139] 不被当作类别（否则下拉会被上千个 RJ 号撑爆）', () => {
      const raw = line('INFO', 'app.core.task_engine', '[RJ01627139] 任务提交 - ID: 0890e117')
      expect(parseModule('[RJ01627139] 任务提交 - ID: 0890e117', raw)).toBe('任务')
    })

    it('销售标题 / 社团名 / 引号串等噪声不被当作类别', () => {
      const cases = [
        ['[幸福少女] 开始扫描', 'app.core.file_processor'],
        ["['zh-TW'] 语言包", 'app.api.routes'],
        ['[Errno 28] 磁盘写入失败', 'app.core.http_download_service'],
        ['[な無] 规则匹配', 'app.core.filter_service'],
        ['[list] 列表结果', 'app.core.library_manager'],
      ]
      for (const [msg, lg] of cases) {
        const raw = line('INFO', lg, msg)
        const result = parseModule(msg, raw)
        expect(KNOWN_FUNCTION_TAGS.has(msg.slice(1, msg.indexOf(']')))).toBe(false)
        expect(result).not.toBe(msg.slice(1, msg.indexOf(']')))
      }
    })
  })

  describe('③ logger 模块名兜底映射', () => {
    it('无标签行按模块名推导类别', () => {
      expect(parseModule('库存浏览走索引: lib=真库存 path=/汉化',
        line('INFO', 'app.core.library_manager', '库存浏览走索引: lib=真库存 path=/汉化'))).toBe('库存')
      expect(parseModule('开始等待文件复制完成: /input/RJ01647973.zip',
        line('INFO', 'app.core.extract_service', '开始等待文件复制完成: /input/RJ01647973.zip'))).toBe('解压')
      expect(parseModule('检测到音频格式分布: {mp3: 10}',
        line('INFO', 'app.core.filter_service', '检测到音频格式分布: {mp3: 10}'))).toBe('分类')
    })

    it('watcher 子模块映射到「索引 watcher」而非「索引维护」（前缀顺序敏感）', () => {
      expect(moduleFromLogger(line('INFO', 'app.core.library_index.watcher_driver', 'x'))).toBe('索引 watcher')
      expect(moduleFromLogger(line('INFO', 'app.core.library_index.service', 'x'))).toBe('索引维护')
    })

    it('无法识别的模块名返回 null', () => {
      expect(moduleFromLogger(line('INFO', 'engineio.client', 'x'))).toBeNull()
      expect(moduleFromLogger(line('INFO', 'apscheduler.scheduler', 'x'))).toBeNull()
    })
  })

  describe('④ 关键词兜底与未分类', () => {
    it('无标签且模块名未知时按关键词归类', () => {
      expect(parseModule('7z 解压完成', line('INFO', '__main__', '7z 解压完成'))).toBe('解压')
      expect(parseModule('抓取元数据完成', line('INFO', '__main__', '抓取元数据完成'))).toBe('元数据')
    })

    it('第三方/基础设施日志返回 null（UI 显示为「未分类」）', () => {
      expect(parseModule('websocket-client package not installed',
        line('WARNING', 'engineio.client', 'websocket-client package not installed'))).toBeNull()
    })
  })

  describe('extractKnownTag 判据', () => {
    it('跳过级别标记，只认白名单标签', () => {
      expect(extractKnownTag('2026-09-07 14:32:17 [INFO] app.core.x - [DLsite] hi')).toBe('DLsite')
      // [INFO] 之后没有白名单标签 → null
      expect(extractKnownTag('2026-09-07 14:32:17 [INFO] app.core.x - 无标签消息')).toBeNull()
      // 伪标签不在白名单 → null
      expect(extractKnownTag('2026-09-07 14:32:17 [INFO] app.core.x - [LocalScanner] hi')).toBeNull()
    })

    it('空输入安全返回 null', () => {
      expect(extractKnownTag('')).toBeNull()
      expect(extractKnownTag(null)).toBeNull()
    })
  })
})
