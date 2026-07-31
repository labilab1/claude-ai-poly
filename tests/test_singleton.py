"""Tests for the one-bot-per-data-directory lock.

The defect this prevents, observed live: four bot processes ended up polling
the same token because each "restart" killed a shell pipeline and left the
Python process running. Telegram hands an update to whichever poller asks
first, so the owner's taps were answered at random by processes running older
code - "unknown command /menu" for a command that exists, and "this
confirmation is no longer valid" for a fresh button.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.scripts import telegram_bot as script
from polymarket_bot.telegram.singleton import AlreadyRunning, SingleInstance


def _settings(tmp: Path) -> Settings:
    return Settings(
        private_key="0x0", wallet="0xw", data_dir=tmp,
        telegram_bot_token="tok", telegram_chat_id="1",
    )


# ---------------------------------------------------------------------------
# acquiring
# ---------------------------------------------------------------------------


def test_the_first_instance_acquires_the_lock(tmp_path):
    lock = SingleInstance(_settings(tmp_path))
    lock.acquire()
    assert lock.path.exists()
    lock.release()


def test_the_lock_records_the_owning_pid(tmp_path):
    lock = SingleInstance(_settings(tmp_path))
    lock.acquire()
    assert json.loads(lock.path.read_text())["pid"] == os.getpid()
    lock.release()


def test_a_second_instance_is_refused_while_the_first_lives(tmp_path):
    settings = _settings(tmp_path)
    first = SingleInstance(settings)
    first.acquire()

    second = SingleInstance(settings)
    # A different, living pid holds it.
    with mock.patch.object(second, "_read_owner", return_value=999999), \
         mock.patch("polymarket_bot.telegram.singleton._pid_alive", return_value=True):
        with pytest.raises(AlreadyRunning) as info:
            second.acquire()
    assert "already running" in str(info.value)
    first.release()


def test_the_refusal_names_the_lock_file_so_it_can_be_cleared(tmp_path):
    settings = _settings(tmp_path)
    lock = SingleInstance(settings)
    with mock.patch.object(lock, "_read_owner", return_value=999999), \
         mock.patch("polymarket_bot.telegram.singleton._pid_alive", return_value=True):
        with pytest.raises(AlreadyRunning) as info:
            lock.acquire()
    assert str(lock.path) in str(info.value)


# ---------------------------------------------------------------------------
# not becoming permanently unstartable
# ---------------------------------------------------------------------------


def test_a_lock_from_a_dead_process_is_reclaimed(tmp_path):
    """A crash must not leave the bot unable to start ever again."""
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / "telegram_bot.lock").write_text('{"pid": 999999}', encoding="utf-8")

    lock = SingleInstance(settings)
    with mock.patch("polymarket_bot.telegram.singleton._pid_alive", return_value=False):
        lock.acquire()  # must not raise
    assert json.loads(lock.path.read_text())["pid"] == os.getpid()


def test_a_corrupt_lock_file_is_treated_as_stale(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_data_dir()
    (tmp_path / "telegram_bot.lock").write_text("{not json", encoding="utf-8")
    SingleInstance(settings).acquire()  # must not raise


def test_re_acquiring_in_the_same_process_is_allowed(tmp_path):
    lock = SingleInstance(_settings(tmp_path))
    lock.acquire()
    lock.acquire()  # same pid; not a conflict
    lock.release()


# ---------------------------------------------------------------------------
# releasing
# ---------------------------------------------------------------------------


def test_release_removes_the_lock(tmp_path):
    lock = SingleInstance(_settings(tmp_path))
    lock.acquire()
    lock.release()
    assert not lock.path.exists()


def test_release_does_not_remove_a_lock_another_process_reclaimed(tmp_path):
    settings = _settings(tmp_path)
    lock = SingleInstance(settings)
    lock.acquire()
    # Someone else decided ours was stale and took it.
    lock.path.write_text('{"pid": 4242}', encoding="utf-8")
    lock.release()
    assert lock.path.exists(), "released a lock owned by another process"


def test_the_context_manager_releases_on_exit(tmp_path):
    settings = _settings(tmp_path)
    with SingleInstance(settings) as lock:
        assert lock.path.exists()
    assert not lock.path.exists()


def test_release_without_acquire_is_harmless(tmp_path):
    SingleInstance(_settings(tmp_path)).release()


# ---------------------------------------------------------------------------
# the script honours it
# ---------------------------------------------------------------------------


def test_the_script_refuses_to_start_a_second_bot(tmp_path, capsys):
    settings = _settings(tmp_path)
    with mock.patch.object(script, "load_settings", return_value=settings), \
         mock.patch.object(
             script.SingleInstance, "acquire",
             side_effect=AlreadyRunning(4242, tmp_path / "telegram_bot.lock"),
         ), \
         mock.patch.object(script, "TelegramBot", side_effect=AssertionError("started anyway")):
        code = script.main([])
    assert code == 1
    assert "already running" in capsys.readouterr().out


def test_the_script_releases_the_lock_when_the_bot_stops(tmp_path):
    settings = _settings(tmp_path)
    fake_bot = mock.Mock()
    fake_bot.run_forever.return_value = None
    with mock.patch.object(script, "load_settings", return_value=settings), \
         mock.patch.object(script, "TelegramBot", return_value=fake_bot):
        script.main(["--max-iterations", "1"])
    assert not (tmp_path / "telegram_bot.lock").exists(), "the lock outlived the process"


def test_the_lock_is_released_even_if_the_bot_raises(tmp_path):
    settings = _settings(tmp_path)
    fake_bot = mock.Mock()
    fake_bot.run_forever.side_effect = RuntimeError("boom")
    with mock.patch.object(script, "load_settings", return_value=settings), \
         mock.patch.object(script, "TelegramBot", return_value=fake_bot):
        script.main([])
    assert not (tmp_path / "telegram_bot.lock").exists(), "a crash left the lock behind"
