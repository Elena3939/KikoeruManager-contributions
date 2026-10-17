"""下载收件箱单测：去重、provider 过滤、ack 幂等、TTL、鉴权分支。"""

from __future__ import annotations

import time

import pytest

from app.core.download_inbox_service import (
    DownloadInboxService,
    is_private_address,
    line_identity,
    normalize_line,
)


@pytest.fixture()
def inbox(tmp_path):
    return DownloadInboxService(store_path=tmp_path / "download_inbox.json")


STANDARD = "RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz | 解压码: abc123"
STANDARD_2 = "RJ02222222 | https://pan.baidu.com/s/1BBBBBB?pwd=bbbb | 解压码: passBBB"


def test_normalize_line_unifies_tags_and_spaces():
    raw = "RJ01234567   |   https://pan.baidu.com/s/1AbCdEf ｜ 解压密码：abc123"
    assert normalize_line(raw) == "RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 解压码: abc123"


def test_line_identity_ignores_pass_code_query():
    left = line_identity("RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz | 解压码: a")
    right = line_identity("RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 解压码: b")
    assert left == right


def test_push_dedupes_by_share_identity(inbox):
    first = inbox.push("baidu", STANDARD)
    assert first == {"accepted": 1, "duplicated": 0, "total": 1}

    second = inbox.push("baidu", STANDARD)
    assert second == {"accepted": 0, "duplicated": 1, "total": 1}

    pending = inbox.pending("baidu")
    assert len(pending["items"]) == 1
    assert pending["items"][0]["line"] == STANDARD


def test_push_updates_line_when_password_added(inbox):
    no_password = "RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz"
    inbox.push("baidu", no_password)
    inbox.push("baidu", STANDARD)
    items = inbox.pending("baidu")["items"]
    assert len(items) == 1
    assert items[0]["line"] == STANDARD


def test_pending_filters_by_provider(inbox):
    inbox.push("baidu", STANDARD)
    inbox.push("http", "https://example.com/a.zip")
    assert len(inbox.pending("baidu")["items"]) == 1
    assert len(inbox.pending("http")["items"]) == 1
    assert len(inbox.pending()["items"]) == 2


def test_ack_removes_by_identity_and_is_idempotent(inbox):
    inbox.push("baidu", STANDARD)
    inbox.push("baidu", STANDARD_2)

    result = inbox.ack(STANDARD, "baidu")
    assert result["removed"] == 1
    assert result["total"] == 1

    again = inbox.ack(STANDARD, "baidu")
    assert again["removed"] == 0

    remaining = inbox.pending("baidu")["items"]
    assert [item["line"] for item in remaining] == [STANDARD_2]


def test_ack_tolerates_pwd_query_changes(inbox):
    inbox.push("baidu", STANDARD)
    # 输入框里这一行可能被用户改过 pwd，ack 仍应命中（按分享身份匹配）
    assert inbox.ack("RJ01234567 | https://pan.baidu.com/s/1AbCdEf", "baidu")["removed"] == 1


def test_clear_all_and_by_provider(inbox):
    inbox.push("baidu", STANDARD)
    inbox.push("http", "https://example.com/a.zip")
    assert inbox.clear("baidu")["removed"] == 1
    assert inbox.pending()["total"] == 1
    assert inbox.clear()["removed"] == 1
    assert inbox.pending()["total"] == 0


def test_persist_and_reload(tmp_path):
    path = tmp_path / "download_inbox.json"
    first = DownloadInboxService(store_path=path)
    first.push("baidu", STANDARD)
    assert path.is_file()

    second = DownloadInboxService(store_path=path)
    items = second.pending("baidu")["items"]
    assert len(items) == 1
    assert items[0]["line"] == STANDARD


def test_expired_items_are_pruned(tmp_path, monkeypatch):
    path = tmp_path / "download_inbox.json"
    service = DownloadInboxService(store_path=path)
    service.push("baidu", STANDARD)

    monkeypatch.setattr(
        DownloadInboxService,
        "_ttl_and_limit",
        lambda self: (1, 500),
    )
    with service._lock:
        service._ensure_loaded()
        service._items[0]["last_seen_at"] = int(time.time()) - 7200
    assert service.pending("baidu")["total"] == 0


def test_max_items_keeps_newest(tmp_path, monkeypatch):
    service = DownloadInboxService(store_path=tmp_path / "download_inbox.json")
    monkeypatch.setattr(DownloadInboxService, "_ttl_and_limit", lambda self: (72, 2))
    for index in range(4):
        service.push("baidu", f"RJ0123456{index} | https://pan.baidu.com/s/1AbCdE{index}")
    items = service.pending("baidu")["items"]
    assert len(items) == 2
    assert items[-1]["line"].startswith("RJ01234563")


def test_push_rejects_empty_and_oversized(inbox):
    with pytest.raises(ValueError):
        inbox.push("baidu", "   \n  \n")
    with pytest.raises(ValueError):
        inbox.push("baidu", "x" * (300 * 1024))


def test_push_rejects_too_many_lines(inbox):
    lines = "\n".join(
        f"RJ0123456{index % 10} | https://pan.baidu.com/s/1AbCdE{index}" for index in range(201)
    )
    with pytest.raises(ValueError):
        inbox.push("baidu", lines)


def test_authorize_requires_enabled(inbox, monkeypatch):
    monkeypatch.setattr(
        DownloadInboxService,
        "_inbox_config",
        staticmethod(lambda: {"enabled": False, "token": "", "lan_only": True}),
    )
    ok, status, _ = inbox.authorize_push("10.0.0.2", "")
    assert (ok, status) == (False, 403)


def test_authorize_lan_only_blocks_public_source(inbox, monkeypatch):
    monkeypatch.setattr(
        DownloadInboxService,
        "_inbox_config",
        staticmethod(lambda: {"enabled": True, "token": "", "lan_only": True}),
    )
    ok, status, _ = inbox.authorize_push("8.8.8.8", "")
    assert (ok, status) == (False, 403)
    ok, status, _ = inbox.authorize_push("192.168.1.10", "")
    assert (ok, status) == (True, 200)


def test_authorize_token_overrides_lan_only(inbox, monkeypatch):
    monkeypatch.setattr(
        DownloadInboxService,
        "_inbox_config",
        staticmethod(lambda: {"enabled": True, "token": "secret-token", "lan_only": True}),
    )
    ok, status, _ = inbox.authorize_push("8.8.8.8", "")
    assert (ok, status) == (False, 401)
    ok, status, _ = inbox.authorize_push("10.0.0.2", "wrong")
    assert (ok, status) == (False, 401)
    ok, status, _ = inbox.authorize_push("8.8.8.8", "secret-token")
    assert (ok, status) == (True, 200)


def test_is_private_address():
    assert is_private_address("10.0.0.5")
    assert is_private_address("172.16.3.4")
    assert is_private_address("192.168.0.9")
    assert is_private_address("127.0.0.1")
    assert is_private_address("::1")
    assert is_private_address("::ffff:10.0.0.5")
    assert not is_private_address("8.8.8.8")
    assert not is_private_address("")
