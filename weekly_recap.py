# -*- coding: utf-8 -*-
"""
SynapsePro - Weekly Recap
=========================

A once-a-week summary card for the deck browser: reviews done vs. the week
before, the retention change, the best study hour detected, weekly focus
time, and the most-neglected deck with a real backlog.

Nothing here is new data — it's all already sitting in `revlog`/`cards` or
the gamification/pomodoro state — this module only aggregates it into one
place instead of leaving the user to piece it together from several
separate widgets.

The recap for a given week is computed once (right after that week starts,
on whichever deck-browser render happens to trigger it) and cached to disk
under SynapsePro_Data/weekly_recap_state.json; every later render this week
reuses that cached snapshot instead of re-running the SQL. The card stays
visible until the user dismisses it or a new week begins.
"""

from __future__ import annotations

import datetime
import html as _html_mod
import os
from typing import Any, Dict, Optional, Tuple

try:
    from aqt import mw
except ImportError:  # pragma: no cover - Anki not available
    mw = None  # type: ignore

try:
    from .logging_util import get_logger
except ImportError:
    from logging_util import get_logger  # type: ignore
log = get_logger(__name__)

try:
    from .json_state import atomic_write_json, read_json_file
except ImportError:
    from json_state import atomic_write_json, read_json_file  # type: ignore

try:
    from .locales import _
except ImportError:
    def _(text):  # type: ignore
        return text

try:
    from .theme import palette as _palette
except ImportError:
    def _palette(night):  # type: ignore
        return {
            "surface": "#1c1c1e" if night else "#ffffff",
            "grey_light": "#3a3a3c" if night else "#e9ecef",
            "text": "#f5f5f7" if night else "#1D1D1F",
            "text_muted": "#98989d" if night else "#86868B",
            "green": "#34C759", "red": "#FF3B30",
        }

_RECAP_STATE_FILENAME = "weekly_recap_state.json"

# A single week's data is much thinner than the dashboard's all-time best-
# hour sample, so a lower minimum keeps the insight from being empty most
# weeks while still ignoring one-off outliers (e.g. a single 11pm review).
MIN_HOUR_SAMPLE = 5
# A deck needs at least this many due/learnable cards before "you neglected
# it" is a meaningful thing to say — otherwise a deck with 2 due cards you
# genuinely finished would misleadingly show up.
MIN_NEGLECTED_DUE = 5
# Minimum reviews last week before a recap is shown at all — a near-empty
# week (e.g. a new install, or a week off) isn't worth summarizing.
MIN_REVIEWS_FOR_RECAP = 5


def _rollover_hour() -> int:
    try:
        return int(mw.col.get_config("rollover", 4) or 4)
    except Exception:
        return 4


def _anki_today() -> datetime.date:
    return (datetime.datetime.now() - datetime.timedelta(hours=_rollover_hour())).date()


def _iso_week_key(d: datetime.date) -> str:
    year, week, _weekday = d.isocalendar()
    return f"{year}-W{week:02d}"


def _week_bounds(weeks_ago: int) -> Tuple[datetime.date, datetime.date]:
    """[start_date, end_date) for the Mon-Sun week `weeks_ago` weeks before
    the current one (0 = this week, 1 = last week, ...)."""
    today = _anki_today()
    this_week_start = today - datetime.timedelta(days=today.weekday())
    start_date = this_week_start - datetime.timedelta(weeks=weeks_ago)
    return start_date, start_date + datetime.timedelta(weeks=1)


def _ms_bounds(start_date: datetime.date, end_date: datetime.date) -> Tuple[int, int]:
    rollover = _rollover_hour()
    start_dt = datetime.datetime.combine(start_date, datetime.time()) + datetime.timedelta(hours=rollover)
    end_dt = datetime.datetime.combine(end_date, datetime.time()) + datetime.timedelta(hours=rollover)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _state_path() -> Optional[str]:
    try:
        root = mw.pm.profileFolder() if mw and mw.pm else None
        if not root:
            return None
        folder = os.path.join(root, "SynapsePro_Data")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, _RECAP_STATE_FILENAME)
    except Exception:
        return None


def _load_state() -> Dict[str, Any]:
    value = read_json_file(_state_path())
    return value if isinstance(value, dict) else {}


def _save_state(state: Dict[str, Any]) -> None:
    if not atomic_write_json(_state_path(), state, indent=2, ensure_ascii=False):
        log.error("Weekly recap: could not persist state")


def _reviews_and_retention(start_ms: int, end_ms: int) -> Tuple[int, float]:
    if not mw or not mw.col:
        return 0, 0.0
    try:
        row = mw.col.db.first(
            "SELECT COUNT(*), SUM(CASE WHEN ease > 1 THEN 1 ELSE 0 END) "
            "FROM revlog WHERE id >= ? AND id < ? AND ease > 0",
            start_ms, end_ms,
        )
    except Exception as exc:
        log.error(f"Weekly recap: reviews/retention query failed: {exc}")
        return 0, 0.0
    total, correct = row or (0, 0)
    total = total or 0
    correct = correct or 0
    retention = (correct / total * 100.0) if total else 0.0
    return total, retention


