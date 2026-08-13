# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import time
import json
import html as _html_mod
from aqt import mw

# --- Night Mode Detection ---
is_night_mode = False
try:
    if mw and mw.pm.night_mode():
        is_night_mode = True
except Exception:
    pass

try:
    from .theme import palette as _palette, FONT_FAMILY as _FONT_FAMILY
except ImportError:
    def _palette(night): return {"blue": "#0071D3", "blue_bright": "#40A5FF" if night else "#007AFF"}  # type: ignore
    _FONT_FAMILY = "sans-serif"

try:
    from .locales import _
except ImportError:
    def _(text):  # type: ignore
        return text

WIDGET_MAX_WIDTH = "860px"

# DeckBrowser.refresh() can be requested more than once while Anki is settling
# its startup layout.  Keep the expensive, read-only collection snapshot for a
# very short window so those duplicate renders do not repeat the SQL snapshot.
# One second is deliberately short: it coalesces a startup burst without making
# normal review/deck changes appear stale to the user.
_STATISTICS_CACHE_TTL_SECONDS = 1.0
_statistics_cache = {}


def invalidate_statistics_cache() -> None:
    """Drop all cached dashboard snapshots (profile/review boundary)."""
    _statistics_cache.clear()

# ── Review activity helpers ───────────────────────────────────────────────────
def _get_rollover_hour() -> int:
    """Anki's day-rollover hour (default 4 a.m.)."""
    try:
        return int(mw.col.get_config("rollover", 4) or 4)
    except Exception:
        return 4


def anki_today():
    """Today as an 'Anki day' (a day runs from rollover to rollover)."""
    return (datetime.now() - timedelta(hours=_get_rollover_hour())).date()


def get_deck_and_children_ids(deck_id):
    """All deck ids in the subtree rooted at deck_id (deck_id included)."""
    if not mw or not mw.col or not deck_id:
        return []
    try:
        return list(mw.col.decks.deck_and_child_ids(deck_id))
    except Exception:
        try:
            deck = mw.col.decks.get(deck_id)
            name = deck["name"] if deck else None
            ids = [deck_id]
            if name:
                prefix = name + "::"
                for d in mw.col.decks.all_names_and_ids():
                    if d.name.startswith(prefix):
                        ids.append(d.id)
            return ids
        except Exception:
            return [deck_id]


def get_deck_filter_options():
    """[(id, display_name), ...] for the stats deck-filter dropdown, sorted
    by name with subdeck depth shown via a "→" separator."""
    if not mw or not mw.col:
        return []
    try:
        return sorted(
            ((d.id, d.name.replace("::", " → ")) for d in mw.col.decks.all_names_and_ids()),
            key=lambda t: t[1].lower(),
        )
    except Exception:
        return []


def get_daily_review_counts(since_days=None, deck_ids=None):
    """Reviews per day, keyed by ISO date string, respecting the rollover hour.

    since_days=None returns the whole collection history; an int restricts
    the query for cheap dashboard renders. deck_ids, if given, restricts to
    reviews on cards belonging to those decks.
    """
    if not mw or not mw.col:
        return {}
    offset = _get_rollover_hour() * 3600
    deck_sql = ""
    deck_params = []
    if deck_ids:
        placeholders = ",".join("?" * len(deck_ids))
        deck_sql = f" AND cid IN (SELECT id FROM cards WHERE did IN ({placeholders}))"
        deck_params = list(deck_ids)
    try:
        if since_days is None:
            rows = mw.col.db.all(
                "SELECT strftime('%Y-%m-%d', id/1000 - ?, 'unixepoch', 'localtime') AS d, "
                f"COUNT(*) FROM revlog WHERE 1=1{deck_sql} GROUP BY d",
                offset, *deck_params,
            )
        else:
            start = datetime.now() - timedelta(days=int(since_days))
            rows = mw.col.db.all(
                "SELECT strftime('%Y-%m-%d', id/1000 - ?, 'unixepoch', 'localtime') AS d, "
                f"COUNT(*) FROM revlog WHERE id >= ?{deck_sql} GROUP BY d",
                offset,
                int(start.timestamp()) * 1000,
                *deck_params,
            )
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        print(f"SynapsePro: review activity query failed: {e}")
        return {}


