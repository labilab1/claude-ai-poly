"""A Notifier sink that pushes to a Telegram chat.

This is what makes a stop-loss visible when you are not at the machine. The
monitor already emits every state change through the `Notifier` protocol
(`notify.py`); this implementation carries those events to a phone.

Two deliberate behaviours:

  Silence is the default for routine events. `min_level` starts at "trade",
  so `trade`/`alert`/`error` are pushed and `debug`/`info` are not. A sweep
  runs every `POLYMARKET_MONITOR_INTERVAL_SECONDS`; forwarding its per-pass
  chatter would train you to ignore the one message that matters.

  It never raises. A failed send is swallowed, because the caller is often
  mid-sell: `Monitor` emits through the notifier while executing a triggered
  rule, and a Telegram outage must not become an exception that abandons an
  exit half-done. `MultiNotifier` applies the same rule to its fan-out, but
  this sink is frequently used on its own, so it cannot rely on that.
"""

from __future__ import annotations

from typing import Any

from polymarket_bot.notify import Level
from polymarket_bot.telegram.api import chunk_message

# Same ordering as notify.ConsoleNotifier; kept local so a change there is a
# deliberate change here too rather than a silent shift in what reaches a phone.
_ORDER: list[Level] = ["debug", "info", "trade", "alert", "error"]


class TelegramNotifier:
    """Sends notifier events to one chat. Implements `notify.Notifier`."""

    def __init__(self, api: Any, chat_id: int | str | None, *, min_level: Level = "trade") -> None:
        self._api = api
        self._chat_id = chat_id
        self._allowed = set(_ORDER[_ORDER.index(min_level) :])

    def send(self, message: str, *, level: Level = "info") -> None:  # noqa: D102
        # No chat configured means no destination. Not an error: the monitor is
        # expected to run with Telegram unconfigured, and it should stay quiet
        # rather than fail on every event.
        if self._chat_id is None or self._chat_id == "":
            return
        if level not in self._allowed:
            return
        for chunk in chunk_message(str(message)):
            try:
                self._api.send_message(self._chat_id, chunk)
            except Exception:
                # Deliberately broad and deliberately silent: see module
                # docstring. Keep going so a transient failure on one chunk
                # does not swallow the remainder of a multi-part alert.
                continue
