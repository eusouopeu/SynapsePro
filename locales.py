# -*- coding: utf-8 -*-
"""
SynapsePro - Localization Module
================================

A lightweight in-code translation system for the SynapsePro add-on.

UI strings in the source code stay in English and are wrapped with ``_(...)``.
The :func:`_` function looks up the translated value for the currently
active language in the ``locales/<lang>.json`` data files.

The active language is controlled by :data:`USER_LANG`, which is set from
the ``language`` key of ``addon_settings.json`` (see ``__init__.py``).
Changing the value takes effect on the next ``_()`` call – no restart
needed. UI that has already been rendered (window titles, existing menu
entries, etc.) only updates the next time it is rebuilt.

Supported values for :data:`USER_LANG`:

* ``"auto"`` – detect from Anki's UI language (fallback: ``"en"``)
* ``"en"``   – English (source language; keys are returned as-is)
* ``"de"``   – Deutsch
* ``"es"``   – Español
* ``"ko"``   – 한국어
* ``"pt"``   – Português
* ``"fr"``   – Français
* ``"vi"``   – Tiếng Việt
* ``"zh"``   – 中文
* ``"hi"``   – हिन्दी


Where the translations live
----------------------------
Every non-English string used to live in one ~600 KB Python dict literal in
this file (plus a second one in ``web_translations.py``), all of it parsed
and held in memory on every Anki startup no matter which language — often
none of them — the user actually has active.

They now live in small per-language JSON files under ``locales/``:

* ``locales/<lang>.json`` — what :func:`_` actually looks up. One combined,
  already-precedence-resolved table per language (the old
  ``TRANSLATIONS.get(text) or WEB_TRANSLATIONS.get(text)`` behaviour is
  baked in at build time: a key present in the "native" source always wins
  over the "web" one for the same key).
* ``locales/native/<lang>.json`` / ``locales/web/<lang>.json`` — the two
  original sources kept separate, for the rare caller that needs one
  specific source regardless of the other (see :func:`translate_native_for`
  / :func:`translate_web_for` — the onboarding wizard uses these to ship
  strings for every language at once instead of just the active one).

Only the active language's file(s) are read, and only the first time
they're needed — see :func:`_load_lang`. English needs no file at all.

Adding / editing translations
-----------------------------
* Edit the ``locales/*.json`` files directly — plain ``{"English source":
  "translated string", ...}`` maps, one file per language, no Python
  required.
* The **key** is the exact English source string that appears in the code.
* The **value** is the translated string for that language. A missing or
  empty entry falls back to the key.
* Keep placeholder tokens (``{}``, ``{name}``, ``%s``, newlines, HTML
  tags …) identical across all languages so ``.format()`` keeps working.
"""

from __future__ import annotations

import json
import os
from typing import Dict

_SUPPORTED_LANGS = ("de", "es", "ko", "pt", "fr", "vi", "zh", "hi")

_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


# --------------------------------------------------------------------- #
# Active language
# --------------------------------------------------------------------- #
# Valid values: "auto", "en", "de", "es", "ko", "pt", "fr", "vi", "zh", "hi".
# Defaults to "auto" until ``on_profile_open()`` overwrites it from the
# user's settings.
USER_LANG: str = "auto"


# --------------------------------------------------------------------- #
# Lazy per-language JSON loading
# --------------------------------------------------------------------- #
# Each dict is populated at most once per language, the first time that
# language is actually requested, and kept for the rest of the process.
_lang_cache: Dict[str, Dict[str, str]] = {}
_native_cache: Dict[str, Dict[str, str]] = {}
_web_cache: Dict[str, Dict[str, str]] = {}


def _read_json(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _load_from(cache: Dict[str, Dict[str, str]], subdir: str, lang: str) -> Dict[str, str]:
    if lang in cache:
        return cache[lang]
    folder = os.path.join(_LOCALES_DIR, subdir) if subdir else _LOCALES_DIR
    data = _read_json(os.path.join(folder, f"{lang}.json"))
    cache[lang] = data
    return data


def _load_lang(lang: str) -> Dict[str, str]:
    """The combined table :func:`_` looks strings up in for `lang`."""
    return _load_from(_lang_cache, "", lang)


# --------------------------------------------------------------------- #
# Language detection helpers
# --------------------------------------------------------------------- #
def _detect_anki_lang() -> str:
    """Best-effort detection of Anki's current UI language.

    Returns one of ``"en"``, ``"de"``, ``"es"``, ``"ko"``, ``"pt"``,
    ``"fr"``, ``"vi"``, ``"zh"``, ``"hi"``. Unknown Anki locales fall
    back to English.
    """
    raw = ""
    # Preferred API (modern Anki versions).
    try:
        from anki.lang import currentLang  # type: ignore
        raw = currentLang or ""
    except Exception:
        pass
    # Fallback API name used on some older Anki versions.
    if not raw:
        try:
            from anki.lang import current_lang  # type: ignore
            raw = current_lang or ""
        except Exception:
            pass
    # Last resort: read directly from the profile manager.
    if not raw:
        try:
            from aqt import mw  # type: ignore
            if mw is not None and getattr(mw, "pm", None) is not None:
                raw = mw.pm.meta.get("defaultLang", "") or ""
        except Exception:
            pass

    raw = (raw or "").lower()
    if raw.startswith("de"):
        return "de"
    if raw.startswith("es"):
        return "es"
    if raw.startswith("ko"):
        return "ko"
    if raw.startswith("pt"):
        return "pt"
    if raw.startswith("fr"):
        return "fr"
    if raw.startswith("vi"):
        return "vi"
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("hi"):
        return "hi"
    return "en"


def _current_lang() -> str:
    """Resolve :data:`USER_LANG` to a concrete language code."""
    lang = USER_LANG
    if lang == "auto":
        return _detect_anki_lang()
    if lang in ("en",) + _SUPPORTED_LANGS:
        return lang
    return "en"


# --------------------------------------------------------------------- #
# Public translation functions
# --------------------------------------------------------------------- #
def _(text: str) -> str:
    """Return the translation of *text* for the current user language.

    If the key is missing or has no entry for the active language, the
    original English ``text`` is returned unchanged as a safe fallback.
    """
    lang = _current_lang()
    if lang == "en":
        return text
    return _load_lang(lang).get(text) or text


def translate_for(text: str, lang: str) -> str:
    """Like :func:`_`, but for an explicitly given language rather than the
    currently active one — for the rare caller that needs several
    languages at once (e.g. the onboarding wizard ships every language's
    strings to its JS side in one shot so the in-page language picker
    doesn't need a round trip back to Python)."""
    if lang == "en":
        return text
    return _load_lang(lang).get(text) or text


def translate_native_for(text: str, lang: str) -> str:
    """Like :func:`translate_for`, but only ever consults the "native" UI
    source (the old ``locales.py`` dict), ignoring the web/HTML one even if
    both happen to define the same key with a different translation."""
    if lang == "en":
        return text
    return _load_from(_native_cache, "native", lang).get(text, text)


def translate_web_for(text: str, lang: str) -> str:
    """Like :func:`translate_for`, but only ever consults the "web" source
    (the old ``web_translations.py`` dict). A few strings (e.g. "Dismiss")
    are deliberately worded differently there than in the native UI, so
    this must not fall back to the native table."""
    if lang == "en":
        return text
    return _load_from(_web_cache, "web", lang).get(text, text)