def get_daily_study_minutes(since_days=None, deck_ids=None):
    """Minutes studied per day, keyed by ISO date string (per-card time
    capped at 45s, matching the rest of the addon's time accounting)."""
    if not mw or not mw.col:
        return {}
    offset = _get_rollover_hour() * 3600
    deck_sql = ""
    deck_params = []
    if deck_ids:
        placeholders = ",".join("?" * len(deck_ids))
        deck_sql = f" AND cid IN (SELECT id FROM cards WHERE did IN ({placeholders}))"
        deck_params = list(deck_ids)
    try:
        start = datetime.now() - timedelta(days=int(since_days)) if since_days else None
        where_id = " AND id >= ?" if start is not None else ""
        params = [offset]
        if start is not None:
            params.append(int(start.timestamp()) * 1000)
        params.extend(deck_params)
        rows = mw.col.db.all(
            "SELECT strftime('%Y-%m-%d', id/1000 - ?, 'unixepoch', 'localtime') AS d, "
            "SUM(CASE WHEN time > 45000 THEN 45000 WHEN time < 0 THEN 0 ELSE time END) "
            f"FROM revlog WHERE ease > 0{where_id}{deck_sql} GROUP BY d",
            *params,
        )
        return {r[0]: (r[1] or 0) / 60000.0 for r in rows}
    except Exception as e:
        print(f"SynapsePro: study-time-per-day query failed: {e}")
        return {}


CHART_DAYS = 30  # days shown in the mini activity sparkline


