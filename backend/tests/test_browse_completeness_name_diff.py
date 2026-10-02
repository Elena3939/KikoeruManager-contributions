"""浏览完整性探测（名字级 diff）回归测试。

背景（v2.6.40 真机反馈）：进入目录显示旧内容，需等 ~2s 轮询自纠。
根因：默认浏览链路（browser/files 索引分支）的完整性探测用
``len(fs_names) > index_total`` 计数比较——重命名/移动是 -1+1 计数守恒，
探测永远不触发，快照里的旧名字（幽灵）+ 缺失的新名字直接返回给前端。

修复：`_list_files_via_index` 的本地分支改为**名字集合 diff**——
磁盘 readdir 一次（本来就有），与索引条目名对比，任何差异都
`return None`（回退 _list_files 真扫盘，本次响应即正确）并入队 reconcile。

打桩原则：不连库。FakeEntry 直接构造，os.listdir 用 monkeypatch 控制，
验证三类场景：磁盘多（新增）、快照多（幽灵/重命名旧名）、两侧同数但名字不同（重命名）。
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.library_manager import LibraryManager  # noqa: E402


def _manager() -> LibraryManager:
    return LibraryManager.__new__(LibraryManager)


def _entry(name, relative_path, entry_type="file", size=100):
    return SimpleNamespace(
        library_id="lib1",
        entry_type=entry_type,
        relative_path=relative_path,
        name=name,
        rjcode=None,
        parent_path="",
        size=size,
        file_count=0,
        mtime=None,
        depth=None,
        absolute_path=f"D:/data/lib/{relative_path}",
        indexed_at=0,
        generation=1,
        materialized_seq=0,
    )


def _run_probe(monkeypatch, tmp_path, entries, disk_names):
    """驱动 _list_files_via_index 的 diff 探测，返回 (indexed_result, reconcile_calls)。"""
    from app.core import library_index as index_pkg

    manager = _manager()

    monkeypatch.setattr(
        LibraryManager,
        "_library_uses_inventory_index",
        lambda self, library: True,
    )
    monkeypatch.setattr(
        LibraryManager,
        "_index_has_usable_snapshot",
        lambda self, service, library_id: True,
    )
    monkeypatch.setattr(
        LibraryManager,
        "_validate_local_index_entries_for_read",
        lambda self, library, ents, return_stale_paths=False: (
            [e for e in ents], set()
        ),
    )
    # os.listdir 返回磁盘真名（带 skip 过滤由 _should_skip_entry 决定）
    monkeypatch.setattr(os, "listdir", lambda _p: list(disk_names))
    monkeypatch.setattr(
        LibraryManager,
        "_should_skip_entry",
        lambda self, name: name.startswith(".") or name.startswith("_"),
    )

    reconcile_calls = []
    monkeypatch.setattr(
        LibraryManager,
        "_report_browse_mismatch_and_reconcile",
        lambda self, library, dir_path, *, disk_only, ghost_only: reconcile_calls.append(
            (dir_path, sorted(disk_only), sorted(ghost_only))
        ),
    )

    class _FakeStoreService:
        def list_children_page(self, *_a, **_k):
            return {
                "entries": list(entries),
                "total": len(entries),
                "next_page_cursor": None,
                "used_page_cursor": False,
            }

        def has_library_entries(self, _library_id):
            return True

    # manager 内部 `from .library_index import get_library_index_service` 解析到
    # 包级 re-export，须 patch 包命名空间的名字
    monkeypatch.setattr(
        index_pkg,
        "get_library_index_service",
        lambda: _FakeStoreService(),
    )

    library = SimpleNamespace(
        id="lib1", name="测试库", type="local",
        root_path=str(tmp_path), browse_root_path=None, synology=None,
    )
    result = manager._list_files_via_index(
        library,
        page=1,
        page_size=200,
        current_path=str(tmp_path),
        browse_root=str(tmp_path),
        parent_path="",
        sort_by="size",
        sort_order="desc",
    )
    return result, reconcile_calls


def test_probe_falls_back_on_ghost_entry(tmp_path, monkeypatch):
    """快照有旧名而磁盘没有（删除/重命名旧侧）→ 回退 + diff 命中。"""
    entries = [
        _entry("old-name", "old-name"),
        _entry("b.wav", "b.wav"),
    ]
    result, calls = _run_probe(monkeypatch, tmp_path, entries, ["b.wav"])
    assert result is None                       # 回退 FS 扫描
    assert len(calls) == 1
    _, disk_only, ghost_only = calls[0]
    assert disk_only == []
    assert ghost_only == ["old-name"]


def test_probe_falls_back_on_rename_count_conservation(tmp_path, monkeypatch):
    """重命名场景：两侧总数相同（计数守恒）但名字不同 → 旧探测打不中，diff 必须命中。"""
    entries = [
        _entry("old-name", "old-name"),
        _entry("b.wav", "b.wav"),
    ]
    # 磁盘上 old-name 已改成 new-name：总数都是 2
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ["new-name", "b.wav"]
    )
    assert result is None
    assert len(calls) == 1
    _, disk_only, ghost_only = calls[0]
    assert disk_only == ["new-name"]
    assert ghost_only == ["old-name"]


def test_probe_passes_when_names_match(tmp_path, monkeypatch):
    """磁盘与快照一致 → 走索引快速路径，不回退。"""
    entries = [
        _entry("a.wav", "a.wav", size=100),
        _entry("b.wav", "b.wav", size=200),
    ]
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ["a.wav", "b.wav"]
    )
    assert result is not None
    assert result.get("browse_via_index") is True
    assert calls == []


def test_probe_ignores_hidden_files(tmp_path, monkeypatch):
    """隐藏文件（./_前缀）双侧都不参与 diff，不触发误报。"""
    entries = [_entry("a.wav", "a.wav")]
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ["a.wav", ".hidden", "_work"]
    )
    assert result is not None
    assert calls == []


def test_probe_falls_back_on_disk_only_entry(tmp_path, monkeypatch):
    """磁盘新条目快照没有（解压新完成）→ 回退 + diff 命中。"""
    entries = [_entry("a.wav", "a.wav")]
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ["a.wav", "new-dir"]
    )
    assert result is None
    assert len(calls) == 1
    _, disk_only, ghost_only = calls[0]
    assert disk_only == ["new-dir"]
    assert ghost_only == []


# ---------------------------------------------------------------------------
# v2.6.47：force_refresh 绕过 + 读路径精确修补 + 节流全局共享
#
# 背景（FIX_PLAN §3.2.1 实测）：点刷新时旧实现「进索引 → 18 条全判 stale →
# 整子树 upsert（数万条写）→ materialized_seq 被拖后 → 探测读到该目录 0 行
# → 又回退」，形成自激循环。以下四组用例锁定修复后的语义。
# ---------------------------------------------------------------------------


def _make_bypass_manager(monkeypatch):
    """构造只启用绕过分支所需桩的 manager，记录实际调用的方法。"""
    manager = _manager()
    monkeypatch.setattr(
        LibraryManager,
        "_library_uses_inventory_index",
        lambda self, library: True,
    )
    monkeypatch.setattr(
        LibraryManager,
        "_index_has_usable_snapshot",
        lambda self, service, library_id: (_ for _ in ()).throw(
            AssertionError("force_refresh 应绕过索引，不应查询快照可用性")
        ),
    )
    return manager


def test_force_refresh_bypasses_index_for_local(tmp_path, monkeypatch):
    """F9b：本地上库 + force_refresh=True → 直接回退实时扫描，不查索引。"""
    manager = _make_bypass_manager(monkeypatch)
    library = SimpleNamespace(
        id="lib1", name="测试库", type="local",
        root_path=str(tmp_path), browse_root_path=None, synology=None,
    )
    result = manager._list_files_via_index(
        library,
        page=1,
        page_size=200,
        current_path=str(tmp_path),
        browse_root=str(tmp_path),
        parent_path="",
        sort_by="size",
        sort_order="desc",
        force_refresh=True,
    )
    assert result is None  # 交由调用方落 _list_local_files


def test_force_refresh_does_not_bypass_for_remote(tmp_path, monkeypatch):
    """F9b 边界：远程库不受绕过影响，仍走原有索引路径。"""
    from app.core import library_index as index_pkg

    manager = _manager()
    monkeypatch.setattr(
        LibraryManager,
        "_library_uses_inventory_index",
        lambda self, library: True,
    )
    monkeypatch.setattr(
        LibraryManager,
        "_index_has_usable_snapshot",
        lambda self, service, library_id: False,
    )
    monkeypatch.setattr(
        LibraryManager,
        "_index_status_name",
        lambda self, service, library_id: "building",
    )
    monkeypatch.setattr(
        LibraryManager,
        "_build_index_pending_browse_result",
        lambda self, library, **kwargs: {"pending": True},
    )
    monkeypatch.setattr(
        index_pkg, "get_library_index_service", lambda: SimpleNamespace()
    )
    library = SimpleNamespace(
        id="lib2", name="远程库", type="synology",
        root_path=str(tmp_path), browse_root_path=None, synology=None,
    )
    result = manager._list_files_via_index(
        library,
        page=1,
        page_size=200,
        current_path=str(tmp_path),
        browse_root=str(tmp_path),
        parent_path="",
        sort_by="size",
        sort_order="desc",
        force_refresh=True,
    )
    # 远程库未就绪走 pending，说明没有被 force_refresh 分支短路
    assert result == {"pending": True}


def test_read_repair_scope_exact_forces_exact_scope(tmp_path, monkeypatch):
    """F12：读路径 stale 修补必须把 scope 钉成 exact，不得升级为 subtree。"""
    manager = _manager()
    captured = {}

    def _fake_record(self, library, absolute_paths, *, kind, source, scopes_by_path=None):
        captured["kind"] = kind
        captured["source"] = source
        captured["paths"] = list(absolute_paths)
        captured["scopes"] = dict(scopes_by_path or {})
        return True

    monkeypatch.setattr(LibraryManager, "_record_index_reconcile_many", _fake_record)
    monkeypatch.setattr(
        LibraryManager,
        "_normalize_index_abs_key",
        lambda self, library, path: os.path.normcase(os.path.abspath(path)),
    )
    library = SimpleNamespace(id="lib1", name="测试库", type="local",
                              root_path=str(tmp_path), browse_root_path=None, synology=None)
    target = os.path.join(str(tmp_path), "RJ016xxxxx")
    manager._enqueue_index_read_repair_scope_exact(library, [target])

    assert captured["kind"] == "reconcile"
    assert captured["source"] == "read_repair_exact"
    assert captured["paths"] == [target]
    # 关键断言：目录路径也必须显式 exact（旧实现会因 isdir 自动升级 subtree）
    assert list(captured["scopes"].values()) == ["exact"]


def test_read_repair_scope_exact_dedupes_within_window(tmp_path, monkeypatch):
    """F12：5 秒窗口内的同一路径不重复入队（沿用既有去重语义）。"""
    manager = _manager()
    calls = []

    def _fake_record(self, library, absolute_paths, *, kind, source, scopes_by_path=None):
        calls.append(list(absolute_paths))
        return True

    monkeypatch.setattr(LibraryManager, "_record_index_reconcile_many", _fake_record)
    monkeypatch.setattr(
        LibraryManager,
        "_normalize_index_abs_key",
        lambda self, library, path: os.path.normcase(os.path.abspath(path)),
    )
    library = SimpleNamespace(id="lib1", name="测试库", type="local",
                              root_path=str(tmp_path), browse_root_path=None, synology=None)
    target = os.path.join(str(tmp_path), "RJ016xxxxx")
    manager._enqueue_index_read_repair_scope_exact(library, [target])
    manager._enqueue_index_read_repair_scope_exact(library, [target])
    assert len(calls) == 1


def test_reconcile_throttle_is_shared_across_entrypoints(tmp_path, monkeypatch):
    """F13：两个不同入口共用同一节流窗口，第二次必须被拦下。"""
    manager = _manager()
    library = SimpleNamespace(id="lib1", name="测试库", type="local")
    # 模拟「完整性探测」与「名字级 diff」两个入口先后到达同一目录
    assert manager._reconcile_throttle_allows(library, "/library/汉化") is True
    assert manager._reconcile_throttle_allows(library, "/library/汉化") is False
    # 不同目录不受影响
    assert manager._reconcile_throttle_allows(library, "/library/其他") is True


def test_reconcile_throttle_scopes_per_library(tmp_path, monkeypatch):
    """F13：节流键包含库 id，不同库的同名目录互不干扰。"""
    manager = _manager()
    lib_a = SimpleNamespace(id="libA", name="A", type="local")
    lib_b = SimpleNamespace(id="libB", name="B", type="local")
    assert manager._reconcile_throttle_allows(lib_a, "/library/汉化") is True
    assert manager._reconcile_throttle_allows(lib_b, "/library/汉化") is True
