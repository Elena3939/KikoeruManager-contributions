"""库存浏览「列一层目录」统一读入口（重构阶段 1 第二步）。

背景：库存浏览曾有多条读入口（browser/files、library/files、folder-contents、
list-folders…），各自在输出层做字段整形 + 落盘/远程回退，导致「选库存不加载」、
「刷新显示的不是实际文件」。

本模块只做一件事：复用 ``service.list_children_page`` —— 也就是 browser/files 本地
索引分支（library_manager._list_files_via_index，约 4324 行）背后那句真正的
"列一层目录"实现 —— 把结果交给 ``listing_view.build_listing_response`` 统一序列化。

新端点 ``GET /api/library/browser/listing`` 调用本模块；后续旧端点的薄适配器也收敛到
同一实现（本阶段因各端点输出结构差异过大，旧端点暂不改造，见交付报告）。

注意坐标系统：本模块把入参 ``relative_path`` 当作**相对库存根（index 坐标）**的路径，
直接作为 ``list_children_page`` 的 ``parent_path``；根目录归一化为空串。这与
``IndexEntry.relative_path`` 的坐标一致，因此条目无需再做路径拼接。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .listing_view import build_listing_response

_LISTING_NEW_ENV = "KIKOERU_LIBRARY_LISTING_NEW"

# 统一响应契约的必含字段（见 docs/library-browser-refactor-plan.md §3.4）
CANONICAL_RESPONSE_KEYS = ("items", "source", "generation", "fresh_at", "cursor", "has_more")

# 默认每页子项数；键集翻页用 cursor，避免大目录全量拉回
DEFAULT_PAGE_SIZE = 500


def listing_new_enabled() -> bool:
    """原子回滚开关：默认开启（"1"）；置为 "0" 时新端点回退旧实现（501）。

    不依赖任何模块级状态，读取即生效，便于线上随时回滚。
    """
    return os.environ.get(_LISTING_NEW_ENV, "1") != "0"


def _index_parent_path(relative_path: str) -> str:
    """把 API 的 relative_path 归一化为索引 parent_path（根目录 -> 空串）。"""
    return str(relative_path or "").strip().replace("\\", "/").strip("/")


def _entry_to_raw(entry: Any) -> dict:
    """把 IndexEntry 摊平成 normalize_listing_item 能识别的裸字典。

    IndexEntry.mtime 是毫秒级，统一形状要求秒级，这里先降一档。
    """
    mtime_ms = getattr(entry, "mtime", None)
    mtime = (float(mtime_ms) / 1000.0) if mtime_ms else 0.0
    is_dir = getattr(entry, "entry_type", None) == "dir"
    raw: dict[str, Any] = {
        "name": getattr(entry, "name", ""),
        "is_dir": is_dir,
        "size": int(getattr(entry, "size", 0) or 0),
        "mtime": mtime,
        "relative_path": getattr(entry, "relative_path", ""),
    }
    if is_dir:
        raw["child_count"] = int(getattr(entry, "file_count", 0) or 0)
    return raw


# ---------------------------------------------------------------------------
# verify 浅扫辅助（阶段 2）：只对当前层做一层 stat，不做全树扫描。
# ---------------------------------------------------------------------------


def _entry_stat_is_stale(entry: Any, stat_result: os.stat_result) -> bool:
    """(size, mtime) 准确性护栏：与 library_manager._index_entry_stat_is_stale 同判据。

    mtime 毫秒差 >1000ms 或文件 size 不一致即 stale；目录 size 允许滞后（累计值）。
    """
    indexed_mtime = getattr(entry, "mtime", None)
    try:
        current_mtime = int(stat_result.st_mtime * 1000)
    except (TypeError, ValueError, OSError, OverflowError):
        current_mtime = None
    if indexed_mtime and current_mtime and abs(int(indexed_mtime) - current_mtime) > 1000:
        return True
    if getattr(entry, "entry_type", "") == "file":
        try:
            return int(getattr(entry, "size", 0) or 0) != int(stat_result.st_size or 0)
        except (TypeError, ValueError):
            return True
    return False


def stat_current_layer(
    library: Any,
    entries: List[Any],
    *,
    absolute_path_for: Optional[Callable[[Any], str]] = None,
) -> Tuple[List[Any], List[Dict[str, str]]]:
    """对当前层条目逐个 os.stat（一层，不递归），拆分有效/缺失条目。

    返回 ``(valid_entries, missing)``；``missing`` 是
    ``{"relative_path", "absolute_path", "reason"}`` 列表（reason: missing|type_changed）。
    丢失/类型变化条目由调用方决定标 stale 还是触发修复。
    """
    valid: List[Any] = []
    missing: List[Dict[str, str]] = []
    for entry in entries:
        path = str(absolute_path_for(entry) if absolute_path_for else getattr(entry, "absolute_path", "") or "")
        if not path:
            continue
        try:
            stat_result = os.stat(path)
            is_dir = os.path.isdir(path)
        except OSError:
            missing.append({
                "relative_path": str(getattr(entry, "relative_path", "") or ""),
                "absolute_path": path,
                "reason": "missing",
            })
            continue
        if (getattr(entry, "entry_type", "") == "dir") != bool(is_dir):
            missing.append({
                "relative_path": str(getattr(entry, "relative_path", "") or ""),
                "absolute_path": path,
                "reason": "type_changed",
            })
            continue
        valid.append(entry)
    return valid, missing


def _index_generation_metadata(service: Any, library_id: str) -> tuple[int, str]:
    """读 active_generation + updated_at；失败降级为未知元数据，不阻断浏览。"""
    generation = 0
    fresh_at = ""
    try:
        status = service.get_status(library_id)
        generation = int(getattr(status, "active_generation", 0) or 0)
        updated_at = getattr(status, "updated_at", None)
        if updated_at:
            fresh_at = datetime.fromtimestamp(
                float(updated_at) / 1000.0, tz=timezone.utc
            ).isoformat()
    except Exception:
        pass
    return generation, fresh_at


def _enqueue_subtree_reconcile(library: Any, relative_path: str) -> bool:
    """把子树 reconcile 入队（mutation 服务范式，与 library_manager 一致）。

    任何失败只记 debug，不影响本次响应 —— 失效是尽力而为，读取路径绝不被它阻塞。
    """
    try:
        from . import get_library_index_mutation_service

        effect = {
            "kind": "reconcile",
            "relative_path": str(relative_path or ""),
            "scope": "subtree",
        }
        service = get_library_index_mutation_service()
        prepared = service.prepare(
            kind="listing_verify",
            effects_by_library={library.id: [effect]},
            idempotency_key=f"listing_verify:{library.id}:{os.urandom(8).hex()}",
        )
        service.mark_filesystem_started(prepared.operation_id)
        service.finalize(
            prepared.operation_id,
            actual_effects_by_library={library.id: [effect]},
            actual_result={"source": "listing_verify", "path": str(relative_path or "")},
        )
        return True
    except Exception:
        return False


def build_library_listing(
    *,
    library: Any,
    relative_path: str = "",
    cursor: str = "",
    mode: str = "index",
    page_size: int = DEFAULT_PAGE_SIZE,
    get_service: Optional[Callable[[], Any]] = None,
    stat_layer: bool = False,
) -> dict:
    """列一层目录并统一序列化。

    ``get_service`` 可注入（测试打桩用）；缺省时从 library_index 取真实 service。

    ``mode=index``（默认）：纯快照读，零磁盘 IO。
    ``mode=verify``：快照读之后对**当前层**逐条 os.stat（一层，不递归，不做全树扫描），
    与磁盘不一致的条目标 ``stale=true``；快照条目磁盘已消失（幽灵）与
    快照里没有而磁盘上有的条目（disk_only）都会触发该子树 reconcile 入队。
    **注意**：disk_only 这一步（一层 readdir）**仅在本页覆盖整个目录时执行** ——
    大目录用游标翻页时 ``entries`` 只是切片，与 ``os.scandir`` 全量做差集会把
    「本页之后的条目」整批误判成 disk_only（v2.6.48 修复，见 `_verify_current_layer`）。
    stat 失败（远程/网络盘等）自动回落 index 模式并标 ``verify_failed=true``，
    不阻塞页面。``stat_layer=True`` 等价于 ``mode=verify``（程序化调用口）。
    """
    if get_service is None:
        from . import get_library_index_service as get_service
    service = get_service()

    parent_path = _index_parent_path(relative_path)
    want_verify = stat_layer or str(mode or "").strip().lower() == "verify"

    payload = service.list_children_page(
        library.id,
        parent_path,
        sort_by="name",
        sort_order="asc",
        offset=0,
        limit=page_size,
        page_cursor=cursor or None,
    )

    entries = list(payload.get("entries") or [])
    next_cursor = str(payload.get("next_page_cursor") or "")
    has_more = bool(next_cursor)
    # 本页是否覆盖**整个目录**（verify 磁盘差集的判据，见 _verify_current_layer）：
    # 用了游标就必然是切片；只有「无游标 + 无下一页 + 声明总数不超过本页条数」才是全量。
    # list_children_page 恒传 include_total=True，故 payload["total"] 是目录全量声明数。
    declared_total = int(payload.get("total") or 0)
    page_covers_directory = (
        not has_more
        and not bool(payload.get("used_page_cursor"))
        and declared_total <= len(entries)
    )

    generation, fresh_at = _index_generation_metadata(service, library.id)

    extra: dict[str, Any] = {}
    verify_failed = False
    stale_paths: set[str] = set()
    disk_only_raws: list[dict[str, Any]] = []
    ghost_paths: list[str] = []
    valid_entries: list[Any] = list(entries)

    if want_verify and library.type == "local":
        (
            verify_failed,
            valid_entries,
            stale_paths,
            disk_only_raws,
            ghost_paths,
        ) = _verify_current_layer(
            library,
            service,
            parent_path,
            entries,
            page_covers_directory=page_covers_directory,
        )
        if disk_only_raws or ghost_paths:
            # 快照缺条目 / 存在幽灵 => 物化滞后或 watcher 漏事件，入队子树 reconcile
            # （阶段 3 前的护栏）。v2.6.48 起幽灵也单独构成触发条件，不再只依赖 disk_only。
            _enqueue_subtree_reconcile(library, parent_path)

    source = "verify" if want_verify else "index"

    def _raw_with_stale(entry: Any) -> dict:
        raw = _entry_to_raw(entry)
        if str(raw.get("relative_path") or "") in stale_paths:
            raw["stale"] = True
        return raw

    response = build_listing_response(
        [_raw_with_stale(entry) for entry in valid_entries] + disk_only_raws,
        source=source,
        base_relative_path=parent_path,
        generation=generation,
        fresh_at=fresh_at,
        cursor=next_cursor,
        has_more=has_more,
        verify_failed=verify_failed,
    )
    # 回显请求模式，便于前端按 mode 路由
    response["mode"] = str(mode or "index")
    return response


def _verify_current_layer(
    library: Any,
    service: Any,
    parent_path: str,
    entries: List[Any],
    *,
    page_covers_directory: bool = True,
) -> tuple[bool, List[Any], set[str], list[dict[str, Any]], list[str]]:
    """对当前层做一层浅扫。

    返回 ``(verify_failed, valid_entries, stale_relative_paths, disk_only_raws, ghost_paths)``：
    - stat 异常（目录不可达/远程盘）→ verify_failed=True，调用方回落 index；
    - 快照条目 stat 不一致 → 标 stale（但保留在 valid_entries 里）；
    - 快照条目磁盘已消失/类型变化 → **幽灵条目**：进 ghost_paths、不进 valid_entries、不进响应；
    - 磁盘有而快照没有 → disk_only_raws（统一形状 raw，附进响应）。

    ``page_covers_directory``（v2.6.48）：**仅当本页覆盖整个目录时才执行第 2 步磁盘差集**。
    旧实现无条件差集，但 ``entries`` 是 ``limit=page_size`` 的**本页**、``os.scandir`` 是
    **全量**，于是任何子项数 > page_size 的目录都会把「本页之后的条目」整批误判成
    disk_only：响应里塞入重复条目（且超过 page_size），并**每次 verify 都误触发一次
    子树 reconcile**（与 FIX_PLAN §3.2.1 描述的自激循环同源）。判据由调用方按
    ``payload["total"]`` / 游标算出，本函数不额外做 IO。
    """
    verify_failed = False
    stale_paths: set[str] = set()
    disk_only_raws: list[dict[str, Any]] = []
    ghost_paths: list[str] = []

    # 1) 快照条目逐个 stat：缺失/类型变化 → 幽灵条目（丢弃）；内容不一致 → 标 stale 保留
    valid_entries: List[Any] = []
    root_abs = os.path.abspath(library.root_path)
    for entry in entries:
        rel = str(getattr(entry, "relative_path", "") or "")
        absolute = str(getattr(entry, "absolute_path", "") or "") or (
            os.path.join(root_abs, *rel.split("/")) if rel else root_abs
        )
        try:
            stat_result = os.stat(absolute)
            is_dir = os.path.isdir(absolute)
        except OSError:
            stale_paths.add(rel)
            ghost_paths.append(rel)
            continue  # 幽灵条目：不进响应
        if (getattr(entry, "entry_type", "") == "dir") != bool(is_dir):
            stale_paths.add(rel)
            ghost_paths.append(rel)
            continue
        valid_entries.append(entry)
        if _entry_stat_is_stale(entry, stat_result):
            stale_paths.add(rel)

    # 2) 磁盘直接子项 vs 快照：磁盘有而快照没有 → disk_only（一层 readdir，不递归）
    #    仅在「本页覆盖整个目录」时执行，否则差集没有意义（本页之外的条目会被整批
    #    算成 disk_only）。ghost_paths 已在上一步单独收集，不受本步骤跳过影响。
    if not page_covers_directory:
        return verify_failed, valid_entries, stale_paths, disk_only_raws, ghost_paths

    target_abs = os.path.join(root_abs, *parent_path.split("/")) if parent_path else root_abs
    try:
        indexed_names = {str(getattr(entry, "name", "") or "") for entry in valid_entries}
        with os.scandir(target_abs) as it:
            for disk_entry in it:
                name = disk_entry.name
                if not name or name.startswith(".") or name.startswith("_"):
                    continue
                if name in indexed_names:
                    continue
                try:
                    st = disk_entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                disk_only_raws.append({
                    "name": name,
                    "is_dir": disk_entry.is_dir(follow_symlinks=False),
                    "size": 0 if disk_entry.is_dir(follow_symlinks=False) else int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "relative_path": f"{parent_path}/{name}" if parent_path else name,
                    "child_count": -1,
                    "stale": True,
                })
    except OSError:
        # 当前层不可达：verify 失败，回落 index 模式（stale 不标，快照行原样返回）
        return True, list(entries), set(), [], []

    return verify_failed, valid_entries, stale_paths, disk_only_raws, ghost_paths
