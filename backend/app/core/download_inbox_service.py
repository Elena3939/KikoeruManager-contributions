"""下载收件箱（Download Inbox）。

外部来源（浏览器插件、脚本）把「规范格式」的下载输入推送到这里暂存，
前端在进入 ASMR 同步页 / 切回标签页时拉取，合并进对应面板的输入框。

职责边界（很重要）：

- 只做「文本搬运」：不解析链接语义、不触发预览、不创建下载任务、
  不修改任何业务配置；
- 独立 JSON 文件落盘（与 ``config.yaml`` 同级目录），重启不丢；
- 与 ``preview_name_hints`` 的分工：那边负责解析规范行，这边只负责
  按「分享身份」去重、暂存、过期淘汰。

为什么按「分享身份」而不是整行去重
----------------------------------

插件重复抓同一页面（密码从无到有、或轻微改动）时，整行去重会留下两条
同链接记录，而 ack 又按 URL 身份匹配，用户体验上表现为输入框里出现
重复行。这里对同一 ``share_identity`` 只保留最新一行，重复推送等于覆盖。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import logging

from .preview_name_hints import share_identity

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("baidu", "http")
MAX_TEXT_BYTES = 256 * 1024
MAX_LINES_PER_PUSH = 200

_TAG_NORMALIZE_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?:提取码|提取口令|访问码)\s*[:：=]\s*", re.IGNORECASE), "提取码: "),
    (
        re.compile(
            r"(?:解压密码|解壓密碼|解压码|解壓碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼)"
            r"\s*[:：=]\s*",
            re.IGNORECASE,
        ),
        "解压码: ",
    ),
)

_URL_LIKE_RE = re.compile(r"https?://[^\s<>\"'|｜]+", re.IGNORECASE)
_BARE_URL_LIKE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:pan|yun|eyun)\.baidu\.com(?:/[^\s<>\"'|｜]*)?",
    re.IGNORECASE,
)


def normalize_provider(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SUPPORTED_PROVIDERS else "baidu"


def normalize_line(line: str) -> str:
    """归一化整行文本，用于兜底去重键。"""
    text = str(line or "").replace("｜", "|").replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _TAG_NORMALIZE_RULES:
        text = pattern.sub(replacement, text)
    return text


def line_identity(line: str) -> str:
    """行级身份：有 URL 用分享身份（去掉 pwd 等参数），否则退回归一化整行。"""
    text = str(line or "")
    match = _URL_LIKE_RE.search(text) or _BARE_URL_LIKE_RE.search(text)
    if match:
        identity = share_identity(match.group(0))
        if identity:
            return identity
    return normalize_line(text)


def is_private_address(host: str) -> bool:
    """判断来源地址是否属于私网/回环，供「局域网免 token」模式使用。"""
    text = str(host or "").strip()
    if not text:
        return False
    if text in ("localhost", "testclient"):
        return True
    if text.startswith("::ffff:"):
        text = text[7:]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
    )


def _data_dir() -> Path:
    """收件箱落盘目录：优先 DATA_PATH，其次 config 文件的祖父目录。"""
    data_path = str(os.environ.get("DATA_PATH") or "").strip()
    if data_path:
        return Path(data_path).resolve()
    try:
        from ..config.settings import get_config_file_path

        config_path = Path(get_config_file_path()).resolve()
    except Exception:
        return Path("./data").resolve()
    if config_path.parent.name == "config":
        return config_path.parent.parent
    return config_path.parent


class DownloadInboxService:
    """下载收件箱（单例）。"""

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        self._store_path = store_path or (_data_dir() / "download_inbox.json")
        self._loaded = False
        self._last_write_at = 0.0

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    @property
    def store_path(self) -> Path:
        return self._store_path

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._store_path
        try:
            if not path.is_file():
                return
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            logger.warning("[下载收件箱] 读取落盘文件失败，按空队列处理 path=%s", path, exc_info=True)
            return
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        restored: List[Dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            line = str(entry.get("line") or "").strip()
            if not line:
                continue
            restored.append(
                {
                    "id": str(entry.get("id") or secrets.token_hex(4)),
                    "provider": normalize_provider(entry.get("provider")),
                    "line": line,
                    "identity": str(entry.get("identity") or line_identity(line)),
                    "source": str(entry.get("source") or ""),
                    "client_id": str(entry.get("client_id") or ""),
                    "created_at": int(entry.get("created_at") or time.time()),
                    "last_seen_at": int(entry.get("last_seen_at") or time.time()),
                }
            )
        self._items = restored

    def _persist(self) -> None:
        """原子写：temp 文件 + os.replace，半成品文件不会被读到。"""
        path = self._store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "items": self._items}
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
            self._last_write_at = time.time()
        except Exception:
            logger.warning("[下载收件箱] 落盘失败 path=%s", path, exc_info=True)

    def _prune_locked(self, ttl_hours: int, max_items: int) -> None:
        now = time.time()
        deadline = now - max(1, int(ttl_hours)) * 3600
        kept = [item for item in self._items if int(item.get("last_seen_at") or 0) >= deadline]
        if len(kept) > max_items:
            kept.sort(key=lambda item: int(item.get("last_seen_at") or 0))
            kept = kept[-max_items:]
        self._items = kept

    def _ttl_and_limit(self) -> Tuple[int, int]:
        try:
            from ..config.settings import get_config

            config = get_config()
            section = getattr(config, "http_downloader", None)
            ttl_hours = int(getattr(section, "download_inbox_ttl_hours", 72) or 72)
            max_items = int(getattr(section, "download_inbox_max_items", 500) or 500)
        except Exception:
            ttl_hours, max_items = 72, 500
        return max(1, ttl_hours), max(1, max_items)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    @staticmethod
    def _inbox_config() -> Dict[str, Any]:
        try:
            from ..config.settings import get_config

            section = getattr(get_config(), "http_downloader", None)
        except Exception:
            section = None
        return {
            "enabled": bool(getattr(section, "download_inbox_enabled", False)),
            "token": str(getattr(section, "download_inbox_token", "") or ""),
            "lan_only": bool(getattr(section, "download_inbox_lan_only", True)),
        }

    def authorize_push(self, client_host: str, token: str) -> Tuple[bool, int, str]:
        """校验外部写入权限，返回 ``(ok, status_code, detail)``。"""
        settings = self._inbox_config()
        if not settings["enabled"]:
            return False, 403, "下载收件箱未启用，请先在设置中开启"
        expected = str(settings["token"] or "")
        provided = str(token or "")
        if expected:
            import hmac

            if not provided or not hmac.compare_digest(provided, expected):
                return False, 401, "收件箱令牌不匹配"
            return True, 200, ""
        if settings["lan_only"] and not is_private_address(client_host):
            return False, 403, "仅允许局域网来源写入（或请在设置中配置令牌）"
        return True, 200, ""

    # ------------------------------------------------------------------
    # 业务
    # ------------------------------------------------------------------

    def push(
        self,
        provider: Any,
        text: str,
        source: str = "",
        client_id: str = "",
    ) -> Dict[str, Any]:
        raw_text = str(text or "")
        if len(raw_text.encode("utf-8", errors="ignore")) > MAX_TEXT_BYTES:
            raise ValueError("正文超过 256 KB")
        lines = [line.strip() for line in raw_text.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            raise ValueError("没有可用的内容")
        if len(lines) > MAX_LINES_PER_PUSH:
            raise ValueError(f"单次最多推送 {MAX_LINES_PER_PUSH} 行")

        normalized_provider = normalize_provider(provider)
        accepted = 0
        duplicated = 0
        now = int(time.time())
        ttl_hours, max_items = self._ttl_and_limit()

        with self._lock:
            self._ensure_loaded()
            self._prune_locked(ttl_hours, max_items)
            index = {str(item.get("identity") or ""): item for item in self._items}
            for line in lines:
                identity = line_identity(line)
                if not identity:
                    continue
                existing = index.get(identity)
                if existing is not None:
                    existing["provider"] = normalized_provider
                    existing["line"] = line
                    existing["last_seen_at"] = now
                    if source:
                        existing["source"] = str(source)
                    if client_id:
                        existing["client_id"] = str(client_id)
                    duplicated += 1
                    continue
                item = {
                    "id": secrets.token_hex(4),
                    "provider": normalized_provider,
                    "line": line,
                    "identity": identity,
                    "source": str(source or ""),
                    "client_id": str(client_id or ""),
                    "created_at": now,
                    "last_seen_at": now,
                }
                self._items.append(item)
                index[identity] = item
                accepted += 1
            self._persist()
            total = len(self._items)
        return {"accepted": accepted, "duplicated": duplicated, "total": total}

    def pending(self, provider: Optional[str] = None) -> Dict[str, Any]:
        wanted = normalize_provider(provider) if str(provider or "").strip() else ""
        ttl_hours, max_items = self._ttl_and_limit()
        with self._lock:
            self._ensure_loaded()
            self._prune_locked(ttl_hours, max_items)
            items = [
                {
                    "id": item["id"],
                    "provider": item["provider"],
                    "line": item["line"],
                    "created_at": item["created_at"],
                    "last_seen_at": item["last_seen_at"],
                }
                for item in self._items
                if not wanted or item.get("provider") == wanted
            ]
            items.sort(key=lambda entry: int(entry.get("created_at") or 0))
            total = len(self._items)
        return {"items": items, "total": total}

    def ack(self, text: str, provider: Optional[str] = None) -> Dict[str, Any]:
        """按分享身份确认消费；幂等，未命中不报错。"""
        wanted = normalize_provider(provider) if str(provider or "").strip() else ""
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        identities = {line_identity(line) for line in lines}
        identities.discard("")
        removed = 0
        with self._lock:
            self._ensure_loaded()
            if not identities:
                return {"removed": 0, "total": len(self._items)}
            kept: List[Dict[str, Any]] = []
            for item in self._items:
                matched = str(item.get("identity") or "") in identities
                if wanted and item.get("provider") != wanted:
                    matched = False
                if matched:
                    removed += 1
                    continue
                kept.append(item)
            self._items = kept
            if removed:
                self._persist()
            total = len(self._items)
        return {"removed": removed, "total": total}

    def clear(self, provider: Optional[str] = None) -> Dict[str, Any]:
        wanted = normalize_provider(provider) if str(provider or "").strip() else ""
        with self._lock:
            self._ensure_loaded()
            if wanted:
                kept = [item for item in self._items if item.get("provider") != wanted]
                removed = len(self._items) - len(kept)
                self._items = kept
            else:
                removed = len(self._items)
                self._items = []
            self._persist()
            total = len(self._items)
        return {"removed": removed, "total": total}

    def status(self) -> Dict[str, Any]:
        settings = self._inbox_config()
        ttl_hours, max_items = self._ttl_and_limit()
        with self._lock:
            self._ensure_loaded()
            self._prune_locked(ttl_hours, max_items)
            counts: Dict[str, int] = {}
            last_seen = 0
            for item in self._items:
                key = str(item.get("provider") or "baidu")
                counts[key] = counts.get(key, 0) + 1
                last_seen = max(last_seen, int(item.get("last_seen_at") or 0))
            total = len(self._items)
            path = str(self._store_path)
        return {
            "enabled": settings["enabled"],
            "token_configured": bool(settings["token"]),
            "lan_only": settings["lan_only"],
            "total": total,
            "providers": counts,
            "last_seen_at": last_seen,
            "ttl_hours": ttl_hours,
            "max_items": max_items,
            "store_path": path,
        }

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._items = []
            self._loaded = True
            self._persist()


_service: Optional[DownloadInboxService] = None
_service_lock = threading.Lock()


def get_download_inbox_service() -> DownloadInboxService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = DownloadInboxService()
    return _service
