# -*- coding: utf-8 -*-
"""
SynapsePro - Shared JSON State Persistence
===========================================

Every panel that keeps its own small state file (the website sidebar's
``website_state.json``, the mind map's recovery snapshot, the notebook's
notes/todos/PDF-list recovery snapshots, the AI assistant's
``ai_secrets.json``, ...) used to hand-roll the same
"write to a ``.tmp`` file, then ``os.replace()`` it into place, cleaning up
the ``.tmp`` file on failure" dance, each with slightly different
formatting flags and no shared name for the pattern.

This module gives all of them one implementation to call instead, so a fix
to the atomic-write logic (or e.g. adding a integrity check later) lands
everywhere at once instead of needing to be repeated per file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


def read_json_file(path: Optional[str], default: Any = None) -> Any:
    """Read and parse `path` as JSON, returning `default` if it doesn't
    exist or fails to parse. Never raises."""
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def atomic_write_json(
    path: Optional[str],
    data: Any,
    *,
    indent: Optional[int] = None,
    ensure_ascii: bool = True,
    separators: Optional[tuple] = None,
    fsync: bool = False,
    chmod: Optional[int] = None,
) -> bool:
    """Write `data` as JSON to `path` without ever leaving a half-written
    file behind: it's written to ``<path>.tmp`` first and only becomes
    `path` via ``os.replace()``, which is atomic on every platform Anki
    supports. On any failure the ``.tmp`` file is removed and `path` is
    left exactly as it was.

    The formatting knobs (`indent`, `ensure_ascii`, `separators`) default
    to Python's own `json.dump` defaults so switching a call site to this
    helper is a drop-in replacement — pass the same arguments the old
    inline `json.dump(...)` call used.

    `fsync=True` also flushes and fsyncs the ``.tmp`` file before the
    rename, for state a crash must not be allowed to lose even the
    in-flight write (used by the mind map / notebook recovery snapshots).
    `chmod` (e.g. ``0o600``) restricts the ``.tmp`` file's permissions
    before it becomes `path` — used for secrets such as AI provider keys.

    Returns True on success, False on failure.
    """
    if not path:
        return False
    tmp_path = f"{path}.tmp"
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, ensure_ascii=ensure_ascii,
                       separators=separators)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        if chmod is not None:
            try:
                os.chmod(tmp_path, chmod)
            except OSError:
                pass
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False
