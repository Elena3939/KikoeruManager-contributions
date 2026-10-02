# -*- coding: utf-8 -*-
"""AI 标题汉化服务的输出健壮性回归测试。

历史事故：max_tokens 硬编码 200，文件级重命名返回「标题 + 全部文件名」的 JSON
映射时输出被截断成半个 JSON；旧清理逻辑把 JSON 残骸当翻译成功结果传下去，
项目文件夹被重命名成 `RJxxxx {` 之类的垃圾名。这里固定回归：
- finish_reason=length（截断）必须判失败并进入重试；
- 有 `{` 无 `}` 的不完整 JSON 必须判失败；
- 完整 JSON 映射、纯文本翻译仍按原逻辑接受。
"""
from __future__ import annotations

import sys
import types

import pytest

from app.core.ai_title_translation_service import AITitleTranslationService


def _make_response(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _install_fake_litellm(monkeypatch: pytest.MonkeyPatch, responses: list) -> None:
    """把假 litellm 塞进 sys.modules，acompletion 依次弹出 responses。"""
    queue = list(responses)

    async def acompletion(**kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


def _config(**overrides) -> dict:
    base = {
        "enabled": True,
        "model": "openai/gpt-4o-mini",
        "api_key": "sk-test",
        "max_retries": 0,
        "temperature": 0.1,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_truncated_json_finish_reason_length_is_failure(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response('{"作品": "标题", "01文件": "01翻', finish_reason="length")])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is False
    assert result["translated_title"] is None
    assert "截断" in str(result.get("error", {}).get("suggestion", "")) or result.get("status") == "failed"


@pytest.mark.asyncio
async def test_incomplete_json_without_closing_brace_is_failure(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response('{\n  "作品": "标题",\n  "01文件": "01翻译', finish_reason="stop")])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is False
    assert result["translated_title"] is None


@pytest.mark.asyncio
async def test_truncation_retries_then_succeeds(monkeypatch):
    _install_fake_litellm(monkeypatch, [
        _make_response('{"作品": "标题", "01文件": "01翻', finish_reason="length"),
        _make_response('{"作品": "标题翻译", "01文件": "01翻译"}', finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config(max_retries=1))
    assert result["success"] is True
    assert "标题翻译" in result["translated_title"]
    assert "01翻译" in result["translated_title"]


@pytest.mark.asyncio
async def test_complete_json_mapping_returns_redumped_json(monkeypatch):
    payload = '{"作品": "标题翻译", "01文件": "01翻译"}'
    _install_fake_litellm(monkeypatch, [_make_response(payload)])
    service = AITitleTranslationService()
    result = await service.translate_single("作品", _config())
    assert result["success"] is True
    assert "标题翻译" in result["translated_title"]
    assert "01翻译" in result["translated_title"]


@pytest.mark.asyncio
async def test_plain_text_translation_still_accepted(monkeypatch):
    _install_fake_litellm(monkeypatch, [_make_response("这是翻译后的标题")])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True
    assert result["translated_title"] == "这是翻译后的标题"


def test_completion_kwargs_uses_configured_max_tokens():
    service = AITitleTranslationService()
    assert service._completion_kwargs(_config(max_tokens=8192), [])["max_tokens"] == 8192
    assert service._completion_kwargs(_config(), [])["max_tokens"] == 4096


# ---------------------------------------------------------------------------
# 推理模型「正文为空」回归（v2.6.45）
#
# 用户实测：服务商控制台显示输出 12,304 token，本地日志却是 error=模型返回为空。
# 根因：推理模型把输出全花在 reasoning_content，标准 content 为空，而流式解析
# 只读 content 字段，且没有任何诊断日志可看出「模型其实返回了什么」。
# 这里固定三件事：
#   1. 流式路径要能从非 content 字段回退取到正文（不报空响应）；
#   2. 通用收集不硬编码字段名（未知自定义字段也要收到）；
#   3. usage 按字段 merge，不能被后续分片整体覆盖丢证据。
# ---------------------------------------------------------------------------

class _FakeStream:
    """带 __aiter__ 的假流式响应，让 _call_model 走流式分支。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _delta_chunk(delta: dict, *, finish_reason: str = "", usage: dict | None = None) -> dict:
    chunk: dict = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _install_fake_litellm_stream(monkeypatch: pytest.MonkeyPatch, chunks: list) -> None:
    async def acompletion(**kwargs):
        return _FakeStream(chunks)

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


def test_extract_verbose_picks_reasoning_when_content_empty():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "", "reasoning_content": "让我分析这个标题…"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert primary == "让我分析这个标题…"
    assert collected["reasoning_content"] == "让我分析这个标题…"
    assert "content" not in collected  # 空内容不入收集


def test_extract_verbose_collects_unknown_custom_field():
    """通用性：未预置的字段名也要能收到，不为具体模型写特判。"""
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "", "answer_text": "自定义字段正文"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert collected.get("answer_text") == "自定义字段正文"
    assert primary == "自定义字段正文"


def test_extract_verbose_prefers_content_over_reasoning():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"content": "正文", "reasoning_content": "思考"})
    primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert primary == "正文"
    assert collected["reasoning_content"] == "思考"


def test_exclude_structural_fields_from_text_collection():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({"role": "assistant", "content": "x", "tool_calls": [{"id": "1"}]})
    _primary, collected, _usage, _reason = extract_litellm_stream_delta_verbose(chunk)
    assert "role" not in collected
    assert "tool_calls" not in collected
    assert collected["content"] == "x"


def test_verbose_returns_usage_and_finish_reason():
    from app.core.ai_subtitle_match_service import extract_litellm_stream_delta_verbose

    chunk = _delta_chunk({}, finish_reason="stop", usage={"completion_tokens": 12304})
    _primary, _collected, usage, reason = extract_litellm_stream_delta_verbose(chunk)
    assert usage["completion_tokens"] == 12304
    assert reason == "stop"


@pytest.mark.asyncio
async def test_stream_reasoning_only_content_is_used_not_failed(monkeypatch):
    """核心回归：推理模型只在 reasoning_content 输出时，必须取到正文而非判失败。"""
    _install_fake_litellm_stream(monkeypatch, [
        _delta_chunk({"content": "", "reasoning_content": "（思考）先分析日文标题"},
                    usage={"prompt_tokens": 10}),
        _delta_chunk({"content": "", "reasoning_content": "圣职者姐妹"}, usage={"completion_tokens": 12304}),
        _delta_chunk({}, finish_reason="stop"),
    ])
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    assert "圣职者姐妹" in result["translated_title"]


@pytest.mark.asyncio
async def test_stream_usage_merged_across_chunks(monkeypatch):
    """usage 必须按字段 merge：后面的分片不能把前面的 token 计数覆盖掉。"""
    captured: dict = {}

    async def acompletion(**kwargs):
        return _FakeStream([
            _delta_chunk({"content": "a"}, usage={"prompt_tokens": 10}),
            _delta_chunk({"content": "b"}, usage={"completion_tokens": 12304}),
            _delta_chunk({}, finish_reason="stop"),
        ])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    service = AITitleTranslationService()

    original = service._call_model

    async def spy(config, work_name):
        content, usage = await original(config, work_name)
        captured["usage"] = usage
        return content, usage

    monkeypatch.setattr(service, "_call_model", spy)
    result = await service.translate_single("タイトル", _config())
    assert result["success"] is True, result
    # 两个字段分别来自不同分片，merge 后都要在。
    assert captured["usage"]["prompt_tokens"] == 10
    assert captured["usage"]["completion_tokens"] == 12304


def test_non_retryable_error_does_not_include_truncation():
    """⚠️ 截断必须保持可重试（既有 test_truncation_retries_then_succeeds 依赖）。"""
    from app.core.ai_title_translation_service import _is_non_retryable_error

    assert _is_non_retryable_error(ValueError(
        "模型输出达到 max_tokens 上限（200）被截断，请在 AI 标题汉化设置中调大输出 token 上限"
    )) is False
    assert _is_non_retryable_error(ValueError("模型返回了不完整的 JSON（缺少结束括号），输出可能被截断")) is True
    assert _is_non_retryable_error(ValueError("Authentication Fails, Your api key is invalid")) is True
    assert _is_non_retryable_error(TimeoutError("read timeout")) is False
    assert _is_non_retryable_error(ConnectionError("connection reset by peer")) is False


@pytest.mark.asyncio
async def test_auth_error_does_not_retry(monkeypatch):
    """认证失败属确定性错误：不应重试（实测一次失败空转 16 秒）。"""
    calls = {"n": 0}

    async def acompletion(**kwargs):
        calls["n"] += 1
        raise RuntimeError("Authentication Fails, Your api key: ****jRhL is invalid")

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    service = AITitleTranslationService()
    result = await service.translate_single("タイトル", _config(max_retries=3))
    assert result["success"] is False
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# /api/ai-title-translation/test 接口回归
#
# v2.5.26 用户实测：设置页"AI 标题汉化 → 测试连接"点击无反应。双重根因：
#   1. 前端 <stateful-button :click="..."> 传了不存在的 prop，组件只认 @click；
#   2. 后端 /api/ai-title-translation/test 接口根本不存在（404）。
# 这里固定后端接口行为：普通模式用表单草稿参数 + 掩码 Key 回退；
# use_ai_subtitle_api=true 时复用字幕配对连接配置。
# ---------------------------------------------------------------------------

def _install_fake_litellm_capture(monkeypatch: pytest.MonkeyPatch, capture: dict):
    async def acompletion(**kwargs):
        capture["kwargs"] = kwargs
        return _make_response('{"テスト": "测试"}')

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))


def test_test_endpoint_exists_and_translates(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"enabled": True, "model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["translated_title"] == "测试"
    assert capture["kwargs"]["model"] == "openai/gpt-4o-mini"


def test_test_endpoint_reuses_subtitle_api_config(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    saved_model = client.app.state  # noqa: F841  仅确保 app 可访问
    from app.config.settings import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "ai_subtitle_matching", type(
        "SubCfg",
        (),
        {
            "model": "openai/subtitle-model",
            "api_key": "sk-sub",
            "api_base": "https://sub.example.com/v1",
            "api_version": "",
            "organization": "",
            "proxy_url": "",
            "timeout_seconds": 45,
            "max_retries": 1,
            "temperature": 0.2,
        },
    )(), raising=False)

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"use_ai_subtitle_api": True}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert capture["kwargs"]["model"] == "openai/subtitle-model"
    assert capture["kwargs"]["api_key"] == "sk-sub"
    assert capture["kwargs"]["api_base"] == "https://sub.example.com/v1"


def test_test_endpoint_masked_api_key_falls_back_to_disk(client, monkeypatch):
    capture: dict = {}
    _install_fake_litellm_capture(monkeypatch, capture)

    import app.api.routes as routes_mod
    monkeypatch.setattr(
        routes_mod,
        "_read_ai_title_translation_api_key_from_disk",
        lambda: "sk-saved-on-disk",
    )

    resp = client.post(
        "/api/ai-title-translation/test",
        json={"config": {"enabled": True, "model": "openai/gpt-4o-mini", "api_key": "********"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert capture["kwargs"]["api_key"] == "sk-saved-on-disk"