def _best_hour(start_ms: int, end_ms: int) -> Optional[int]:
    if not mw or not mw.col:
        return None
    try:
        rows = mw.col.db.all(
            "SELECT strftime('%H', id/1000, 'unixepoch', 'localtime') AS hr, COUNT(*) "
            "FROM revlog WHERE id >= ? AND id < ? AND ease > 0 GROUP BY hr",
            start_ms, end_ms,
        )
    except Exception as exc:
        log.error(f"Weekly recap: best-hour query failed: {exc}")
        return None
    best_hour, best_count = None, 0
    for hr_str, count in rows:
        if count and count > best_count:
            best_hour, best_count = int(hr_str), count
    if best_hour is None or best_count < MIN_HOUR_SAMPLE:
        return None
    return best_hour


def _most_neglected_deck(start_ms: int, end_ms: int) -> Optional[str]:
    """The concrete deck with the largest ignored backlog: it has a real
    number of due/learnable cards but few or no reviews last week."""
    if not mw or not mw.col:
        return None
    try:
        decks = mw.col.decks.all_names_and_ids()
    except Exception as exc:
        log.error(f"Weekly recap: deck listing failed: {exc}")
        return None

    worst_name, worst_due, worst_reviewed = None, 0, None
    for entry in decks:
        name = getattr(entry, "name", None)
        did = getattr(entry, "id", None)
        if not name or did is None or name == "Default":
            continue
        try:
            due_count = mw.col.db.scalar(
                "SELECT COUNT(*) FROM cards WHERE did = ? AND queue IN (1, 2, 3)", did) or 0
            if due_count < MIN_NEGLECTED_DUE:
                continue
            reviewed = mw.col.db.scalar(
                "SELECT COUNT(*) FROM revlog WHERE id >= ? AND id < ? AND ease > 0 "
                "AND cid IN (SELECT id FROM cards WHERE did = ?)",
                start_ms, end_ms, did) or 0
        except Exception as exc:
            log.error(f"Weekly recap: neglected-deck query failed for deck {did}: {exc}")
            continue
        # "Neglected" means the backlog dwarfs what was actually reviewed —
        # a deck with 6 due cards and 4 reviews isn't neglected, one with 40
        # due cards and 2 reviews is.
        if reviewed >= max(3, due_count // 10):
            continue
        if worst_name is None or reviewed < worst_reviewed or (
            reviewed == worst_reviewed and due_count > worst_due
        ):
            worst_name, worst_due, worst_reviewed = name, due_count, reviewed
    return worst_name


def _focus_minutes(start_date: datetime.date, end_date: datetime.date) -> int:
    try:
        from . import pomodoro
    except ImportError:
        import pomodoro  # type: ignore
    try:
        stats = pomodoro.load_pomodoro_stats()
    except Exception as exc:
        log.error(f"Weekly recap: could not load pomodoro stats: {exc}")
        return 0
    daily = stats.get("daily_data", {})
    if not isinstance(daily, dict):
        return 0
    total = 0
    day = start_date
    while day < end_date:
        entry = daily.get(day.isoformat())
        if isinstance(entry, dict):
            try:
                total += int(entry.get("focus_minutes", 0) or 0)
            except (TypeError, ValueError):
                pass
        day += datetime.timedelta(days=1)
    return total


def compute_recap() -> Optional[Dict[str, Any]]:
    """Build the recap payload for the week that just ended (the last
    completed Mon-Sun week), or None if there isn't enough data to make one
    worthwhile yet."""
    if not mw or not mw.col:
        return None
    last_start_date, last_end_date = _week_bounds(1)
    prev_start_date, prev_end_date = _week_bounds(2)
    last_start_ms, last_end_ms = _ms_bounds(last_start_date, last_end_date)
    prev_start_ms, prev_end_ms = _ms_bounds(prev_start_date, prev_end_date)

    reviews, retention = _reviews_and_retention(last_start_ms, last_end_ms)
    if reviews < MIN_REVIEWS_FOR_RECAP:
        return None

    prev_reviews, prev_retention = _reviews_and_retention(prev_start_ms, prev_end_ms)

    return {
        "reviews": reviews,
        "prev_reviews": prev_reviews,
        "retention": retention,
        "prev_retention": prev_retention if prev_reviews > 0 else None,
        "best_hour": _best_hour(last_start_ms, last_end_ms),
        "neglected_deck": _most_neglected_deck(last_start_ms, last_end_ms),
        "focus_minutes": _focus_minutes(last_start_date, last_end_date),
    }


def get_pending_recap() -> Optional[Dict[str, Any]]:
    """Returns the recap payload to render this week, or None if the user
    already dismissed it for this week or there isn't enough data. Computes
    at most once per week — later calls this week reuse the cached result."""
    state = _load_state()
    current_week_key = _iso_week_key(_anki_today())
    if state.get("dismissed_week") == current_week_key:
        return None

    cached = state.get("cached_data")
    if isinstance(cached, dict) and state.get("cached_for_week") == current_week_key:
        data = dict(cached)
    else:
        data = compute_recap()
        if data is None:
            return None
        state["cached_data"] = data
        state["cached_for_week"] = current_week_key
        _save_state(state)

    data["_current_week_key"] = current_week_key
    return data


def dismiss_recap(week_key: str) -> None:
    """Hide the recap card for the given week (the week it's currently
    showing for — see the `_current_week_key` field of get_pending_recap()'s
    result). A new week's card is unaffected."""
    if not week_key:
        return
    state = _load_state()
    state["dismissed_week"] = week_key
    _save_state(state)


def _format_delta(current: float, previous: Optional[float], pal: Dict[str, str],
                   suffix: str = "", is_percent_points: bool = False) -> str:
    if previous is None:
        return ""
    diff = current - previous
    threshold = 0.5 if is_percent_points else 1
    if abs(diff) < threshold:
        return ""
    arrow = "▲" if diff > 0 else "▼"
    color = pal["green"] if diff > 0 else pal["red"]
    sign = "+" if diff > 0 else ""
    return (f'<span style="color:{color}; font-size:0.78em; font-weight:600; '
            f'margin-left:6px;">{arrow} {sign}{diff:.0f}{suffix}</span>')


def render_recap_card_html(data: Dict[str, Any]) -> str:
    """Render the dismissible weekly recap card. `data` is whatever
    get_pending_recap() returned."""
    night = False
    try:
        night = bool(mw and mw.pm.night_mode())
    except Exception:
        pass
    pal = _palette(night)

    reviews = data.get("reviews", 0)
    prev_reviews = data.get("prev_reviews")
    retention = data.get("retention", 0.0)
    prev_retention = data.get("prev_retention")
    best_hour = data.get("best_hour")
    neglected_deck = data.get("neglected_deck")
    focus_minutes = data.get("focus_minutes", 0)
    week_key = data.get("_current_week_key", "")

    reviews_delta = _format_delta(reviews, prev_reviews, pal)
    retention_delta = _format_delta(retention, prev_retention, pal, "pp", is_percent_points=True)

    if best_hour is not None:
        best_hour_text = f"{best_hour:02d}:00–{(best_hour + 1) % 24:02d}:00"
    else:
        best_hour_text = _("Not enough data")

    rows = [
        (_("Reviews"), f"{reviews}{reviews_delta}"),
        (_("Retention"), f"{retention:.0f}%{retention_delta}"),
        (_("Best study time"), best_hour_text),
    ]
    if focus_minutes > 0:
        hrs, mins = divmod(int(focus_minutes), 60)
        time_text = f"{hrs}h {mins:02d}m" if hrs else f"{mins}m"
        rows.append((_("Focus time"), time_text))
    if neglected_deck:
        rows.append((_("Needs attention"), _html_mod.escape(neglected_deck)))

    row_html = "".join(
        f'<div style="display:flex; justify-content:space-between; align-items:baseline; '
        f'padding:7px 0; border-bottom:1px solid var(--sp-recap-border);">'
        f'<span style="color:var(--sp-recap-muted); font-size:0.85em;">{label}</span>'
        f'<span style="font-weight:600; color:var(--sp-recap-text); font-size:0.95em; '
        f'white-space:nowrap;">{value}</span>'
        f'</div>'
        for label, value in rows
    )

    title = _("Your Week in Review")
    dismiss_tip = _("Dismiss")

    return f'''
    <style>
      :root {{
        --sp-recap-surface: {pal["surface"]};
        --sp-recap-border: {pal["grey_light"]};
        --sp-recap-text: {pal["text"]};
        --sp-recap-muted: {pal["text_muted"]};
      }}
    </style>
    <div class="gamewidget sp-weekly-recap" style="max-width:860px; width:100%; margin:0 auto 15px auto;
                background:var(--sp-recap-surface); border:1px solid var(--sp-recap-border); border-radius:12px;
                padding:16px; box-sizing:border-box; position:relative;">
        <span title="{dismiss_tip}"
              onclick="event.stopPropagation(); pycmd('pycmd:synapsepro:weekly_recap_dismiss:{week_key}')"
              style="position:absolute; top:12px; right:14px; cursor:pointer; opacity:0.5; font-size:14px;
                     line-height:1; user-select:none; color:var(--sp-recap-text);"
              onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=0.5">✕</span>
        <h5 style="font-size:1em; font-weight:bold; color:var(--sp-recap-text); margin:0 0 10px 0;
                   padding-right:22px;">{title}</h5>
        {row_html}
    </div>
    '''