def _build_mini_chart(deck_ids=None):
    """Inline-SVG sparkline: reviews per day, last CHART_DAYS days.

    The y-axis is relative to the window: the best day in the window is 100%,
    a day with no reviews sits on the baseline. Includes x-axis tick labels
    relative to today (e.g. "-30", "-10", "Today") and a custom follow-cursor
    tooltip (invisible full-height hover rects as hit targets, hit target ≫
    mark) showing the exact date, review count and minutes studied for the
    hovered day.

    Returns (html, avg_daily_reviews) — the average is the mean review count
    over the CHART_DAYS window, reused by the "Daily Average" stat block so
    it doesn't need a second pass over the same data.
    """
    today = anki_today()
    counts = get_daily_review_counts(since_days=CHART_DAYS + 2, deck_ids=deck_ids)
    minutes = get_daily_study_minutes(since_days=CHART_DAYS + 2, deck_ids=deck_ids)
    start = today - timedelta(days=CHART_DAYS - 1)
    days, values = [], []
    for i in range(CHART_DAYS):
        d = start + timedelta(days=i)
        days.append(d)
        values.append(counts.get(d.isoformat(), 0))
    vmax = max(values) or 1

    W, H, PAD = 300.0, 62.0, 4.0
    PAD_R = 5.0  # right inset so the "today" dot isn't clipped at the edge
    step = (W - PAD_R) / (CHART_DAYS - 1)
    y_lo, y_hi = H - PAD, PAD  # baseline / peak
    pts = [(i * step, y_lo - (v / vmax) * (y_lo - y_hi)) for i, v in enumerate(values)]

    def _clamp_y(y):
        return max(y_hi, min(y_lo, y))

    def _smooth_path(p):
        """Catmull-Rom → cubic Bézier, control-y clamped so zero-runs never
        dip below the baseline."""
        if len(p) < 3:
            return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in p)
        d = f"M{p[0][0]:.1f},{p[0][1]:.1f}"
        for i in range(len(p) - 1):
            p0 = p[i - 1] if i > 0 else p[i]
            p1, p2 = p[i], p[i + 1]
            p3 = p[i + 2] if i + 2 < len(p) else p2
            c1y = _clamp_y(p1[1] + (p2[1] - p0[1]) / 6.0)
            c2y = _clamp_y(p2[1] - (p3[1] - p1[1]) / 6.0)
            d += (f" C{p1[0] + (p2[0] - p0[0]) / 6.0:.1f},{c1y:.1f}"
                  f" {p2[0] - (p3[0] - p1[0]) / 6.0:.1f},{c2y:.1f}"
                  f" {p2[0]:.1f},{p2[1]:.1f}")
        return d

    line = _smooth_path(pts)
    # Close the area on the baseline (the x-axis), ending at the last point.
    area = line + f" L{pts[-1][0]:.1f},{y_lo:.1f} L0,{y_lo:.1f} Z"
    # Subtle x/y axes as hairlines in the neutral grey.
    axes = (
        f'<line x1="0.5" y1="{y_hi:.1f}" x2="0.5" y2="{y_lo:.1f}" '
        f'stroke="var(--gray)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        f'<line x1="0" y1="{y_lo:.1f}" x2="{W:.1f}" y2="{y_lo:.1f}" '
        f'stroke="var(--gray)" stroke-width="1" vector-effect="non-scaling-stroke"/>'
    )

    # Invisible full-height hit-target rects; the exact value is shown in a
    # custom follow-cursor tooltip (built below) rather than a native <title>,
    # so it can be positioned precisely above the hovered point.
    hover = []
    points_js = []
    for i, (d, v) in enumerate(zip(days, values)):
        hover.append(
            f'<rect class="spark-hit" data-i="{i}" x="{i * step - step / 2:.1f}" '
            f'y="0" width="{step:.1f}" height="{H:.0f}" fill="transparent"/>'
        )
        label = d.strftime("%d.%m.%Y")
        mins = round(minutes.get(d.isoformat(), 0.0), 1)
        points_js.append(
            f'{{"x":{pts[i][0]:.1f},"y":{pts[i][1]:.1f},"label":"{label}","value":{v},"minutes":{mins}}}'
        )

    last = pts[-1]
    svg = (
        f'<svg class="spark" viewBox="0 0 {W:.0f} {H:.0f}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'{axes}'
        f'<path d="{area}" fill="var(--main-blue)" fill-opacity="0.12" stroke="none"/>'
        f'<path d="{line}" fill="none" stroke="var(--main-blue)" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="3" fill="var(--main-blue)"/>'
        f'{"".join(hover)}'
        f'</svg>'
    )

    # 4 x-axis tick labels relative to today, e.g. "-30", "-20", "-10", "Today".
    label_today = _("Today")
    offsets = (CHART_DAYS, (CHART_DAYS * 2) // 3, CHART_DAYS // 3, 0)
    axis_labels_html = "".join(
        f'<span>{("-" + str(o)) if o else label_today}</span>' for o in offsets
    )
    axis_html = f'<div class="spark-axis-labels">{axis_labels_html}</div>'

    tooltip_html = '<div class="spark-tooltip"></div>'
    tooltip_detail_tpl = _("{} cards • {} min")

    script = f"""
    <script>
    (function() {{
        var pts = [{','.join(points_js)}];
        var detailTpl = {json.dumps(tooltip_detail_tpl)};
        var root = document.currentScript.parentElement;
        var svgEl = root.querySelector('svg.spark');
        var tip = root.querySelector('.spark-tooltip');
        if (!svgEl || !tip) return;

        function showTip(i) {{
            var p = pts[i];
            if (!p) return;
            var rect = svgEl.getBoundingClientRect();
            var rootRect = root.getBoundingClientRect();
            var left = (rect.left - rootRect.left) + (p.x / {W:.1f}) * rect.width;
            var top = (rect.top - rootRect.top) + (p.y / {H:.1f}) * rect.height;
            var detail = detailTpl.replace('{{}}', p.value).replace('{{}}', p.minutes);
            tip.textContent = p.label + ': ' + detail;
            tip.style.left = left + 'px';
            tip.style.top = top + 'px';
            tip.style.opacity = '1';
        }}
        function hideTip() {{ tip.style.opacity = '0'; }}

        var hits = root.querySelectorAll('.spark-hit');
        for (var i = 0; i < hits.length; i++) {{
            hits[i].addEventListener('mousemove', (function(idx) {{
                return function() {{ showTip(idx); }};
            }})(i));
        }}
        root.addEventListener('mouseleave', hideTip);
    }})();
    </script>
    """

    avg_daily = (sum(values) / CHART_DAYS) if CHART_DAYS else 0.0
    return svg + axis_html + tooltip_html + script, avg_daily


def _revlog_period_totals(start_ts, end_ts=None, deck_ids=None):
    """Aggregate revlog totals over a (start_ts, end_ts] ms window (end_ts=None
    means "up to now"), optionally restricted to deck_ids (subtree)."""
    id_clause = "id > ?"
    params = [start_ts]
    if end_ts is not None:
        id_clause += " AND id <= ?"
        params.append(end_ts)
    deck_sql = ""
    if deck_ids:
        placeholders = ",".join("?" * len(deck_ids))
        deck_sql = f" AND cid IN (SELECT id FROM cards WHERE did IN ({placeholders}))"
        params.extend(deck_ids)
    row = mw.col.db.first(
        f"""
        SELECT
            SUM(CASE WHEN type = 1 AND ease > 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN type = 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN ease > 1 THEN 1 ELSE 0 END),
            COUNT(*),
            SUM(CASE WHEN time > 45000 THEN 45000 ELSE time END)
        FROM revlog
        WHERE {id_clause} AND ease > 0{deck_sql}
        """,
        *params,
    )
    correct_reviews, total_reviews, correct_cards, total_cards, total_ms = row or (0, 0, 0, 0, 0)
    return (correct_reviews or 0, total_reviews or 0, correct_cards or 0,
            total_cards or 0, total_ms or 0)


def _delta_points(current, previous):
    """Percentage-POINT change (for values that are already percentages)."""
    if previous is None:
        return None
    return current - previous


def _delta_percent(current, previous):
    """Relative percentage change (for raw counts/rates, not percentages)."""
    if not previous:
        return None
    return ((current - previous) / previous) * 100.0


def get_statistics_data(stats_days=7, deck_id=None):
    """
    Sammelt und berechnet alle Statistiken.
    Erhält den Zeitraum (stats_days) als Argument von __init__.py.
    deck_id, if given, restricts every metric to that deck's subtree.
    """

    if not isinstance(stats_days, int) or stats_days < 1:
        stats_days = 7

    cache_key = (stats_days, deck_id)
    now = time.monotonic()
    cached = _statistics_cache.get(cache_key)
    if cached and now - cached[0] <= _STATISTICS_CACHE_TTL_SECONDS:
        return cached[1]

    deck_ids = get_deck_and_children_ids(deck_id) if deck_id else None

    chart_html, avg_daily_reviews = _build_mini_chart(deck_ids=deck_ids)

    stats_start_ts = int((datetime.now() - timedelta(days=stats_days)).timestamp()) * 1000
    prev_start_ts = int((datetime.now() - timedelta(days=stats_days * 2)).timestamp()) * 1000

    (correct_reviews, total_reviews, correct_cards,
     total_cards, total_ms) = _revlog_period_totals(stats_start_ts, deck_ids=deck_ids)
    (prev_correct_reviews, prev_total_reviews, prev_correct_cards,
     prev_total_cards, prev_total_ms) = _revlog_period_totals(
        prev_start_ts, end_ts=stats_start_ts, deck_ids=deck_ids)

    retention_percent = (correct_reviews / total_reviews * 100) if total_reviews > 0 else 0
    prev_retention_percent = (
        prev_correct_reviews / prev_total_reviews * 100 if prev_total_reviews > 0 else None)

    total_minutes = (total_ms / 1000.0) / 60.0

    efficiency_score = (correct_cards / total_minutes) if total_minutes > 0 else 0
    accuracy_percent = (correct_cards / total_cards * 100) if total_cards > 0 else 0

    prev_total_minutes = (prev_total_ms / 1000.0) / 60.0
    prev_efficiency_score = (
        prev_correct_cards / prev_total_minutes if prev_total_minutes > 0 else None)
    prev_accuracy_percent = (
        prev_correct_cards / prev_total_cards * 100 if prev_total_cards > 0 else None)

    TARGET_CARDS_PER_MIN = 7.5
    efficiency_bar_percent_raw = (efficiency_score / TARGET_CARDS_PER_MIN) * 100

    card_counts_sql = (
        "SELECT SUM(CASE WHEN queue != -1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN queue = 0 THEN 1 ELSE 0 END) FROM cards"
    )
    card_counts_params = []
    if deck_ids:
        placeholders = ",".join("?" * len(deck_ids))
        card_counts_sql += f" WHERE did IN ({placeholders})"
        card_counts_params = list(deck_ids)
    card_counts = mw.col.db.first(card_counts_sql, *card_counts_params)
    total_active_cards, total_new_cards = card_counts or (0, 0)
    total_active_cards = total_active_cards or 0
    total_new_cards = total_new_cards or 0

    new_cards_percent = (total_new_cards / total_active_cards * 100) if total_active_cards > 0 else 0
    studied_percent = 100 - new_cards_percent

    # Previous 30-day average, independent of stats_days (the chart/"Daily
    # Average" block always looks at a fixed 30-day window).
    deck_sql = ""
    deck_params = []
    if deck_ids:
        placeholders = ",".join("?" * len(deck_ids))
        deck_sql = f" AND cid IN (SELECT id FROM cards WHERE did IN ({placeholders}))"
        deck_params = list(deck_ids)
    prev_chart_start = int((datetime.now() - timedelta(days=CHART_DAYS * 2)).timestamp()) * 1000
    prev_chart_end = int((datetime.now() - timedelta(days=CHART_DAYS)).timestamp()) * 1000
    prev_chart_reviews = mw.col.db.scalar(
        f"SELECT COUNT(*) FROM revlog WHERE id > ? AND id <= ? AND ease > 0{deck_sql}",
        prev_chart_start, prev_chart_end, *deck_params) or 0
    prev_avg_daily_reviews = (prev_chart_reviews / CHART_DAYS) if CHART_DAYS else None

    result = {
        "chart_html": chart_html,
        "avg_daily_reviews": avg_daily_reviews,
        "avg_daily_delta_pct": _delta_percent(avg_daily_reviews, prev_avg_daily_reviews),
        "efficiency_raw": efficiency_bar_percent_raw,
        "efficiency_cards_per_min": efficiency_score,
        "efficiency_delta_pct": _delta_percent(efficiency_score, prev_efficiency_score),
        "accuracy_raw": accuracy_percent,
        "accuracy_delta_pp": _delta_points(accuracy_percent, prev_accuracy_percent),
        "retention_percent": retention_percent,
        "retention_delta_pp": _delta_points(retention_percent, prev_retention_percent),
        "new_cards_percent": new_cards_percent,
        "studied_percent": studied_percent,
        "days_scope": stats_days,
        "deck_id": deck_id,
    }
    _statistics_cache[cache_key] = (time.monotonic(), result)
    return result


def get_minimal_statistics_data(stats_days=7):
    """Return only the metrics used by the minimal dashboard.

    Efficiency and accuracy remain excluded; the requested New Cards share
    is fetched with one small aggregate query.
    """
    if not isinstance(stats_days, int) or stats_days < 1:
        stats_days = 7

    cache_key = ("minimal", stats_days)
    now = time.monotonic()
    cached = _statistics_cache.get(cache_key)
    if cached and now - cached[0] <= _STATISTICS_CACHE_TTL_SECONDS:
        return cached[1]

    chart_html, _avg_daily_reviews = _build_mini_chart()
    stats_start_ts = int(
        (datetime.now() - timedelta(days=stats_days)).timestamp()
    ) * 1000
    retention_row = mw.col.db.first(
        "SELECT "
        "SUM(CASE WHEN type = 1 AND ease > 1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN type = 1 THEN 1 ELSE 0 END) "
        "FROM revlog WHERE id > ? AND ease > 0",
        stats_start_ts,
    )
    correct_reviews, total_reviews = retention_row or (0, 0)
    correct_reviews = correct_reviews or 0
    total_reviews = total_reviews or 0
    retention_percent = (
        (correct_reviews / total_reviews) * 100.0 if total_reviews > 0 else 0.0
    )
    card_counts = mw.col.db.first(
        "SELECT "
        "SUM(CASE WHEN queue != -1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN queue = 0 THEN 1 ELSE 0 END) FROM cards"
    )
    total_active_cards, total_new_cards = card_counts or (0, 0)
    total_active_cards = total_active_cards or 0
    total_new_cards = total_new_cards or 0
    new_cards_percent = (
        (total_new_cards / total_active_cards) * 100.0
        if total_active_cards > 0 else 0.0
    )
    result = {
        "chart_html": chart_html,
        "retention_percent": retention_percent,
        "new_cards_percent": new_cards_percent,
        "days_scope": stats_days,
    }
    _statistics_cache[cache_key] = (time.monotonic(), result)
    return result

def _format_delta_badge(value, suffix, tooltip=""):
    """Small ▲/▼ badge for a period-over-period change. `suffix` is appended
    verbatim after the number (e.g. "pp" for percentage points, "%" for a
    relative change). Returns "" when there's no prior-period data or the
    change is negligible, so the badge never shows misleading noise."""
    if value is None or abs(value) < 0.5:
        return ""
    arrow = "▲" if value > 0 else "▼"
    cls = "stat-delta-up" if value > 0 else "stat-delta-down"
    sign = "+" if value > 0 else ""
    tip_attr = f' title="{tooltip}"' if tooltip else ""
    return f'<span class="stat-delta {cls}"{tip_attr}>{arrow} {sign}{value:.0f}{suffix}</span>'


def render_widget_html_internal(stats):
    """Interne Funktion zum Bauen des HTMLs."""
    retention_val = stats['retention_percent']
    new_cards_val = stats['new_cards_percent']
    days_scope = stats['days_scope']
    selected_deck_id = stats.get('deck_id')

    if days_scope == 1:
        period_text = _("Last 24 hours")
    else:
        period_text = _("Last {} days").format(days_scope)

    acc_real = stats['accuracy_raw']
    if acc_real >= 90:
        acc_visual = 100
    elif acc_real >= 80:
        acc_visual = 85 + ((acc_real - 80) * 1.5)
    elif acc_real >= 70:
        acc_visual = 50 + ((acc_real - 70) * 3.5)
    else:
        acc_visual = max(10, acc_real / 1.5)
    acc_visual = min(100, acc_visual)

    eff_raw = stats['efficiency_raw']
    eff_visual = min(100, eff_raw)
    eff_cards_per_min = stats.get('efficiency_cards_per_min', 0)
    avg_daily_reviews = stats.get('avg_daily_reviews', 0)
    studied_val = stats.get('studied_percent', 100 - new_cards_val)

    MAIN_BLUE = _palette(is_night_mode)["blue"]

    efficiency_color = MAIN_BLUE
    accuracy_color = MAIN_BLUE
    retention_color = MAIN_BLUE

    tooltip_consistency = _("Reviews per day (last 30 days). The best day in this period is the top of the curve.")
    tooltip_avg_daily = _("Average cards reviewed per day over the last 30 days.")
    tooltip_efficiency = _("Cards per minute ({}). Time capped at 45s/card.").format(period_text)
    tooltip_accuracy = _("Correct answers: {:.1f}% ({}).").format(acc_real, period_text)
    tooltip_retention = _("Retention on reviews ({}).").format(period_text)
    tooltip_studied = _("Percentage of cards in your active collection that you have studied at least once.")
    tooltip_info = _("What do these statistics show?")
    tooltip_delta_period = _("vs. previous {} days").format(days_scope)
    tooltip_delta_30d = _("vs. previous {} days").format(30)
    tooltip_deck_filter = _("Filter statistics by deck")
    tooltip_export = _("Export dashboard snapshot as PNG")

    # Pre-computed translated labels for HTML
    label_consistency = _("Consistency")
    label_daily_average = _("Daily Average")
    label_eff_short = _("Eff.")
    label_acc_short = _("Acc.")
    label_retention = _("Retention")
    label_studied = _("Studied Cards")
    label_all_decks = _("All decks")
    label_export = _("Export")

    deck_options_html = f'<option value="0"{"" if selected_deck_id else " selected"}>{label_all_decks}</option>'
    for did, name in get_deck_filter_options():
        sel = " selected" if did == selected_deck_id else ""
        deck_options_html += f'<option value="{did}"{sel}>{_html_mod.escape(name)}</option>'

    avg_daily_badge = _format_delta_badge(
        stats.get('avg_daily_delta_pct'), "%", tooltip_delta_30d)
    efficiency_badge = _format_delta_badge(
        stats.get('efficiency_delta_pct'), "%", tooltip_delta_period)
    accuracy_badge = _format_delta_badge(
        stats.get('accuracy_delta_pp'), "pp", tooltip_delta_period)
    retention_badge = _format_delta_badge(
        stats.get('retention_delta_pp'), "pp", tooltip_delta_period)


    _cl = _palette(False)
    _cd = _palette(True)
    css = f"""
    <style>
        :root {{
            --spacer-height-efficiency: 10px;
            --stat-bg: {_cl["surface"]};
            --stat-border: {_cl["grey_light"]};
            --text-color: {_cl["text"]};
            --text-color-light: {_cl["text_muted"]};
            --progress-bg: {_cl["grey_light"]};
            --gray: {_cl["grey_light"]};
            --main-blue: {_cl["blue"]};
            --warning-blue: {_cl["streak_warn"]};
            --delta-up: {_cl["green"]};
            --delta-down: {_cl["red"]};
        }}
        body.night_mode {{
            --stat-bg: {_cd["surface"]};
            --stat-border: {_cd["grey_mid"]};
            --text-color: {_cd["text"]};
            --text-color-light: {_cd["text_muted"]};
            --progress-bg: {_cd["grey_mid"]};
            --gray: {_cd["grey_mid"]};
            --main-blue: {_cd["blue_bright"]};
            --warning-blue: {_cd["streak_warn"]};
            --delta-up: {_cd["green"]};
            --delta-down: {_cd["red"]};
        }}
        @keyframes spFadeIn {{
            from {{ opacity: 0; transform: translateY(-3px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .stats-toolbar {{
            display: flex;
            align-items: center;
            gap: 8px;
            max-width: {WIDGET_MAX_WIDTH};
            margin: 0 auto 8px auto;
            padding: 0 2px;
            box-sizing: border-box;
            animation: spFadeIn 220ms ease-out;
        }}
        .stats-toolbar-spacer {{ flex: 1 1 auto; }}
        .stats-deck-filter, .stats-export-btn {{
            font-size: 12px;
            padding: 4px 8px;
            border-radius: 6px;
            border: 1px solid var(--stat-border);
            background-color: var(--stat-bg);
            color: var(--text-color-light);
            cursor: pointer;
            max-width: 220px;
        }}
        .stats-export-btn {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-family: inherit;
        }}
        .stats-deck-filter:hover, .stats-export-btn:hover {{
            opacity: 0.85;
        }}
        .stat-delta {{
            font-size: 11px;
            font-weight: 600;
            margin-left: 6px;
            white-space: nowrap;
        }}
        .stat-delta-up {{ color: var(--delta-up); }}
        .stat-delta-down {{ color: var(--delta-down); }}
        .stats-widget-container {{
            /* anchor for the absolutely positioned info button (top right) */
            position: relative;
            display: grid;
            grid-template-columns: 1.1fr 1.2fr 0.9fr;
            gap: 16px;
            /* stretch: all three blocks share the tallest height, so their
               contents can bottom-align flush via the flexible spacers */
            align-items: stretch;
            background-color: var(--stat-bg);
            border-radius: 12px;
            /* extra right padding nudges the stats slightly left and leaves
               room for the info button; outer box dimensions are unchanged */
            padding: 16px 42px 16px 16px;
            margin: -15px auto 20px;
            max-width: {WIDGET_MAX_WIDTH};
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            color: var(--text-color);
            border: 1px solid var(--stat-border);
            box-sizing: border-box;
            animation: spFadeIn 220ms ease-out;
        }}
        body.night_mode .stats-widget-container {{
            border: 1px solid {_cd["grey_mid"]};
        }}
        .stat-block {{
            display: flex;
            flex-direction: column;
            gap: 8px;
            text-align: left;
        }}
        .stat-block.circle-group {{
            flex-direction: row;
            justify-content: center;
            gap: 20px;
        }}
        /* Divider before the middle and right blocks. With the grid gap of
           16px and 16px padding after the line, the spacing is symmetric on
           both sides of each divider. */
        .stat-block.divided {{
            border-left: 1px solid var(--stat-border);
            padding-left: 16px;
        }}
        .stat-block h3 {{
            margin: 0 0 4px 0;
            font-size: 16px;
            font-weight: 600;
        }}
        .stat-block h3.subtle-title {{
            color: var(--text-color-light);
            font-size: 14px;
            font-weight: normal;
        }}
        .stat-block .label {{
            color: var(--text-color-light);
            font-size: 14px;
        }}
        /* ── Mini activity sparkline: reviews/day, last 30 days ── */
        .spark-wrap {{
            width: 100%;
            line-height: 0;
            position: relative;
        }}
        .spark {{
            width: 100%;
            height: 62px;
            display: block;
        }}
        .spark-axis-labels {{
            display: flex;
            justify-content: space-between;
            line-height: normal;
            font-size: 10px;
            color: var(--text-color-light);
            margin-top: 2px;
            padding: 0 1px;
        }}
        .spark-tooltip {{
            position: absolute;
            transform: translate(-50%, -130%);
            background-color: var(--stat-bg);
            border: 1px solid var(--stat-border);
            color: var(--text-color);
            font-size: 11px;
            line-height: normal;
            padding: 3px 6px;
            border-radius: 4px;
            white-space: nowrap;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.1s ease;
            box-shadow: 0 2px 6px rgba(0,0,0,0.15);
            z-index: 5;
        }}

        .progress-row {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .progress-row .label {{
            flex: 0 0 auto;
            white-space: nowrap;
        }}
        .progress-bar-bg {{
            flex: 1 1 auto;
            min-width: 24px;
            height: 12px;
            background-color: var(--progress-bg);
            border-radius: 6px;
            overflow: hidden;
        }}
        .progress-bar-fill {{
            height: 100%;
            border-radius: 6px;
            transition: width 0.5s ease-out;
        }}
        .progress-row .value {{
            flex: 0 0 auto;
            text-align: right;
            font-size: 12px;
            color: var(--text-color-light);
            white-space: nowrap;
        }}
        .avg-daily-row {{
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: 8px;
        }}
        .avg-daily-value {{
            font-size: 16px;
            font-weight: 600;
            color: var(--text-color);
            white-space: nowrap;
        }}
        .single-circle-stat {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }}
        .retention-circle {{
            width: 70px;
            height: 70px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            font-size: 18px;
            font-weight: 600;
        }}
        /* ── Info button (top right): opens the statistics explanations ── */
        .stats-info-btn {{
            position: absolute;
            top: 12px;
            right: 12px;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            font-size: 12px;
            font-weight: 600;
            line-height: 1;
            color: var(--text-color-light);
            background-color: var(--progress-bg);
            cursor: pointer;
            user-select: none;
            opacity: 0.8;
            transition: opacity 0.2s ease;
        }}
        .stats-info-btn:hover {{
            opacity: 1;
        }}
    </style>
    """

    html = f"""
    <div class="stats-toolbar">
        <select class="stats-deck-filter" title="{tooltip_deck_filter}"
                onchange="pycmd('pycmd:synapsepro:stats_deck_filter:' + this.value)">
            {deck_options_html}
        </select>
        <span class="stats-toolbar-spacer"></span>
        <button type="button" class="stats-export-btn" title="{tooltip_export}"
                onclick="pycmd('pycmd:synapsepro:export_stats_image')">⇩ {label_export}</button>
    </div>
    <div class="stats-widget-container">
        <!-- Info button: opens a dialog explaining every statistic -->
        <div class="stats-info-btn" title="{tooltip_info}" onclick="pycmd('pycmd:synapsepro:stats_info')">i</div>

        <!-- Block 1: Consistency (activity sparkline) -->
        <div class="stat-block">
            <h3 class="subtle-title" title="{tooltip_consistency}" style="margin:0;">{label_consistency}</h3>
            <div style="flex: 1 1 auto;"></div>
            <div class="spark-wrap">
                {stats['chart_html']}
            </div>
        </div>

        <!-- Block 2: Daily Average + Efficiency bars -->
        <div class="stat-block divided">
            <div class="avg-daily-row" title="{tooltip_avg_daily}">
                <h3 class="subtle-title" style="margin:0;">{label_daily_average}</h3>
                <span class="avg-daily-value">{avg_daily_reviews:.0f}{avg_daily_badge}</span>
            </div>
            <div style="flex: 1 1 auto; min-height: var(--spacer-height-efficiency);"></div>

            <div class="progress-row" title="{tooltip_efficiency}">
                <span class="label">{label_eff_short}</span>
                <div class="progress-bar-bg" role="progressbar" aria-valuemin="0" aria-valuemax="100"
                     aria-valuenow="{eff_visual:.0f}" aria-valuetext="{eff_cards_per_min:.1f}/min"
                     aria-label="{label_eff_short}">
                    <div class="progress-bar-fill" style="width: {eff_visual:.1f}%; background-color: {efficiency_color};"></div>
                </div>
                <span class="value">{eff_cards_per_min:.1f}/min{efficiency_badge}</span>
            </div>

            <div class="progress-row" title="{tooltip_accuracy}">
                <span class="label">{label_acc_short}</span>
                <div class="progress-bar-bg" role="progressbar" aria-valuemin="0" aria-valuemax="100"
                     aria-valuenow="{acc_real:.0f}" aria-label="{label_acc_short}">
                    <div class="progress-bar-fill" style="width: {acc_visual:.1f}%; background-color: {accuracy_color};"></div>
                </div>
                <span class="value">{acc_real:.0f}%{accuracy_badge}</span>
            </div>
        </div>

        <!-- Block 3: Circles -->
        <div class="stat-block circle-group divided">
            <div class="single-circle-stat" title="{tooltip_retention}">
                <h3 class="subtle-title">{label_retention}{retention_badge}</h3>
                <div class="retention-circle-container">
                    <div class="retention-circle" role="img" aria-label="{label_retention}: {retention_val:.0f}%"
                         style="background: radial-gradient(closest-side, var(--stat-bg) 79%, transparent 80% 100%), conic-gradient({retention_color} {retention_val:.1f}%, var(--progress-bg) 0);">{retention_val:.0f}%</div>
                </div>
            </div>

            <div class="single-circle-stat" title="{tooltip_studied}">
                <h3 class="subtle-title">{label_studied}</h3>
                <div class="retention-circle-container">
                    <div class="retention-circle" role="img" aria-label="{label_studied}: {studied_val:.0f}%"
                         style="background: radial-gradient(closest-side, var(--stat-bg) 79%, transparent 80% 100%), conic-gradient({MAIN_BLUE} {studied_val:.1f}%, var(--progress-bg) 0);">{studied_val:.0f}%</div>
                </div>
            </div>
        </div>

    </div>
    """

    return css + html

def render_statistics_widget_html(stats_days=7, deck_id=None):
    """
    Hauptfunktion, die von __init__.py aufgerufen wird.
    Akzeptiert den Zeitraum und gibt das HTML zurück.
    """
    data = get_statistics_data(stats_days, deck_id=deck_id)
    return render_widget_html_internal(data)


def show_statistics_info_dialog(parent=None, stats_days=7):
    """Info dialog for the dashboard statistics widget ("i" button).

    Explains every statistic shown in the widget. Opened from the deck
    browser via pycmd ('synapsepro:stats_info'), see __init__.py.
    """
    from aqt.qt import QDialog, QVBoxLayout, QLabel, QDialogButtonBox

    if not isinstance(stats_days, int) or stats_days < 1:
        stats_days = 7
    if stats_days == 1:
        period_text = _("Last 24 hours")
    else:
        period_text = _("Last {} days").format(stats_days)

    sections = [
        (_("Consistency"),
         _("Your reviews per day over the last 30 days. The curve is relative "
           "to this period: the day with the most reviews forms the peak, days "
           "without reviews sit on the baseline. The dot marks today.")),
        (_("Daily Average"),
         _("Average cards reviewed per day over the last 30 days.")),
        (_("Efficiency (Eff.)"),
         _("How many cards you answer correctly per minute of study time "
           "({}). A full bar equals 7.5 correct cards per minute. Time per "
           "card is capped at 45 seconds so breaks don't distort the "
           "value.").format(period_text)),
        (_("Accuracy (Acc.)"),
         _("The percentage of all answered cards you got right ({}). The bar "
           "is scaled for readability — hover over it to see the exact "
           "value.").format(period_text)),
        (_("Retention"),
         _("The percentage of correct answers on review cards ({}) — cards "
           "you had already learned. This shows how well you retain content "
           "long-term.").format(period_text)),
        (_("Studied Cards"),
         _("Percentage of cards in your active collection that you have "
           "studied at least once.")),
    ]

    body = "".join(
        f"<p style='margin:0 0 12px 0;'><b>{title}</b><br>{text}</p>"
        for title, text in sections
    )
    footer = _("The time period ({}) can be changed in the SynapsePro settings.").format(period_text)
    html_text = (
        f"<h3 style='margin:0 0 12px 0;'>{_('Statistics')}</h3>"
        f"{body}"
        f"<p style='margin:0;color:#888;font-size:12px;'>{footer}</p>"
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle(_("SynapsePro - Statistics"))
    dialog.setMinimumWidth(520)

    layout = QVBoxLayout()
    label = QLabel(html_text)
    label.setWordWrap(True)

    button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    button_box.rejected.connect(dialog.reject)

    layout.addWidget(label)
    layout.addWidget(button_box)
    dialog.setLayout(layout)
    dialog.exec()
