"""浏览完整性探测（幽灵触发）回归测试。

演进史（三段，读代码前必看，否则会误判「回退了功能」）：

1. v2.6.40 真机反馈：进目录显示旧内容，需等 ~2s 轮询自纠。根因是探测用
   ``len(fs_names) > index_total`` 计数比较——重命名/移动是 -1+1 计数守恒，
   永远不触发，快照里的旧名字（幽灵）+ 缺失的新名字被直接返回给前端。
2. v2.6.41 改为**名字集合 diff**（readdir 全目录 vs 索引条目名），任何差异都
   回退实时扫描。**分页上线后该口径就错了**：entries 是 ``limit=page_size`` 的
   *本页*，fs_names 是 *全量*，于是任何子项数 > page_size 的目录 100% 误判
   「磁盘多出 page_size 条」（实测 disk_only=191=291-100），恒回退 →
   索引快路径被废掉、每页白读一次全目录、每 60s 入队一次 reconcile。
3. **v2.6.48（A 方案，本文件当前锁定的语义）**：回退信号只取**幽灵**
   （索引里有、磁盘上已不存在），来自 ``_validate_local_index_entries_for_read``
   回填的 ``diagnostics["missing_paths"]``。浏览热路径**不再做任何目录遍历**。
   代价（显式接受）：磁盘单侧多出的条目不再触发回退 —— 软件内新增走 mutation
   即时入索引，外部直拷由 watcher 轻量巡检兜底（见 watcher_driver._scrub_once）。

打桩原则：不连库。FakeEntry 直接构造；``os.listdir`` / ``os.scandir`` 一律换成
「调用即失败」的哨兵，把「零磁盘遍历」也变成可断言的契约。
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


def _run_probe(monkeypatch, tmp_path, entries, *, ghost_names=(), index_total=None):
    """驱动 _list_files_via_index 的幽灵探测，返回 (indexed_result, reconcile_calls)。

    ``ghost_names``：本次要让校验函数回填成幽灵的条目名（模拟「索引有、磁盘无」）。
    ``os.listdir`` / ``os.scandir`` 被替换为哨兵——A 方案下探测命中与否都不得遍历目录。
    """
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

    ghost_set = set(ghost_names)

    def _fake_validate(self, library, ents, *, return_stale_paths=False, diagnostics=None):
        # 契约：读路径必须传 diagnostics 才能拿到幽灵（新增可选出参，不改返回值元数）
        assert diagnostics is not None, "A 方案要求调用方传入 diagnostics 出参"
        assert library.type == "local"
        diagnostics["missing_paths"] = [f"D:/data/lib/{name}" for name in ghost_names]
        diagnostics["stale_paths"] = []
        diagnostics["valid_count"] = len(ents)
        kept = [e for e in ents if e.name not in ghost_set]
        return (kept, set()) if return_stale_paths else kept

    monkeypatch.setattr(
        LibraryManager,
        "_validate_local_index_entries_for_read",
        _fake_validate,
    )

    def _no_disk_traversal(*_args, **_kwargs):
        raise AssertionError("A 方案下浏览热路径不得遍历磁盘目录（listdir/scandir）")

    monkeypatch.setattr(os, "listdir", _no_disk_traversal)
    monkeypatch.setattr(os, "scandir", _no_disk_traversal)
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
                "total": len(entries) if index_total is None else index_total,
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
    """索引有、磁盘已不存在（删除）→ 回退 FS 扫描 + reconcile 入队。"""
    entries = [
        _entry("old-name", "old-name"),
        _entry("b.wav", "b.wav"),
    ]
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ghost_names=["old-name"]
    )
    assert result is None                       # 回退 FS 扫描
    assert len(calls) == 1
    _, disk_only, ghost_only = calls[0]
    assert disk_only == []                      # A 方案不再计算磁盘侧差集
    assert ghost_only == ["old-name"]


def test_probe_falls_back_on_rename_old_side_ghost(tmp_path, monkeypatch):
    """重命名：两侧总数相同（计数守恒）但旧名成幽灵 → 必须命中。

    这是 v2.6.41 的原始动机，A 方案不减配：重命名后索引里的旧名字在磁盘上
    必然不存在 → 幽灵 → 回退，本次响应即正确。
    """
    entries = [
        _entry("old-name", "old-name"),
        _entry("b.wav", "b.wav"),
    ]
    # 磁盘上 old-name 已改成 new-name（总数都是 2）——A 方案不看磁盘侧，只看幽灵
    result, calls = _run_probe(
        monkeypatch, tmp_path, entries, ghost_names=["old-name"]
    )
    assert result is None
    assert len(calls) == 1
    _, disk_only, ghost_only = calls[0]
    assert disk_only == []
    assert ghost_only == ["old-name"]


def test_probe_does_not_fall_back_on_disk_only_entry(tmp_path, monkeypatch):
    """★A 方案的行为变更：磁盘单侧多出条目（外部直拷/解压新完成）**不再**回退。

    收益：任何子项数 > page_size 的目录不再被恒误判（旧名字级 diff 的致命缺陷）。
    代价（显式接受）：软件内新增走 mutation 即时入索引；外部直拷由 watcher
    轻量巡检兜底（watcher_driver._roots 已与 observer 挂载解耦，300s 一轮）。
    """
    entries = [_entry("a.wav", "a.wav")]
    result, calls = _run_probe(monkeypatch, tmp_path, entries, ghost_names=[])
    assert result is not None
    assert result.get("browse_via_index") is True
    assert calls == []


def test_index_fast_path_never_touches_disk(tmp_path, monkeypatch):
    """零幽灵时不得发生任何 listdir/scandir（哨兵已设为「调用即失败」）。"""
    entries = [
        _entry("a.wav", "a.wav", size=100),
        _entry("b.wav", "b.wav", size=200),
    ]
    result, calls = _run_probe(monkeypatch, tmp_path, entries, ghost_names=[], index_total=291)
    assert result is not None
    assert result.get("browse_via_index") is True
    assert calls == []


def test_probe_ignores_missing_absolute_path(tmp_path, monkeypatch):
    """诊断出参缺字段（如整批条目无 absolute_path）时按「零幽灵」处理，不得炸。"""
    entries = [_entry("a.wav", "a.wav")]

    def _fake_validate(self, library, ents, *, return_stale_paths=False, diagnostics=None):
        # 故意不填 missing_paths，模拟实现里 early-return 分支
        diagnostics["stale_paths"] = []
        return ([e for e in ents], set()) if return_stale_paths else [e for e in ents]

    from app.core import library_index as index_pkg

    monkeypatch.setattr(
        LibraryManager, "_validate_local_index_entries_for_read", _fake_validate
    )
    monkeypatch.setattr(LibraryManager, "_library_uses_inventory_index", lambda self, l: True)
    monkeypatch.setattr(LibraryManager, "_index_has_usable_snapshot", lambda self, s, i: True)
    monkeypatch.setattr(LibraryManager, "_should_skip_entry", lambda self, n: n.startswith("."))

    class _Svc:
        def list_children_page(self, *_a, **_k):
            return {"entries": list(entries), "total": 1, "next_page_cursor": None}

        def has_library_entries(self, _lid):
            return True

    monkeypatch.setattr(index_pkg, "get_library_index_service", lambda: _Svc())
    library = SimpleNamespace(
        id="lib1", name="测试库", type="local",
        root_path=str(tmp_path), browse_root_path=None, synology=None,
    )
    result = _manager()._list_files_via_index(
        library, page=1, page_size=200, current_path=str(tmp_path),
        browse_root=str(tmp_path), parent_path="", sort_by="size", sort_order="desc",
    )
    assert result is not None and result.get("browse_via_index") is True


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


# ---------------------------------------------------------------------------
# v2.6.48：listing_service 同源修复（mode=verify 的 page-vs-full 口径）
#
# _verify_current_layer 旧实现用「valid_entries 的名字集合」（= 本页，因为
# build_library_listing 传 limit=page_size）与 os.scandir 的**全量**直接做差集，
# 于是任何子项数 > page_size 的目录都会把「本页之后的条目」整批算成 disk_only：
# 响应里塞重复条目（且超过 page_size），并每次 verify 都误触发一次子树 reconcile。
# 修复：只有「本页覆盖整目录」（无游标 + 无下一页 + 声明总数 ≤ 本页条数）才做差集。
# ---------------------------------------------------------------------------


def _real_entry(tmp_path, rel, payload=b"x"):
    path = Path(tmp_path) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return SimpleNamespace(
        library_id="lib1",
        entry_type="file",
        relative_path=rel,
        name=os.path.basename(rel),
        rjcode=None,
        parent_path="",
        size=len(payload),
        file_count=0,
        mtime=None,
        depth=None,
        absolute_path=str(path),
        indexed_at=0,
        generation=1,
        materialized_seq=0,
    )


def _build_verify_listing(monkeypatch, tmp_path, entries, *, total, cursor=""):
    from app.core.library_index import listing_service as ls

    enqueued = []
    monkeypatch.setattr(
        ls,
        "_enqueue_subtree_reconcile",
        lambda library, rel: enqueued.append(rel) or True,
    )
    service = SimpleNamespace(
        list_children_page=lambda *_a, **_k: {
            "entries": list(entries),
            "total": total,
            "next_page_cursor": cursor or None,
            "used_page_cursor": bool(cursor),
        },
        get_status=lambda _lid: SimpleNamespace(active_generation=1, updated_at=None),
    )
    library = SimpleNamespace(
        id="lib1", name="测试库", type="local", root_path=str(tmp_path)
    )
    response = ls.build_library_listing(
        library=library,
        relative_path="",
        mode="verify",
        get_service=lambda: service,
    )
    return response, enqueued


def test_verify_skips_disk_diff_when_page_is_incomplete(tmp_path, monkeypatch):
    """本页只是切片（有下一页）→ 不得 scandir 做差集，也不得入队 reconcile。"""
    entries = [_real_entry(tmp_path, "a.wav"), _real_entry(tmp_path, "b.wav")]
    # 磁盘上确实还有更多条目，但本页只返回 2 条 —— 旧实现会把它们全算成 disk_only
    for index in range(3):
        _real_entry(tmp_path, f"extra-{index}.wav")

    def _no_scandir(*_args, **_kwargs):
        raise AssertionError("本页未覆盖整目录时不得 scandir 做磁盘差集")

    monkeypatch.setattr(os, "scandir", _no_scandir)
    response, enqueued = _build_verify_listing(
        monkeypatch, tmp_path, entries, total=600, cursor="cursor-2"
    )

    assert response["source"] == "verify"
    assert len(response["items"]) == 2      # 不再被「本页之后的条目」灌满
    assert enqueued == []


def test_verify_runs_disk_diff_when_page_covers_directory(tmp_path, monkeypatch):
    """小目录（本页即全量）保留既有 verify 契约：磁盘多出的条目附进响应并触发 reconcile。"""
    entries = [_real_entry(tmp_path, "a.wav"), _real_entry(tmp_path, "b.wav")]
    _real_entry(tmp_path, "extra.wav")

    response, enqueued = _build_verify_listing(
        monkeypatch, tmp_path, entries, total=2
    )

    names = {item.get("name") for item in response["items"]}
    assert {"a.wav", "b.wav", "extra.wav"} <= names
    assert enqueued == [""]     # 根目录的相对路径是空串


def test_verify_treats_ghost_row_as_not_in_response(tmp_path, monkeypatch):
    """幽灵条目（快照有、磁盘无）不进响应；且单独构成 reconcile 触发条件。"""
    entries = [
        _real_entry(tmp_path, "a.wav"),
        SimpleNamespace(  # 磁盘上不存在 → 幽灵
            library_id="lib1", entry_type="file", relative_path="gone.wav",
            name="gone.wav", rjcode=None, parent_path="", size=1, file_count=0,
            mtime=None, depth=None, absolute_path=str(Path(tmp_path) / "gone.wav"),
            indexed_at=0, generation=1, materialized_seq=0,
        ),
    ]
    response, enqueued = _build_verify_listing(
        monkeypatch, tmp_path, entries, total=2
    )

    names = {item.get("name") for item in response["items"]}
    assert "gone.wav" not in names
    assert "a.wav" in names
    assert enqueued == [""]
