"""Offline tests for the Telegram HTTP wrapper. No real network calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from polymarket_bot.telegram.api import (
    MESSAGE_LIMIT,
    TelegramAPI,
    TelegramError,
    chunk_message,
    confirm_keyboard,
)


def _fake_response(payload, status_code=200):
    return SimpleNamespace(json=lambda: payload, status_code=status_code, text=str(payload))


def test_successful_call_returns_result():
    api = TelegramAPI("test-token")
    with mock.patch.object(
        api._session, "post", return_value=_fake_response({"ok": True, "result": {"id": 1}})
    ):
        assert api.get_me() == {"id": 1}


def test_telegram_error_on_ok_false():
    api = TelegramAPI("test-token")
    with mock.patch.object(
        api._session, "post",
        return_value=_fake_response({"ok": False, "description": "Unauthorized"}, status_code=401),
    ):
        with pytest.raises(TelegramError) as exc_info:
            api.get_me()
    assert "Unauthorized" in str(exc_info.value)


def test_non_json_response_raises_telegram_error():
    api = TelegramAPI("test-token")
    bad = SimpleNamespace(json=mock.Mock(side_effect=ValueError("no json")), status_code=502, text="<html>bad gw</html>")
    with mock.patch.object(api._session, "post", return_value=bad):
        with pytest.raises(TelegramError):
            api.get_me()


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        TelegramAPI("")


def test_get_updates_passes_offset_and_timeout():
    api = TelegramAPI("test-token")
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        captured["timeout"] = timeout
        return _fake_response({"ok": True, "result": []})

    with mock.patch.object(api._session, "post", side_effect=fake_post):
        api.get_updates(offset=42, timeout=10)
    assert captured["json"] == {"timeout": 10, "offset": 42}
    assert captured["timeout"] > 10  # our HTTP timeout must exceed the long-poll timeout


def test_edit_message_clears_keyboard_when_none_passed():
    api = TelegramAPI("test-token")
    captured = {}

    def fake_post(url, json, timeout):
        captured["json"] = json
        return _fake_response({"ok": True, "result": {}})

    with mock.patch.object(api._session, "post", side_effect=fake_post):
        api.edit_message_text(1, 2, "hi")
    assert captured["json"]["reply_markup"] == {"inline_keyboard": []}


def test_confirm_keyboard_shape():
    kb = confirm_keyboard("abc123")
    row = kb["inline_keyboard"][0]
    assert row[0]["callback_data"] == "confirm:abc123"
    assert row[1]["callback_data"] == "cancel:abc123"


def test_chunk_message_short_text_unchanged():
    assert chunk_message("hello") == ["hello"]


def test_chunk_message_splits_on_line_boundaries():
    line = "x" * 100
    text = "\n".join([line] * 50)  # ~5050 chars, over the (patched) limit
    chunks = chunk_message(text, limit=1000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1000
    # Reassembling (accounting for the join newlines) recovers the content.
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_message_hard_cuts_a_single_overlong_line():
    huge_line = "y" * 5000
    chunks = chunk_message(huge_line, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == huge_line


def test_message_limit_is_telegrams_actual_cap():
    assert MESSAGE_LIMIT == 4096
