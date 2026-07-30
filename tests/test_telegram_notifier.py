"""Offline tests for the Telegram notifier sink. No real network calls.

The notifier is what carries a monitor alert to a phone. Two properties
matter more than anything else here and are pinned below:

  1. It never raises. A Telegram outage during a sweep must not propagate
     into the monitor's sell path.
  2. It does not forward routine noise. A sweep runs every interval; only
     state changes are worth a push notification.
"""

from __future__ import annotations

from polymarket_bot.telegram.api import MESSAGE_LIMIT
from polymarket_bot.telegram.notifier import TelegramNotifier


class _FakeAPI:
    """Records send_message calls; optionally blows up like a real outage."""

    def __init__(self, *, explode: BaseException | None = None) -> None:
        self.sent: list[tuple[object, str]] = []
        self._explode = explode

    def send_message(self, chat_id, text, *, reply_markup=None):
        if self._explode is not None:
            raise self._explode
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}


def test_forwards_trade_alert_and_error_by_default():
    api = _FakeAPI()
    notifier = TelegramNotifier(api, 12345)

    notifier.send("filled a buy", level="trade")
    notifier.send("stop-loss triggered", level="alert")
    notifier.send("api died", level="error")

    assert [text for _chat, text in api.sent] == [
        "filled a buy",
        "stop-loss triggered",
        "api died",
    ]
    assert all(chat == 12345 for chat, _text in api.sent)


def test_drops_debug_and_info_by_default():
    api = _FakeAPI()
    notifier = TelegramNotifier(api, 12345)

    notifier.send("checking market 1 of 40", level="debug")
    notifier.send("sweep complete, nothing triggered", level="info")

    assert api.sent == []


def test_min_level_can_be_lowered_to_forward_everything():
    api = _FakeAPI()
    notifier = TelegramNotifier(api, 12345, min_level="debug")

    notifier.send("noisy", level="debug")

    assert len(api.sent) == 1


def test_default_level_argument_is_info_and_is_therefore_dropped():
    # Notifier.send's signature defaults to level="info"; core modules that
    # emit without naming a level must not spam the chat.
    api = _FakeAPI()
    TelegramNotifier(api, 1).send("unlabelled chatter")
    assert api.sent == []


def test_long_message_is_chunked_under_the_telegram_limit():
    api = _FakeAPI()
    notifier = TelegramNotifier(api, 12345)

    long_message = "\n".join(f"line {i} " + "x" * 100 for i in range(200))
    notifier.send(long_message, level="alert")

    assert len(api.sent) > 1
    assert all(len(text) <= MESSAGE_LIMIT for _chat, text in api.sent)


def test_api_failure_is_swallowed_not_raised():
    # The whole point: a dead Telegram must not abort a sweep mid-sell.
    api = _FakeAPI(explode=RuntimeError("connection reset"))
    notifier = TelegramNotifier(api, 12345)

    notifier.send("stop-loss triggered", level="alert")  # must not raise


def test_a_failed_chunk_does_not_stop_the_remaining_chunks():
    class _FlakyAPI:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._calls = 0

        def send_message(self, chat_id, text, *, reply_markup=None):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("transient")
            self.sent.append(text)
            return {"message_id": self._calls}

    api = _FlakyAPI()
    notifier = TelegramNotifier(api, 1)
    long_message = "\n".join(f"line {i} " + "y" * 100 for i in range(200))

    notifier.send(long_message, level="alert")

    # First chunk was lost to the error; the rest still went out.
    assert len(api.sent) >= 1


def test_missing_chat_id_makes_the_notifier_a_no_op():
    # Guards the wiring: an unconfigured TELEGRAM_CHAT_ID must not turn every
    # alert into an exception, and must not send to a bogus chat.
    api = _FakeAPI()
    notifier = TelegramNotifier(api, None)

    notifier.send("stop-loss triggered", level="alert")

    assert api.sent == []


# ---------------------------------------------------------------------------
# monitor wiring: which sink the continuous-mode script actually builds
# ---------------------------------------------------------------------------
def _settings(**overrides):
    from polymarket_bot.config import Settings

    base = {"private_key": "0xdead", "wallet": "0xbeef"}
    base.update(overrides)
    return Settings(**base)


def test_monitor_builds_telegram_sink_when_both_credentials_are_set():
    from polymarket_bot.notify import MultiNotifier
    from polymarket_bot.scripts.monitor import _build_notifier

    notifier = _build_notifier(
        _settings(telegram_bot_token="token", telegram_chat_id="999"), as_json=False
    )

    assert isinstance(notifier, MultiNotifier)
    assert any(isinstance(sink, TelegramNotifier) for sink in notifier._notifiers)


def test_monitor_stays_console_only_when_telegram_is_unconfigured():
    from polymarket_bot.notify import ConsoleNotifier
    from polymarket_bot.scripts.monitor import _build_notifier

    notifier = _build_notifier(_settings(), as_json=False)

    assert isinstance(notifier, ConsoleNotifier)


def test_monitor_stays_console_only_when_only_the_token_is_set():
    # A token without a chat id has nowhere to send. Half-configured must not
    # mean half-working.
    from polymarket_bot.notify import ConsoleNotifier
    from polymarket_bot.scripts.monitor import _build_notifier

    notifier = _build_notifier(_settings(telegram_bot_token="token"), as_json=False)

    assert isinstance(notifier, ConsoleNotifier)


def test_json_mode_never_pushes_to_telegram():
    # --json is for scheduled/scripted runs; those are not the audience for a
    # phone alert, and the JSONL stream must stay the only output.
    from polymarket_bot.scripts.monitor import _build_notifier

    notifier = _build_notifier(
        _settings(telegram_bot_token="token", telegram_chat_id="999"), as_json=True
    )

    assert not isinstance(notifier, TelegramNotifier)
    assert type(notifier).__name__ == "_JsonNotifier"
