"""Signal history sparkline — compact inline trend chart.

Pure, reusable rendering for a list of floats using Unicode block
characters (▁▂▃▄▅▆▇█).  The top-level public helpers are:

* :func:`render_sparkline`  —  values → :class:`rich.text.Text`
* :func:`scale_blocks`      —  values → one block glyph per value
* :func:`summarize`         —  values → ``(min, max, avg)``
* :func:`extract_history_values` — parse a ``getSignalHistory`` response

The module performs no I/O and has no Textual dependencies.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

from rich.text import Text

from azext_healthmodels_plus.tui.models.enums import HealthState

# Eight levels — matches the classic sparkline alphabet.
_BLOCKS: tuple[str, ...] = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
_EMPTY_PLACEHOLDER = "—"


# ─── Pure helpers ─────────────────────────────────────────────────────


def summarize(values: Iterable[float]) -> tuple[float, float, float] | None:
    """Return ``(min, max, avg)`` for *values*, or ``None`` if empty."""
    vs = [float(v) for v in values if v is not None]
    if not vs:
        return None
    return (min(vs), max(vs), sum(vs) / len(vs))


def scale_blocks(values: Iterable[float | None]) -> list[str]:
    """One block per value scaled to the series min..max, "" for None; a flat series sits mid-height."""
    series = [v if v is not None and math.isfinite(v) else None for v in values]
    present = [v for v in series if v is not None]
    lo, hi = (min(present), max(present)) if present else (0.0, 0.0)
    last_idx = len(_BLOCKS) - 1

    def block(v: float) -> str:
        if hi == lo:
            return _BLOCKS[last_idx // 2]
        return _BLOCKS[min(last_idx, max(0, int(round((v - lo) / (hi - lo) * last_idx))))]

    return ["" if v is None else block(v) for v in series]


def _downsample(values: list[float], width: int) -> list[float]:
    """Bucket *values* down to at most *width* points by averaging."""
    n = len(values)
    if n <= width:
        return values
    out: list[float] = []
    for i in range(width):
        lo = (i * n) // width
        hi = ((i + 1) * n) // width
        chunk = values[lo:hi] or [values[lo]]
        out.append(sum(chunk) / len(chunk))
    return out


def render_sparkline(
    values: list[float] | None,
    width: int = 20,
    state: HealthState | None = None,
) -> Text:
    """Render *values* as a single-line sparkline.

    Parameters
    ----------
    values:
        Series of floats (oldest first).  ``None`` or an empty list
        renders a dim placeholder so callers never crash on missing
        history.
    width:
        Target character width.  Longer series are averaged down;
        shorter series render at their natural length.
    state:
        Optional health state used to color the sparkline.  Defaults
        to a neutral dim style.
    """
    if not values:
        return Text(_EMPTY_PLACEHOLDER, style="dim italic")

    cleaned = [float(v) for v in values if v is not None]
    if not cleaned:
        return Text(_EMPTY_PLACEHOLDER, style="dim italic")

    glyphs = "".join(scale_blocks(_downsample(cleaned, max(1, width))))

    style = state.color if state is not None else "cyan"
    return Text(glyphs, style=style)


# ─── Response parsing ─────────────────────────────────────────────────


def extract_history_values(response: Any) -> list[float]:
    """Pull the numeric value series out of a ``getSignalHistory`` response.

    The exact shape of the response is not strongly contracted, so this
    helper is defensive: it walks a handful of common shapes and returns
    an empty list when it cannot find anything usable.  Never raises.
    """
    if not response:
        return []

    # Response may itself be a list of points.
    candidates: list[Any] = []
    if isinstance(response, list):
        candidates = response
    elif isinstance(response, dict):
        for key in ("history", "values", "points", "dataPoints", "items", "value"):
            maybe = response.get(key)
            if isinstance(maybe, list):
                candidates = maybe
                break

    values: list[float] = []
    for point in candidates:
        v = _coerce_point_value(point)
        if v is not None:
            values.append(v)
    return values


def _coerce_point_value(point: Any) -> float | None:
    if point is None:
        return None
    if isinstance(point, (int, float)) and not isinstance(point, bool):
        return float(point)
    if isinstance(point, dict):
        for key in ("value", "rawValue", "numericValue", "average", "avg", "y"):
            raw = point.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw)
                except ValueError:
                    continue
    return None


# ─── Summary renderer ─────────────────────────────────────────────────


def render_summary(values: list[float] | None) -> Text:
    """Render ``min / avg / max`` next to the sparkline."""
    stats = summarize(values or [])
    if stats is None:
        return Text("no history", style="dim italic")
    lo, hi, avg = stats
    t = Text()
    t.append("min ", style="dim")
    t.append(f"{lo:g}", style="bold")
    t.append("  avg ", style="dim")
    t.append(f"{avg:.3g}", style="bold")
    t.append("  max ", style="dim")
    t.append(f"{hi:g}", style="bold")
    t.append(f"  ({len(values or [])} pts)", style="dim")
    return t
