"""Tests for the sparkline pure rendering helpers."""
from __future__ import annotations

import pytest
from rich.text import Text

from azext_healthmodels_plus.tui.models.enums import HealthState
from azext_healthmodels_plus.tui.watch.sparkline import (
    extract_history_values,
    render_sparkline,
    render_summary,
    scale_blocks,
    summarize,
)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_scale_blocks_leaves_non_finite_values_blank(bad: float) -> None:
    blocks = scale_blocks([1.0, bad, 3.0])
    assert blocks[1] == "" and blocks[0] and blocks[2]
    assert render_sparkline([1.0, bad, 3.0]).plain


# ─── render_sparkline ─────────────────────────────────────────────────


class TestRenderSparkline:
    @pytest.mark.parametrize("values", [None, [], [None, None]])
    def test_empty_renders_placeholder(self, values):
        text = render_sparkline(values)
        assert isinstance(text, Text)
        assert text.plain == "—"

    def test_ascending_series_uses_full_range(self):
        text = render_sparkline([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        assert text.plain[0] == "▁"
        assert text.plain[-1] == "█"

    def test_flat_series_uses_middle_block(self):
        text = render_sparkline([5.0, 5.0, 5.0, 5.0])
        assert set(text.plain) == {"▄"}

    def test_downsamples_to_width(self):
        values = list(range(100))
        text = render_sparkline([float(v) for v in values], width=10)
        assert len(text.plain) == 10

    def test_shorter_than_width_stays_natural_length(self):
        text = render_sparkline([1.0, 2.0, 3.0], width=20)
        assert len(text.plain) == 3

    @pytest.mark.parametrize(
        "state,expected_style",
        [
            (HealthState.HEALTHY, "green"),
            (HealthState.DEGRADED, "yellow"),
            (HealthState.UNHEALTHY, "red"),
        ],
    )
    def test_color_follows_health_state(self, state, expected_style):
        text = render_sparkline([1.0, 2.0, 3.0], state=state)
        assert text.style == expected_style

    def test_ignores_none_entries(self):
        text = render_sparkline([1.0, None, 2.0, None, 3.0])  # type: ignore[list-item]
        assert len(text.plain) == 3


# ─── summarize ────────────────────────────────────────────────────────


class TestSummarize:
    def test_empty_returns_none(self):
        assert summarize([]) is None

    def test_returns_min_max_avg(self):
        assert summarize([1.0, 2.0, 3.0, 4.0]) == (1.0, 4.0, 2.5)

    def test_ignores_none(self):
        assert summarize([None, 10.0, None, 20.0]) == (10.0, 20.0, 15.0)


# ─── extract_history_values ───────────────────────────────────────────


class TestExtractHistoryValues:
    @pytest.mark.parametrize("response", [None, {}, [], {"other": 1}])
    def test_missing_returns_empty(self, response):
        assert extract_history_values(response) == []

    def test_history_list_of_dicts_with_value(self):
        resp = {"history": [{"value": 1.5}, {"value": 2.5}, {"value": 3.5}]}
        assert extract_history_values(resp) == [1.5, 2.5, 3.5]

    def test_top_level_list(self):
        resp = [{"value": 10}, {"rawValue": 20}, {"numericValue": 30}]
        assert extract_history_values(resp) == [10.0, 20.0, 30.0]

    def test_string_values_are_coerced(self):
        resp = {"points": [{"value": "1.25"}, {"value": "2.75"}]}
        assert extract_history_values(resp) == [1.25, 2.75]

    def test_unparseable_points_are_skipped(self):
        resp = {"history": [{"value": "nope"}, {"value": 5}, {}]}
        assert extract_history_values(resp) == [5.0]

    def test_bool_values_rejected(self):
        # bool is a subclass of int — must not be treated as numeric.
        resp = {"history": [{"value": True}, {"value": 1.5}]}
        assert extract_history_values(resp) == [1.5]


# ─── render_summary ───────────────────────────────────────────────────


class TestRenderSummary:
    def test_empty_placeholder(self):
        assert render_summary([]).plain == "no history"

    def test_shows_stats(self):
        out = render_summary([1.0, 2.0, 3.0]).plain
        assert "min" in out and "max" in out and "avg" in out
        assert "3 pts" in out
