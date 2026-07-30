"""A per-chat list of markets the owner wants to keep an eye on.

Deliberately separate from `rules.py`. An exit rule is a standing instruction
that can SELL; a watchlist entry is a bookmark that can do nothing at all. They
are stored apart so that nothing here is ever one refactor away from acquiring
the power to trade.

Persisted, unlike menu sessions: a bookmark that vanished when the bot
restarted would not be a bookmark. Storage is the same atomic-write pattern the
rest of the package uses, and every read degrades to "empty" rather than
raising - a corrupt bookmark file must not take down a screen.
"""

from __future__ import annotations

import json
import os
from typing import Final
from uuid import uuid4

from polymarket_bot.config import Settings

_FILENAME: Final[str] = "telegram_watchlist.json"

#: Cap per chat. A watchlist is a shortlist; past this it is a second copy of
#: the market list, and every entry costs an API read when the list is rendered.
MAX_ENTRIES: Final[int] = 25


class Watchlist:
    """Chat id -> ordered market refs. Newest first."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- storage -------------------------------------------------------
    @property
    def path(self):
        return self._settings.data_dir / _FILENAME

    def _read(self) -> dict[str, list[str]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        # Defensive: this file is user-editable, so coerce rather than trust.
        out: dict[str, list[str]] = {}
        for chat_id, refs in data.items():
            if isinstance(refs, list):
                out[str(chat_id)] = [str(r) for r in refs if isinstance(r, str) and r]
        return out

    def _write(self, data: dict[str, list[str]]) -> None:
        self._settings.ensure_data_dir()
        path = self.path
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid4().hex[:6]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    # ---- api -----------------------------------------------------------
    def list(self, chat_id: int | str) -> list[str]:
        return list(self._read().get(str(chat_id), []))

    def contains(self, chat_id: int | str, market_ref: str) -> bool:
        return market_ref in self._read().get(str(chat_id), [])

    def toggle(self, chat_id: int | str, market_ref: str) -> bool:
        """Add or remove. Returns True if the market is now being watched.

        Read-modify-write so a second chat's list is never dropped, matching
        the language preference store.
        """
        data = self._read()
        key = str(chat_id)
        refs = data.get(key, [])
        if market_ref in refs:
            refs = [r for r in refs if r != market_ref]
            watching = False
        else:
            # Newest first, and bounded: the oldest bookmark falls off rather
            # than the list growing until rendering it times out.
            refs = [market_ref, *refs][:MAX_ENTRIES]
            watching = True
        data[key] = refs
        self._write(data)
        return watching

    def clear(self, chat_id: int | str) -> None:
        data = self._read()
        data.pop(str(chat_id), None)
        self._write(data)
