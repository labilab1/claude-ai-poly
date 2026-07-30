"""Tests for the Telegram UI translation table and language preference.

The property that matters most: a key with only one language must be a loud
failure, not a silent English string appearing in a Hebrew menu. Missing
translations are the normal decay mode of an i18n table, and they are
invisible unless something checks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from polymarket_bot.config import Settings
from polymarket_bot.telegram import i18n
from polymarket_bot.telegram.i18n import LANGUAGES, t


def _settings(tmp: Path) -> Settings:
    return Settings(private_key="0x0", wallet="0xw", data_dir=tmp)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


def test_every_key_defines_every_language():
    missing: list[str] = []
    for key, translations in i18n._STRINGS.items():
        for lang in LANGUAGES:
            if not translations.get(lang):
                missing.append(f"{key}.{lang}")
    assert missing == [], f"keys missing a translation: {missing}"


def test_translation_placeholders_match_across_languages():
    """A placeholder present in one language and absent in the other means one
    of them renders with a stray value or raises KeyError at format time."""
    import re

    bad: list[str] = []
    for key, translations in i18n._STRINGS.items():
        sets = {
            lang: set(re.findall(r"\{(\w+)", text)) for lang, text in translations.items()
        }
        reference = sets[LANGUAGES[0]]
        for lang, names in sets.items():
            if names != reference:
                bad.append(f"{key}: {lang}={sorted(names)} vs {LANGUAGES[0]}={sorted(reference)}")
    assert bad == [], f"placeholder mismatch: {bad}"


def test_t_returns_the_requested_language():
    assert t("menu.hot", "en") != t("menu.hot", "he")


def test_t_formats_parameters():
    rendered = t("list.page", "en", page=2, pages=5)
    assert "2" in rendered and "5" in rendered


def test_unknown_key_raises_rather_than_returning_the_key():
    # Returning the key would ship "menu.nope" to a user as if it were text.
    with pytest.raises(KeyError):
        t("menu.nope", "en")


def test_unknown_language_falls_back_to_english_rather_than_raising():
    # A corrupted preference file must not break every screen.
    assert t("menu.hot", "kl") == t("menu.hot", "en")


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_default_language_is_english():
    with tempfile.TemporaryDirectory() as td:
        assert i18n.load_lang(_settings(Path(td)), 12345) == "en"


def test_a_saved_language_survives_a_reload():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        i18n.save_lang(settings, 12345, "he")
        assert i18n.load_lang(settings, 12345) == "he"


def test_languages_are_stored_per_chat():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        i18n.save_lang(settings, 111, "he")
        i18n.save_lang(settings, 222, "en")
        assert i18n.load_lang(settings, 111) == "he"
        assert i18n.load_lang(settings, 222) == "en"


def test_saving_one_chat_does_not_drop_another():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        i18n.save_lang(settings, 111, "he")
        i18n.save_lang(settings, 222, "he")
        i18n.save_lang(settings, 111, "en")
        assert i18n.load_lang(settings, 222) == "he", "a write clobbered another chat's setting"


def test_an_unwritable_preference_file_does_not_crash_the_read():
    # A broken prefs file must degrade to the default, not take down the menu.
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        settings.ensure_data_dir()
        (settings.data_dir / "telegram_prefs.json").write_text("{not json", encoding="utf-8")
        assert i18n.load_lang(settings, 12345) == "en"


def test_an_unknown_stored_language_degrades_to_the_default():
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        i18n.save_lang(settings, 1, "en")
        (settings.data_dir / "telegram_prefs.json").write_text(
            '{"1": {"lang": "klingon"}}', encoding="utf-8"
        )
        assert i18n.load_lang(settings, 1) == "en"


def test_toggle_flips_between_the_two_languages():
    assert i18n.toggled("en") == "he"
    assert i18n.toggled("he") == "en"
