# -*- coding: utf-8 -*-
"""
SynapsePro - Central Logging
=============================

Every module used to swallow errors with ``except Exception: print(...)``,
which means bugs are only visible if the user happens to have Anki's
terminal open. This module gives the whole add-on one logger that writes to
a rotating file inside the user's profile, plus a helper the Help dialog
uses to let users attach real diagnostics to bug reports instead of
whatever they can screenshot from a console window.

Usage in any module::

    from .logging_util import get_logger
    log = get_logger(__name__)
    ...
    try:
        risky()
    except Exception:
        log.exception("failed to do X")   # records the full traceback

``log.exception`` and `log.error`/`log.warning` are all safe to call even
before :func:`init_logging` has run (they fall back to a null handler so
nothing raises), so import order does not matter.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import Optional

_LOGGER_NAME = "synapsepro"
_MAX_BYTES = 1_000_000  # ~1 MB per file
_BACKUP_COUNT = 2

_log_file_path: Optional[str] = None
_initialized = False


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return a logger under the ``synapsepro`` hierarchy.

    Pass ``__name__`` from the calling module so log lines show which file
    produced them (``synapsepro.gamification``, ``synapsepro.ai_assistant``,
    ...). Safe to call at import time, before :func:`init_logging`.
    """
    if name and name != _LOGGER_NAME and not name.startswith(f"{_LOGGER_NAME}."):
        # Turn "SynapsePro1.gamification" / "gamification" into a clean
        # "synapsepro.gamification" child logger regardless of how the
        # caller's __name__ looks (package name varies with the add-on
        # folder name on disk).
        short = name.rsplit(".", 1)[-1]
        name = f"{_LOGGER_NAME}.{short}"
    return logging.getLogger(name)


def init_logging(profile_folder: Optional[str]) -> Optional[str]:
    """Attach a rotating file handler to the ``synapsepro`` logger tree.

    Called once from ``on_profile_open`` with ``mw.pm.profileFolder()``.
    Re-entrant: switching profiles calls this again and it just re-points
    the handler at the new profile's log file. Returns the log file path,
    or ``None`` if it could not be set up (falls back to console-only via
    Python's default "no handlers" warning, which is harmless).
    """
    global _log_file_path, _initialized

    root = get_logger()
    root.setLevel(logging.DEBUG)

    # Remove any handler from a previous profile so logs don't keep being
    # written to a folder the user just switched away from.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    if not profile_folder:
        root.addHandler(logging.NullHandler())
        _initialized = True
        return None

    try:
        folder = os.path.join(profile_folder, "SynapsePro_Data")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "synapsepro.log")
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
        _log_file_path = path
        _initialized = True
        return path
    except Exception:
        # Logging must never be the thing that breaks the add-on.
        root.addHandler(logging.NullHandler())
        _initialized = True
        return None


def get_log_file_path() -> Optional[str]:
    """Path to the current log file, or ``None`` if logging isn't set up yet."""
    return _log_file_path


def read_log_tail(max_chars: int = 200_000) -> str:
    """Return the tail of the current log file as text, for the diagnostics
    dialog. Empty string if there is nothing to show yet."""
    path = _log_file_path
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
        if len(data) > max_chars:
            data = "...(truncated)...\n" + data[-max_chars:]
        return data
    except Exception:
        return ""
