"""Settings loaded from environment (.env).

One frozen Settings object is the single source of truth for credentials,
risk limits and paths. Core modules take Settings as an argument rather than
reading os.environ directly, so a future Telegram bot (or tests) can inject
different settings without touching global state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[1]


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _int_env(name: str, default: int) -> int:
    return int(_float_env(name, float(default)))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Credentials, risk limits and runtime paths."""

    private_key: str
    wallet: str

    # ---- risk limits -------------------------------------------------
    # Hard ceiling on a single order, in USDC. Orders above this are refused
    # by trading.py regardless of any confirmation flag.
    max_order_usdc: float = 5.0
    # Max total cost basis allowed in any one market.
    max_position_usdc: float = 10.0
    # Monitor stops opening/closing automatically once realized losses in a
    # rolling 24h window exceed this.
    daily_loss_limit_usdc: float = 10.0
    # Refuse to spend below this much free cash (keeps a buffer).
    min_cash_reserve_usdc: float = 0.0

    # ---- monitor -----------------------------------------------------
    monitor_interval_seconds: int = 60
    # When True the monitor evaluates and reports but never sends orders.
    monitor_dry_run: bool = True

    # ---- storage -----------------------------------------------------
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")

    # ---- notifications (Telegram wired up later) ---------------------
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    @property
    def rules_path(self) -> Path:
        return self.data_dir / "exit_rules.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "monitor_state.json"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load_settings() -> Settings:
    """Build Settings from the environment."""
    data_dir_raw = os.environ.get("POLYMARKET_DATA_DIR", "").strip()
    return Settings(
        private_key=_require("POLYMARKET_PRIVATE_KEY"),
        wallet=_require("POLYMARKET_WALLET"),
        max_order_usdc=_float_env("POLYMARKET_MAX_ORDER_USDC", 5.0),
        max_position_usdc=_float_env("POLYMARKET_MAX_POSITION_USDC", 10.0),
        daily_loss_limit_usdc=_float_env("POLYMARKET_DAILY_LOSS_LIMIT_USDC", 10.0),
        min_cash_reserve_usdc=_float_env("POLYMARKET_MIN_CASH_RESERVE_USDC", 0.0),
        monitor_interval_seconds=_int_env("POLYMARKET_MONITOR_INTERVAL_SECONDS", 60),
        monitor_dry_run=_bool_env("POLYMARKET_MONITOR_DRY_RUN", True),
        data_dir=Path(data_dir_raw) if data_dir_raw else REPO_ROOT / "data",
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )


# Backwards-compatible helpers used by older scripts.
def load_private_key() -> str:
    return _require("POLYMARKET_PRIVATE_KEY")


def load_wallet() -> str:
    return _require("POLYMARKET_WALLET")
