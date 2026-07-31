"""Thin wrapper over the Telegram Bot HTTP API.

Built on `requests` (already a transitive dependency of the SDK) rather than a
third-party Telegram library - the Bot API is a handful of plain HTTP calls,
and one fewer dependency is one fewer thing to trust with a bot that can move
money.

Long polling, not a webhook: `get_updates` uses Telegram's own long-poll
(the call blocks server-side until an update exists or `timeout` elapses), so
delivery is near-instant without needing a public HTTPS endpoint. A webhook
would shave the poll round-trip but requires a reachable TLS server - not
worth it for a single-owner bot running from a home PC or a small VPS.
"""

from __future__ import annotations

from typing import Any

import requests

_BASE = "https://api.telegram.org/bot{token}/{method}"

# Telegram's own message cap. Anything longer is chunked by the caller.
MESSAGE_LIMIT = 4096

# Long-poll timeout. Our HTTP client timeout must exceed this or we cancel our
# own long poll before Telegram has a chance to answer it.
POLL_TIMEOUT_SECONDS = 25
_HTTP_TIMEOUT_SECONDS = POLL_TIMEOUT_SECONDS + 10


class TelegramError(RuntimeError):
    """The Telegram API rejected a call. `description` is Telegram's own text."""

    def __init__(self, method: str, description: str, *, status_code: int | None = None):
        self.method = method
        self.description = description
        self.status_code = status_code
        super().__init__(f"{method} failed ({status_code}): {description}")


class TelegramAPI:
    """One bot token, one session. Safe to hold open for the process lifetime."""

    def __init__(self, token: str, *, session: requests.Session | None = None):
        if not token:
            raise ValueError("A Telegram bot token is required.")
        self._token = token
        self._session = session or requests.Session()

    def _redact(self, text: str) -> str:
        """Replace the bot token wherever it appears in a string.

        The token is part of the request URL, and `requests` puts the URL into
        the message of every transport-level exception it raises. Those
        messages are logged, and on the menu paths they are shown in the chat -
        so an ordinary network blip would otherwise write a working bot token
        into a log file. Anyone who reads it can place orders.
        """
        if not self._token:
            return text
        return text.replace(self._token, "<BOT_TOKEN_REDACTED>")

    def _call(self, method: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        url = _BASE.format(token=self._token, method=method)
        try:
            response = self._session.post(
                url, json=params or {}, timeout=timeout or _HTTP_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # Never let a transport error escape carrying the URL: see _redact.
            raise TelegramError(method, self._redact(f"{type(exc).__name__}: {exc}")) from None
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(
                method,
                self._redact(f"non-JSON response: {response.text[:200]}"),
                status_code=response.status_code,
            ) from exc
        if not payload.get("ok"):
            raise TelegramError(
                method,
                self._redact(str(payload.get("description") or "unknown error")),
                status_code=response.status_code,
            )
        return payload.get("result")

    # ---- updates -------------------------------------------------------
    def get_updates(self, *, offset: int | None = None, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict]:
        """Long-poll for new updates. Blocks up to `timeout` seconds server-side."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params=params, timeout=timeout + 10)

    # ---- outbound --------------------------------------------------------
    def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: dict | None = None
    ) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._call("sendMessage", params=params)

    def edit_message_text(
        self, chat_id: int | str, message_id: int, text: str, *, reply_markup: dict | None = None
    ) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        # None means "remove the keyboard" - Telegram wants an empty markup for
        # that, not an absent field, once a message already has one.
        params["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        return self._call("editMessageText", params=params)

    def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
    ) -> dict:
        # Telegram requires this within ~a few seconds of the tap, even with an
        # empty body, or the button spins with no feedback.
        params: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text is not None:
            params["text"] = text[:200]
        return self._call("answerCallbackQuery", params=params)

    def delete_message(self, chat_id: int | str, message_id: int) -> dict:
        return self._call("deleteMessage", params={"chat_id": chat_id, "message_id": message_id})

    def get_me(self) -> dict:
        return self._call("getMe")


def confirm_keyboard(token: str) -> dict:
    """Inline Confirm/Cancel row. `token` must already be short (see bot.py)."""
    return {
        "inline_keyboard": [
            [
                {"text": "Confirm", "callback_data": f"confirm:{token}"},
                {"text": "Cancel", "callback_data": f"cancel:{token}"},
            ]
        ]
    }


def chunk_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries where possible; hard-cut only if one line alone
    exceeds the limit (a market question is never going to do that, but a raw
    JSON dump might)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        added = len(line) + 1
        if current_len + added > limit and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        if len(line) > limit:
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit])
            continue
        current.append(line)
        current_len += added
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]
