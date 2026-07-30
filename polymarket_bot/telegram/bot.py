"""Telegram front end for the Polymarket bot.

A thin command router over `service.py` - the same contract the CLI scripts
use, so this bot cannot do anything the terminal could not already do. Two
things this file is deliberately careful about:

  Single-owner lock: every update is checked against `settings.telegram_chat_id`
  before anything runs. Until that is set, the bot answers ONLY with the
  sender's chat id (so the owner can find it) and does nothing else - no
  command, trading or not, executes for an unconfigured bot. Once set, any
  other chat is silently ignored: no error, no hint this is a trading bot.

  Confirm is bound to the plan, not re-typed: /buy and /sell call
  `service.buy`/`service.sell` with `confirm=False` to get a priced plan back
  (nothing is sent), attach an inline Confirm/Cancel keyboard, and store the
  exact `confirm_args` the preview returned. Tapping Confirm replays those
  arguments verbatim - never fewer, never re-derived from the original text -
  so a fill that lands between preview and tap cannot silently trade more
  than what was shown (`service._guard_confirmed` enforces this on top).

Latency: a `SecureClient` is created once and reused for the process lifetime
(passed as `client=` into every `service` call), so a command does not pay for
a fresh auth handshake on every message - see `client.py`. Updates are
long-polled (`api.get_updates`), which delivers close to instantly without a
public HTTPS endpoint.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from polymarket_bot import service
from polymarket_bot.client import get_client
from polymarket_bot.config import Settings
from polymarket_bot.notify import ConsoleNotifier, Notifier
from polymarket_bot.scripts._common import with_disclaimer
from polymarket_bot.telegram import menu as menu_mod
from polymarket_bot.telegram.api import TelegramAPI, chunk_message, confirm_keyboard
from polymarket_bot.telegram.i18n import load_lang, save_lang, t, toggled

_PENDING_TTL_SECONDS = 120

# Callback prefixes owned by the trade confirm flow. Everything else is
# navigation and must never be looked up in `_pending` - see `_handle_callback`.
_TRADE_ACTIONS = frozenset({"confirm", "cancel"})

# Navigation prefix, reserved here so the router knows it even before the menu
# module is wired in.
_NAV_ACTION = "nav"

# How many markets a menu list fetches before paginating them locally. Paging
# in memory beats re-scanning per page: a scan is bounded network I/O, and the
# owner tapping "next" should not wait for another sweep.
_LIST_FETCH = 30


def _int(value: str, default: int = 0) -> int:
    """Callback arguments are strings from an untrusted round trip."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class _Pending:
    action: str  # "buy" or "sell"
    market_ref: str
    outcome: str
    confirm_args: dict
    chat_id: int
    message_id: int | None
    created_at: float


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        api: TelegramAPI | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        if not settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set - see .env.example.")
        self.settings = settings
        self.api = api or TelegramAPI(settings.telegram_bot_token)
        self.client = client or get_client()
        self._owns_client = client is None
        self.notifier = notifier or ConsoleNotifier()
        self._offset: int | None = None
        self._pending: dict[str, _Pending] = {}
        self._running = False
        self._sessions = menu_mod.SessionStore()
        # Persistent-keyboard buttons arrive as plain text, so the router
        # recognises them by label - in either language, since a toggle does
        # not repaint a keyboard already sitting on someone's screen.
        self._reply_labels = menu_mod.reply_labels("en")
        self._commands = {
            # /start opens the menu; /help still lists the typed commands, which
            # remain the complete, scriptable interface.
            "start": self._cmd_start,
            "help": self._cmd_help,
            "menu": self._cmd_start,
            "status": self._cmd_status,
            "positions": self._cmd_positions,
            "scan": self._cmd_scan,
            "market": self._cmd_market,
            "opportunities": self._cmd_opportunities,
            "opp": self._cmd_opportunities,
            "analyze": self._cmd_analyze,
            "buy": self._cmd_buy,
            "sell": self._cmd_sell,
            "rule": self._cmd_rule,
            "rules": self._cmd_rules,
            "removerule": self._cmd_removerule,
            "monitor": self._cmd_monitor,
            "redeem": self._cmd_redeem,
            "cancel": self._cmd_cancel,
        }

    def close(self) -> None:
        if self._owns_client:
            try:
                self.client.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False

    # ---- main loop ---------------------------------------------------------
    def run_forever(self, *, max_iterations: int | None = None) -> None:
        self._running = True
        me = self.api.get_me()  # fails loudly here: a bad token should not run silently
        self._log(f"Telegram bot online as @{me.get('username')}. Long-polling for updates.")
        if not self.settings.telegram_chat_id:
            self._log(
                "TELEGRAM_CHAT_ID is not set - message the bot once to learn your chat id, "
                "then set it in .env and restart.",
                "alert",
            )

        iterations = 0
        backoff = 1.0
        while self._running:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            try:
                updates = self.api.get_updates(offset=self._offset)
                backoff = 1.0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._log(
                    f"getUpdates failed ({type(exc).__name__}: {exc}); retrying in {backoff:g}s",
                    "error",
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            for update in updates:
                # Ack immediately, before handling: a bug in one update must
                # not make Telegram redeliver it forever.
                self._offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception as exc:
                    self._log(
                        f"Error handling update {update.get('update_id')}: "
                        f"{type(exc).__name__}: {exc}",
                        "error",
                    )

    def _log(self, message: str, level: str = "info") -> None:
        if self.notifier is not None:
            self.notifier.send(message, level=level)  # type: ignore[arg-type]

    # ---- routing -------------------------------------------------------
    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(message, text)
            return
        # Plain text is now meaningful: it is either a persistent-keyboard
        # button or the answer to a prompt the bot is waiting on.
        self._handle_text(message, text)

    def _is_authorized(self, chat_id: Any) -> bool:
        allowed = self.settings.telegram_chat_id
        return bool(allowed) and str(chat_id) == str(allowed)

    def _handle_text(self, message: dict, text: str) -> None:
        """A plain (non-command) message: a menu button, or a prompt answer."""
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not self._is_authorized(chat_id):
            # Same silence as an unauthorized command: a stranger who finds the
            # bot learns nothing, not even that it is listening.
            return

        session = self._session(chat_id)

        view = self._reply_labels.get(text)
        if view is not None:
            # A keyboard tap always wins over an outstanding prompt: the owner
            # navigating away is a cancellation of whatever was being asked.
            session.clear_prompt()
            self._show_guarded(chat_id, session, view, "")
            return

        awaiting = session.awaiting
        if awaiting == menu_mod.AWAIT_SEARCH:
            session.clear_prompt()
            session.query = text
            session.page = 0
            self._show_guarded(chat_id, session, menu_mod.VIEW_SEARCH, "0")
            return
        if awaiting == menu_mod.AWAIT_BUY_AMOUNT:
            self._finish_buy_prompt(chat_id, session, text)
            return
        # Nothing was expected. Re-show the keyboard rather than ignoring the
        # owner, who may simply have lost it.
        self._send(chat_id, t("msg.welcome", session.lang), session)

    def _session(self, chat_id: int) -> menu_mod.MenuSession:
        session = self._sessions.get(chat_id, lang=load_lang(self.settings, chat_id))
        return session

    def _send(self, chat_id: int, text: str, session: menu_mod.MenuSession) -> int | None:
        """Send with the persistent keyboard attached."""
        return self._reply(chat_id, menu_mod.persistent_keyboard(session.lang), text)

    def _handle_command(self, message: dict, text: str) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        if not self.settings.telegram_chat_id:
            # No owner configured yet. Tell whoever is messaging their chat id
            # and stop - nothing else runs until TELEGRAM_CHAT_ID is set.
            self._reply(
                chat_id,
                None,
                f"Your chat id is {chat_id}.\n"
                f"Set TELEGRAM_CHAT_ID={chat_id} in .env and restart the bot to use it.",
            )
            return

        if not self._is_authorized(chat_id):
            self._log(f"Ignored command from unauthorized chat {chat_id!r}.", "debug")
            return

        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()  # "/buy@MyBot" -> "buy"
        args = parts[1:]
        handler = self._commands.get(cmd)
        if handler is None:
            self._reply(chat_id, None, f"Unknown command /{cmd}. Send /help for the list.")
            return
        try:
            handler(chat_id, args)
        except Exception as exc:
            self._reply(chat_id, None, f"Error running /{cmd}: {type(exc).__name__}: {exc}")

    def _handle_callback(self, callback: dict) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        if not self._is_authorized(chat_id):
            self._ack(callback_id)
            return

        if ":" not in data:
            self._ack(callback_id, "Unrecognized action.")
            return
        action, token = data.split(":", 1)

        # Namespaces are routed on the prefix, and ONLY the trade prefixes may
        # touch `_pending`. Looking every callback up in the pending-trade map
        # meant a menu button answered "this confirmation is no longer valid" -
        # a dead trade button - for a tap that was never a trade.
        if action not in _TRADE_ACTIONS:
            self._handle_nav(callback_id, chat_id, message_id, action, token)
            return

        pending = self._pending.pop(token, None)

        if pending is None:
            self._ack(callback_id, "This button already expired.")
            self._safe_edit(
                chat_id, message_id,
                "This confirmation is no longer valid - it was already used, or the bot "
                "restarted. Re-run the command for a fresh preview.",
            )
            return

        if time.monotonic() - pending.created_at > _PENDING_TTL_SECONDS:
            self._ack(callback_id, "Expired - prices may have moved.")
            self._safe_edit(
                chat_id, message_id,
                "This confirmation expired (prices move fast). Re-run the command for a "
                "fresh preview.",
            )
            return

        if action == "cancel":
            self._ack(callback_id, "Cancelled.")
            self._safe_edit(chat_id, message_id, "Cancelled - nothing was sent.")
            return

        if action != "confirm":
            self._ack(callback_id, "Unrecognized action.")
            return

        self._ack(callback_id, "Sending...")
        try:
            if pending.action == "buy":
                result = service.buy(
                    pending.market_ref, pending.outcome, **pending.confirm_args, client=self.client
                )
            else:
                result = service.sell(
                    pending.market_ref, pending.outcome, **pending.confirm_args, client=self.client
                )
            text = str(result.get("text") or "(no response)")
        except Exception as exc:
            text = f"Order failed unexpectedly: {type(exc).__name__}: {exc}"
        self._safe_edit(chat_id, message_id, with_disclaimer(text))

    def _handle_nav(
        self, callback_id: str | None, chat_id: int, message_id: int | None, action: str, token: str
    ) -> None:
        """Non-trade callbacks. Menu navigation lands here; anything else is
        an unknown button and is reported as such rather than being silently
        mistaken for a stale trade confirmation."""
        if action != _NAV_ACTION:
            self._ack(callback_id, "Unrecognized action.")
            return

        session = self._session(chat_id)
        view, arg = menu_mod.parse_nav(token)
        # Acknowledge immediately: Telegram spins the button until this lands,
        # and a screen that fetches live data takes longer than that patience.
        self._ack(callback_id, t("msg.loading", session.lang))
        self._show_guarded(chat_id, session, view, arg)

    # ---- screens -----------------------------------------------------------
    def _show_guarded(
        self, chat_id: int, session: menu_mod.MenuSession, view: str, arg: str
    ) -> None:
        """Render a screen, turning any failure into a message.

        Every entry point into the menu goes through here rather than calling
        `_show` directly: a screen reached by typing must fail as gracefully as
        one reached by tapping, and `service` raising is a live possibility on
        every one of them.
        """
        try:
            self._show(chat_id, session, view, arg)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._log(f"menu view {view!r} failed: {detail}", "error")
            self._send(chat_id, t("msg.error", session.lang, error=detail), session)

    def _show(self, chat_id: int, session: menu_mod.MenuSession, view: str, arg: str) -> None:
        """Render one screen. Every branch ends in a message with the
        persistent keyboard attached, so the menu is never lost."""
        session.view = view
        if view == menu_mod.VIEW_HOT:
            self._show_list(chat_id, session, sort="hot", page=_int(arg))
        elif view == menu_mod.VIEW_SEARCH:
            if not session.query:
                session.awaiting = menu_mod.AWAIT_SEARCH
                self._send(chat_id, t("prompt.search", session.lang), session)
            else:
                self._show_list(chat_id, session, sort="hot", page=_int(arg))
        elif view == menu_mod.VIEW_MARKET:
            self._show_market(chat_id, session, arg)
        elif view == menu_mod.VIEW_BUY:
            self._start_buy(chat_id, session, arg)
        elif view == menu_mod.VIEW_PORTFOLIO:
            result = service.positions(client=self.client)
            self._send(chat_id, str(result.get("text") or ""), session)
        elif view == menu_mod.VIEW_LANG:
            self._toggle_language(chat_id, session)
        elif view == menu_mod.VIEW_MORE:
            self._show_more(chat_id, session, arg)
        else:
            self._send(chat_id, t("msg.welcome", session.lang), session)

    def _show_list(self, chat_id: int, session: menu_mod.MenuSession, *, sort: str, page: int) -> None:
        lang = session.lang
        result = service.scan(
            limit=_LIST_FETCH, keyword=session.query, sort=sort, client=self.client
        )
        if not result.get("ok"):
            self._send(chat_id, t("msg.error", lang, error=str(result.get("error"))), session)
            return

        rows = list(result.get("markets") or [])
        if not rows:
            text = (
                t("list.search_empty", lang, query=session.query)
                if session.query
                else t("list.empty", lang)
            )
            self._send(chat_id, text, session)
            return

        title = (
            t("title.search", lang, query=session.query)
            if session.query
            else t("title.hot", lang)
        )
        text, page_rows, pages = menu_mod.render_list(
            rows, lang=lang, page=page, title=title, note=t("msg.hot_note", lang)
        )
        session.page = max(0, min(page, pages - 1))

        # Fresh tokens for this page. The refs map is rebuilt rather than
        # appended to, so it cannot grow without bound over a long session.
        session.refs = {}
        entries = [
            (session.remember(str(row.get("slug") or row.get("condition_id") or "")), row.get("url"))
            for row in page_rows
        ]
        view = menu_mod.VIEW_SEARCH if session.query else menu_mod.VIEW_HOT
        keyboard = menu_mod.market_rows_keyboard(
            entries, lang=lang, page=session.page, pages=pages, view=view
        )
        # Only the inline keyboard here. Telegram allows one markup per
        # message, and a reply keyboard already sent stays on screen until it
        # is explicitly replaced - so the persistent menu is not lost by
        # sending an inline keyboard alongside it.
        self._reply(chat_id, keyboard, text)

    def _show_market(self, chat_id: int, session: menu_mod.MenuSession, token: str) -> None:
        ref = session.resolve(token)
        if ref is None:
            self._send(chat_id, t("msg.expired", session.lang), session)
            return
        result = service.briefing(ref, client=self.client)
        if not result.get("ok"):
            self._send(
                chat_id, t("msg.error", session.lang, error=str(result.get("error"))), session
            )
            return
        text = with_disclaimer(str(result.get("text") or ""))
        back = menu_mod.VIEW_SEARCH if session.query else menu_mod.VIEW_HOT
        keyboard = menu_mod.market_keyboard(
            token, result.get("url"), lang=session.lang, back_view=back
        )
        self._reply(chat_id, keyboard, text)

    def _show_more(self, chat_id: int, session: menu_mod.MenuSession, arg: str) -> None:
        lang = session.lang
        if not arg:
            self._reply(chat_id, menu_mod.more_keyboard(lang), t("title.more", lang))
            return
        actions = {
            "status": lambda: service.status(client=self.client),
            # list_rules reads the local rule store; it takes no client.
            "rules": lambda: service.list_rules(),
            "analyze": lambda: service.analytics(client=self.client),
            "monitor": lambda: service.monitor_once(client=self.client),
            "redeem": lambda: service.redeem(client=self.client),
            "cancel": lambda: service.cancel_orders(client=self.client),
        }
        action = actions.get(arg)
        if action is None:
            self._send(chat_id, t("msg.welcome", lang), session)
            return
        result = action()
        self._send(chat_id, str(result.get("text") or ""), session)

    def _toggle_language(self, chat_id: int, session: menu_mod.MenuSession) -> None:
        session.lang = toggled(session.lang)
        try:
            save_lang(self.settings, chat_id, session.lang)
        except Exception as exc:
            # A preference that could not be written is worth saying out loud:
            # otherwise it silently reverts on restart.
            self._log(f"could not persist language: {type(exc).__name__}: {exc}", "error")
        self._send(chat_id, t("msg.language_set", session.lang), session)
        self._reply(chat_id, menu_mod.more_keyboard(session.lang), t("title.more", session.lang))

    # ---- buying from the menu ---------------------------------------------
    def _start_buy(self, chat_id: int, session: menu_mod.MenuSession, arg: str) -> None:
        """Ask for an amount. Nothing is priced or sent until it arrives."""
        side, _, token = arg[:1], None, arg[1:]
        ref = session.resolve(token)
        if ref is None or side not in ("y", "n"):
            self._send(chat_id, t("msg.expired", session.lang), session)
            return
        outcome = "yes" if side == "y" else "no"
        session.awaiting = menu_mod.AWAIT_BUY_AMOUNT
        session.pending_market = ref
        session.pending_outcome = outcome
        self._send(
            chat_id,
            t("prompt.buy_amount", session.lang, outcome=outcome.upper()),
            session,
        )

    def _finish_buy_prompt(self, chat_id: int, session: menu_mod.MenuSession, text: str) -> None:
        """Turn a typed amount into a PREVIEW. Still nothing sent: this goes
        through the same `confirm=False` path as /buy, so the owner gets the
        priced plan and the Confirm/Cancel keyboard exactly as before."""
        try:
            usd = float(text.replace(",", "").replace("$", "").strip())
            if usd <= 0:
                raise ValueError
        except ValueError:
            self._send(chat_id, t("prompt.bad_amount", session.lang, value=text), session)
            return

        market_ref = session.pending_market or ""
        outcome = session.pending_outcome or "yes"
        session.clear_prompt()
        result = service.buy(market_ref, outcome, usd, client=self.client)
        self._present_trade_preview(
            chat_id, action="buy", market_ref=market_ref, outcome=outcome, result=result
        )

    def _ack(self, callback_id: str | None, text: str | None = None) -> None:
        if not callback_id:
            return
        try:
            self.api.answer_callback_query(callback_id, text=text)
        except Exception:
            pass  # a missed toast is not worth failing the whole update over

    # ---- outbound helpers ------------------------------------------------
    def _reply(self, chat_id: int, reply_markup: dict | None, text: str) -> int | None:
        """Send a message, chunking on Telegram's length limit. The keyboard
        (if any) rides on the last chunk. Returns that chunk's message_id."""
        message_id = None
        chunks = chunk_message(text)
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            try:
                sent = self.api.send_message(chat_id, chunk, reply_markup=markup)
                message_id = sent.get("message_id")
            except Exception as exc:
                self._log(f"send_message failed: {type(exc).__name__}: {exc}", "error")
        return message_id

    def _safe_edit(self, chat_id: int, message_id: int | None, text: str) -> None:
        if message_id is None:
            self._reply(chat_id, None, text)
            return
        if len(text) > 4096:
            text = text[:4090] + " [...]"
        try:
            self.api.edit_message_text(chat_id, message_id, text)
        except Exception as exc:
            self._log(f"edit_message_text failed ({exc}); sending a new message instead.", "debug")
            self._reply(chat_id, None, text)

    def _present_trade_preview(
        self, chat_id: int, *, action: str, market_ref: str, outcome: str, result: dict
    ) -> None:
        text = with_disclaimer(str(result.get("text") or "(no response)"))
        if result.get("needs_confirmation") and result.get("confirm_args"):
            token = uuid.uuid4().hex[:10]
            sent_id = self._reply(chat_id, confirm_keyboard(token), text)
            if sent_id is not None:
                self._pending[token] = _Pending(
                    action=action,
                    market_ref=market_ref,
                    outcome=outcome,
                    confirm_args=result["confirm_args"],
                    chat_id=chat_id,
                    message_id=sent_id,
                    created_at=time.monotonic(),
                )
        else:
            self._reply(chat_id, None, text)

    # ---- commands ----------------------------------------------------------
    def _cmd_start(self, chat_id: int, args: list[str]) -> None:
        """Open the menu: welcome text plus the persistent keyboard."""
        session = self._session(chat_id)
        session.clear_prompt()
        session.query = None
        self._send(chat_id, t("msg.welcome", session.lang), session)

    def _cmd_help(self, chat_id: int, args: list[str]) -> None:
        lines = [
            "Polymarket bot.",
            "",
            "  /menu - open the button menu (hot markets, search, portfolio)",
            "",
            "Typed commands do everything the menu does, and a few things it",
            "does not (limit orders, selling, exit rules):",
            "  /status - cash, positions, P&L, rules, monitor mode",
            "  /positions [all] - open positions ('all' includes settled)",
            "  /scan [keyword] - tradable markets, tightest spread first",
            "  /market <slug-or-0x> - deep-dive one market",
            "  /opportunities [keyword] - tradability-ranked markets",
            "  /analyze - your trading record and insights",
            "  /buy <market> <yes|no> <usd> [limit_price]",
            "  /sell <market> <yes|no> <all|shares> [limit_price]",
            "  /rule <market> <yes|no> <stop_loss|take_profit> pct <n>",
            "  /rule <market> <yes|no> <stop_loss|take_profit> price <p>",
            "  /rule <market> <yes|no> trailing_stop trail <n>",
            "  /rules - list stored exit rules",
            "  /removerule <id> - delete a stored rule",
            "  /monitor - run one rule-check pass (dry-run; the running",
            "             process, not this command, is what enforces rules live)",
            "  /redeem - claim settled positions",
            "  /cancel - cancel all resting orders",
            "",
            "Every /buy and /sell shows a preview with Confirm/Cancel buttons first -",
            "nothing is ever sent from a typed command alone.",
        ]
        self._reply(chat_id, None, "\n".join(lines))

    def _cmd_status(self, chat_id: int, args: list[str]) -> None:
        result = service.status(client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_positions(self, chat_id: int, args: list[str]) -> None:
        include_resolved = bool(args) and args[0].lower() in ("all", "resolved")
        result = service.positions(include_resolved=include_resolved, client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_scan(self, chat_id: int, args: list[str]) -> None:
        keyword = " ".join(args) if args else None
        result = service.scan(limit=15, keyword=keyword, client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_market(self, chat_id: int, args: list[str]) -> None:
        if not args:
            self._reply(chat_id, None, "Usage: /market <slug-or-0x-condition-id>")
            return
        result = service.briefing(args[0], client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_opportunities(self, chat_id: int, args: list[str]) -> None:
        keyword = " ".join(args) if args else None
        result = service.opportunities(limit=15, keyword=keyword, client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_analyze(self, chat_id: int, args: list[str]) -> None:
        result = service.analytics(client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_buy(self, chat_id: int, args: list[str]) -> None:
        if len(args) < 3:
            self._reply(chat_id, None, "Usage: /buy <market> <yes|no> <usd> [limit_price]")
            return
        market_ref, outcome, usd_s, *rest = args
        try:
            usd = float(usd_s)
        except ValueError:
            self._reply(chat_id, None, f"'{usd_s}' is not a number.")
            return
        limit_price = None
        if rest:
            try:
                limit_price = float(rest[0])
            except ValueError:
                self._reply(chat_id, None, f"'{rest[0]}' is not a valid limit price.")
                return
        result = service.buy(
            market_ref, outcome, usd, limit_price=limit_price, confirm=False, client=self.client
        )
        self._present_trade_preview(
            chat_id, action="buy", market_ref=market_ref, outcome=outcome, result=result
        )

    def _cmd_sell(self, chat_id: int, args: list[str]) -> None:
        if len(args) < 3:
            self._reply(chat_id, None, "Usage: /sell <market> <yes|no> <all|shares> [limit_price]")
            return
        market_ref, outcome, size_s, *rest = args
        shares = None
        fraction = None
        if size_s.lower() == "all":
            fraction = 1.0
        else:
            try:
                shares = float(size_s)
            except ValueError:
                self._reply(chat_id, None, f"'{size_s}' is not 'all' or a number of shares.")
                return
        limit_price = None
        if rest:
            try:
                limit_price = float(rest[0])
            except ValueError:
                self._reply(chat_id, None, f"'{rest[0]}' is not a valid limit price.")
                return
        result = service.sell(
            market_ref, outcome, shares=shares, fraction=fraction,
            limit_price=limit_price, confirm=False, client=self.client,
        )
        self._present_trade_preview(
            chat_id, action="sell", market_ref=market_ref, outcome=outcome, result=result
        )

    def _cmd_rule(self, chat_id: int, args: list[str]) -> None:
        if len(args) < 5:
            self._reply(
                chat_id, None,
                "Usage:\n"
                "  /rule <market> <yes|no> stop_loss pct <-25>\n"
                "  /rule <market> <yes|no> stop_loss price <0.30>\n"
                "  /rule <market> <yes|no> take_profit pct <50>\n"
                "  /rule <market> <yes|no> take_profit price <0.80>\n"
                "  /rule <market> <yes|no> trailing_stop trail <20>",
            )
            return
        market_ref, outcome, kind, mode, value_s = args[:5]
        try:
            value = float(value_s)
        except ValueError:
            self._reply(chat_id, None, f"'{value_s}' is not a number.")
            return
        mode = mode.lower()
        kwargs: dict[str, float] = {}
        if mode == "pct":
            kwargs["target_pct"] = value
        elif mode == "price":
            kwargs["target_price"] = value
        elif mode == "trail":
            kwargs["trail_pct"] = value
        else:
            self._reply(chat_id, None, f"Unknown mode '{mode}': expected pct, price or trail.")
            return
        result = service.set_rule(market_ref, outcome, kind.lower(), client=self.client, **kwargs)
        self._reply(chat_id, None, with_disclaimer(str(result.get("text") or "(no response)")))

    def _cmd_rules(self, chat_id: int, args: list[str]) -> None:
        result = service.list_rules()
        self._reply(chat_id, None, with_disclaimer(str(result.get("text") or "(no response)")))

    def _cmd_removerule(self, chat_id: int, args: list[str]) -> None:
        if not args:
            self._reply(chat_id, None, "Usage: /removerule <rule_id>")
            return
        result = service.remove_rule(args[0])
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_monitor(self, chat_id: int, args: list[str]) -> None:
        result = service.monitor_once(client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_redeem(self, chat_id: int, args: list[str]) -> None:
        result = service.redeem(client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))

    def _cmd_cancel(self, chat_id: int, args: list[str]) -> None:
        result = service.cancel_orders(client=self.client)
        self._reply(chat_id, None, str(result.get("text") or "(no response)"))
