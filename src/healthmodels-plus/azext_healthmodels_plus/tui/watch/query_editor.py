"""Query editor modal for viewing/testing a signal's configuration."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TextArea

from azext_healthmodels_plus.tui.client.query_executor import (
    execute_signal,
    resolve_signal_config,
)
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.domain.drafts import ResourceTarget, get_in, parse_field_path
from azext_healthmodels_plus.tui.watch.draft_store import DraftSnapshot, DraftStore, DraftTestResult


_KIND_LABEL = {
    "PrometheusMetricsQuery": "PromQL",
    "AzureResourceMetric": "ARM",
    "LogAnalyticsQuery": "KQL",
    "External": "External",
}


def _fmt_rule(rule: dict[str, Any] | None) -> str:
    if not rule:
        return "—"
    op = rule.get("operator", "?")
    threshold = rule.get("threshold", "?")
    return f"{op} {threshold}"


class QueryEditor(ModalScreen[None]):
    """Modal that shows and test-runs a signal's query configuration.

    Edits to the query text are applied only to the test run — they are
    not persisted back to the service.
    """

    BINDINGS = [
        Binding("escape", "dismiss_editor", "Close", priority=True),
        Binding("ctrl+r", "test_query", "Test", priority=True),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("f2", "open_actions", "Actions", priority=True),
    ]

    def __init__(
        self,
        client: CloudHealthClient,
        resource_group: str,
        model_name: str,
        entity_name: str,
        signal_name: str,
        draft_store: DraftStore | None = None,
        draft_target: ResourceTarget | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._rg = resource_group
        self._model = model_name
        self._entity_name = entity_name
        self._signal_name = signal_name
        self._config: dict[str, Any] = {}
        self._loaded: bool = False
        self._draft_store = draft_store
        self._draft_target = draft_target

    # ── layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="query-editor-root"):
            yield Label("Loading signal configuration…", id="qe-title")
            yield Static("", id="qe-meta")
            yield Label("Query text", classes="qe-section-label")
            yield TextArea("", id="qe-query", show_line_numbers=True)
            with Horizontal(id="qe-buttons"):
                yield Button("Test Query (Ctrl+R)", id="qe-test", variant="primary")
                yield Button("Close (Esc)", id="qe-close")
            yield Label("Test results", classes="qe-section-label")
            yield Static("— not yet run —", id="qe-results")

    # ── lifecycle ─────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.run_worker(self._load_config(), group="editor-load")

    async def _load_config(self) -> None:
        try:
            cfg = await asyncio.to_thread(self._fetch_config)
        except Exception as exc:  # noqa: BLE001
            if not self.is_mounted:
                return
            self.query_one("#qe-title", Label).update(
                Text(f"Error loading signal: {exc}", style="bold red"),
            )
            try:
                self.app.notify(
                    f"Cannot open query editor: {exc}",
                    severity="error",
                    title="Signal not found",
                )
            except Exception:  # noqa: BLE001 — notify is best-effort
                pass
            self._loaded = False
            return

        if not self.is_mounted:
            return
        self._config = cfg
        self._loaded = True
        self._render_config()

    def _fetch_config(self) -> dict[str, Any]:
        config = resolve_signal_config(
            self._client,
            self._rg,
            self._model,
            self._entity_name,
            self._signal_name,
        )
        return {
            "entity_raw": config.entity,
            "signal_instance_raw": config.signal_instance,
            "signal_definition_raw": config.signal_definition,
            "entity_display": config.entity_display,
            "signal_display": config.signal_display_name,
            "signal_kind": config.signal_kind,
            "signal_def_name": config.signal_definition_name,
            "group_key": config.signal_group,
            "query_path": self._signal_query_path(config.entity, config.signal_group, self._signal_name),
            "query_owned_by_definition": self._query_owned_by_definition(config.signal_instance, config.signal_definition),
            "data_source": config.data_source,
            "query_text": config.query_text,
            "time_grain": config.time_grain,
            "value_column_name": config.value_column_name,
            "data_unit": config.data_unit,
            "metric_namespace": config.metric_namespace,
            "metric_name": config.metric_name,
            "aggregation": config.aggregation,
            "degraded_rule": (config.evaluation_rules or {}).get("degradedRule"),
            "unhealthy_rule": (config.evaluation_rules or {}).get("unhealthyRule"),
        }

    @staticmethod
    def _query_owned_by_definition(
        signal_instance: dict[str, Any],
        signal_definition: dict[str, Any] | None,
    ) -> bool:
        has_instance_query = bool(str(signal_instance.get("queryText", "")).strip())
        if has_instance_query:
            return False
        if not isinstance(signal_definition, dict):
            return False
        properties = signal_definition.get("properties")
        return isinstance(properties, dict) and bool(str(properties.get("queryText", "")).strip())

    @staticmethod
    def _signal_query_path(entity: dict[str, Any], group_key: str, signal_name: str) -> str:
        groups = entity.get("properties", {}).get("signalGroups", {})
        group = groups.get(group_key)
        if not isinstance(group, dict):
            return "properties.queryText"
        signals = group.get("signals", [])
        if not isinstance(signals, list):
            return "properties.queryText"
        for index, signal in enumerate(signals):
            if isinstance(signal, dict) and signal.get("name") == signal_name:
                return f"properties.signalGroups.{group_key}.signals[{index}].queryText"
        return "properties.queryText"

    def _render_config(self) -> None:
        c = self._config
        kind = c.get("signal_kind", "")
        kind_label = _KIND_LABEL.get(kind, kind or "?")

        title = Text()
        title.append("◈ ", style="cyan")
        title.append(c.get("signal_display", ""), style="bold")
        title.append(f"   [{kind_label}]", style="magenta")
        title.append(f"   ← {c.get('entity_display', '')}", style="dim italic")
        self.query_one("#qe-title", Label).update(title)

        meta = Text()
        _add_row(meta, "Entity", c.get("entity_display", ""))
        _add_row(meta, "Signal kind", kind or "—")
        _add_row(meta, "Signal group", c.get("group_key", "") or "—")
        if c.get("query_owned_by_definition"):
            _add_row(meta, "Draft scope", "Shared signal definition")
        _add_row(meta, "Data source", c.get("data_source", "") or "—")
        _add_row(meta, "Time grain", c.get("time_grain", "") or "—")
        _add_row(meta, "Data unit", c.get("data_unit", "") or "—")
        if kind == "AzureResourceMetric":
            _add_row(meta, "Metric namespace", c.get("metric_namespace", "") or "—")
            _add_row(meta, "Metric name", c.get("metric_name", "") or "—")
            _add_row(meta, "Aggregation", c.get("aggregation", "") or "—")
        if kind == "LogAnalyticsQuery":
            _add_row(meta, "Value column", c.get("value_column_name", "") or "first column")
        _add_row(meta, "Degraded rule", _fmt_rule(c.get("degraded_rule")))
        _add_row(meta, "Unhealthy rule", _fmt_rule(c.get("unhealthy_rule")))
        if c.get("signal_def_name"):
            _add_row(meta, "Signal definition", c["signal_def_name"])

        self.query_one("#qe-meta", Static).update(meta)

        ta = self.query_one("#qe-query", TextArea)
        ta.text = self._resolve_initial_query(c.get("query_text", "") or "")
        ta.focus(scroll_visible=False)
        # After layout: below a long config, scroll to the buttons so the query above them is in view too.
        self.call_after_refresh(
            self.query_one("#query-editor-root").scroll_to_widget, self.query_one("#qe-buttons"), animate=False,
        )

    def _resolve_initial_query(self, fallback_query: str) -> str:
        found = self._draft(create=False)
        query = get_in(found[2].edited, parse_field_path(found[1])) if found else None
        return query if isinstance(query, str) else fallback_query

    def _draft(self, *, create: bool) -> tuple[ResourceTarget, str, DraftSnapshot] | None:
        """This editor's draft and query path; viewing or testing never creates one, only a change does."""
        if self._draft_store is None:
            return None
        target, query_path = self._active_draft_target_and_path()
        if target is None:
            return None
        draft = self._draft_store.get(target)
        if draft is None and create:
            baseline = self._draft_baseline_for_editor(str(self._config.get("query_text", "") or ""))
            draft = self._draft_store.get_or_create(target, baseline) if baseline is not None else None
        return (target, query_path, draft) if draft is not None else None

    def _draft_baseline_for_editor(self, fallback_query: str) -> dict[str, Any] | None:
        active_target, _ = self._active_draft_target_and_path()
        if active_target is None:
            return None
        if active_target.kind == "entity":
            raw = self._config.get("entity_raw")
            if isinstance(raw, dict):
                baseline = dict(raw)
                baseline["name"] = active_target.name
                return baseline
            return {"name": active_target.name, "properties": {}}
        if active_target.kind == "signal_definition":
            raw = self._config.get("signal_definition_raw")
            if isinstance(raw, dict):
                baseline = dict(raw)
                baseline["name"] = active_target.name
                return baseline
        signal_kind = str(self._config.get("signal_kind", "") or "")
        if not signal_kind:
            return None
        return {
            "name": active_target.name,
            "properties": {
                "queryText": fallback_query,
                "signalKind": signal_kind,
            },
        }

    def _active_draft_target_and_path(self) -> tuple[ResourceTarget | None, str]:
        if self._draft_target is None:
            return None, "properties.queryText"
        if bool(self._config.get("query_owned_by_definition")) and self._draft_target.kind == "entity":
            signal_def_name = str(self._config.get("signal_def_name", "") or "")
            if signal_def_name:
                return (
                    ResourceTarget(self._draft_target.model, "signal_definition", signal_def_name),
                    "properties.queryText",
                )
        if self._draft_target.kind != "entity":
            return self._draft_target, "properties.queryText"
        return self._draft_target, str(self._config.get("query_path", "properties.queryText"))

    # ── actions ───────────────────────────────────────────────────────

    @on(Button.Pressed, "#qe-close")
    def _on_close(self, event: Button.Pressed) -> None:
        self._persist_draft_query()
        self.dismiss(None)

    @on(Button.Pressed, "#qe-test")
    def _on_test(self, event: Button.Pressed) -> None:
        self.run_worker(self._test_query(), exclusive=True)

    @on(TextArea.Changed, "#qe-query")
    def _on_query_changed(self, event: TextArea.Changed) -> None:
        if self._loaded:
            self._persist_draft_query()

    def action_dismiss_editor(self) -> None:
        self._persist_draft_query()
        self.dismiss(None)

    def action_test_query(self) -> None:
        if self._loaded:
            self.run_worker(self._test_query(), exclusive=True)

    def action_request_quit(self) -> None:
        self._persist_draft_query()
        self.app.action_quit()

    def action_open_actions(self) -> None:
        self._persist_draft_query()
        self.app.action_open_actions()

    async def _test_query(self) -> None:
        if not self._loaded:
            return
        results = self.query_one("#qe-results", Static)
        results.update(Text("Running query…", style="italic yellow"))

        edited_query = self.query_one("#qe-query", TextArea).text
        original = self._config.get("query_text", "")
        self._persist_draft_query()
        found = self._draft(create=False)
        active_target = found[0] if found else None
        revision_for_result = found[2].revision if found else None

        try:
            result = await asyncio.to_thread(
                self._run_with_override, edited_query, original,
            )
        except Exception as exc:  # noqa: BLE001
            if not self.is_mounted:
                return
            err = Text()
            err.append("✗ Error: ", style="bold red")
            err.append(str(exc))
            results.update(err)
            return

        if self.is_mounted:
            results.update(_format_result(result))
            if (
                revision_for_result is not None
                and self._draft_store is not None
                and active_target is not None
            ):
                summary = "error" if result.get("error") else "ok"
                current = self._draft_store.get(active_target)
                if current is None or current.revision != revision_for_result:
                    return  # the query was edited while the test ran, so this result is stale
                self._draft_store.record_test_result(
                    active_target,
                    DraftTestResult(
                        revision=revision_for_result,
                        summary=summary,
                        recorded_at_utc=datetime.now(UTC),
                    ),
                )

    def _run_with_override(self, edited_query: str, original: str) -> dict[str, Any]:
        """Call execute_signal for an edited or unedited query."""
        return execute_signal(
            self._client, self._rg, self._model,
            self._entity_name, self._signal_name,
            query_override=None if edited_query == original else edited_query,
        )

    def _persist_draft_query(self) -> None:
        if not self._loaded:
            return
        query = self.query_one("#qe-query", TextArea).text
        unchanged = query == str(self._config.get("query_text", "") or "")
        found = self._draft(create=not unchanged)
        if found is None:
            return
        if unchanged:
            self._draft_store.revert_field(found[0], found[1])
        elif get_in(found[2].edited, parse_field_path(found[1])) != query:
            self._draft_store.update_field(found[0], found[1], query)


def _add_row(text: Text, key: str, value: str) -> None:
    text.append(f"{key:<18}", style="dim")
    text.append(f"{value}\n")


def _format_result(result: dict[str, Any]) -> Text:
    out = Text()
    error = result.get("error")
    if error:
        out.append("✗ ", style="bold red")
        out.append("Query failed\n", style="bold red")
        _add_row(out, "Error", str(error))
    else:
        out.append("✓ ", style="bold green")
        out.append("Query succeeded\n", style="bold green")

    _add_row(out, "Raw value", _fmt_value(result.get("rawValue")))
    _add_row(out, "Health state", str(result.get("healthState", "—")))
    warning = result.get("evaluationWarning")
    if warning:
        _add_row(out, "Evaluation", str(warning))
    _add_row(out, "Duration", f"{result.get('durationMs', 0)} ms")
    _add_row(out, "Timestamp", str(result.get("timestamp", "—")))

    raw = result.get("rawOutput")
    if raw is not None:
        out.append("\nRaw output:\n", style="dim bold")
        try:
            pretty = json.dumps(raw, indent=2, default=str)
        except (TypeError, ValueError):
            pretty = str(raw)
        if len(pretty) > 4000:
            pretty = pretty[:4000] + "\n…(truncated)"
        out.append(pretty, style="dim")

    return out


def _fmt_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
