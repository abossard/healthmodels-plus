"""Search modal screen for the health model watch TUI."""
from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from azext_healthmodels_plus.tui.domain.search import search_forest, search_occurrences
from azext_healthmodels_plus.tui.domain.occurrences import Occurrence
from azext_healthmodels_plus.tui.models.domain import Forest, SearchResult


def _format_result(result: SearchResult) -> Text:
    """Format a SearchResult as a Rich Text line matching tree rendering style."""
    hs = result.health_state
    label = Text()

    if result.is_signal:
        label.append("◈ ", style="cyan")
        label.append(result.display_name, style=f"bold {hs.color}")
        if result.signal_value is not None:
            label.append(f"  {result.signal_value}", style=f"bold {hs.color}")
        label.append(f"  {hs.icon}")
        if result.parent_display_name:
            label.append(f"  ← {result.parent_display_name}", style="dim italic")
    else:
        label.append(f"{hs.icon} ")
        label.append(result.display_name, style=f"bold {hs.color}")
        label.append(" ─── ", style="dim")
        label.append(hs.value, style=hs.color)
    if result.model:
        path = "/".join(p.rsplit("/", 1)[-1] for p in result.occurrence_path)
        label.append(f"  [{result.model.label}: {path}]", style="dim")

    return label


class SearchModal(ModalScreen[SearchResult | None]):
    """Modal overlay for searching entities and signals.

    Returns the selected ``SearchResult`` when the user presses Enter,
    or ``None`` if dismissed with Escape.
    """

    BINDINGS = [
        Binding("escape", "dismiss_search", "Close", priority=True),
    ]

    DEFAULT_CSS = """
    SearchModal {
        align: center middle;
    }

    SearchModal > Vertical {
        width: 80;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }

    SearchModal Input {
        dock: top;
        margin-bottom: 1;
    }

    SearchModal OptionList {
        height: auto;
        max-height: 20;
        background: $surface;
    }
    """

    def __init__(
        self,
        forest: Forest,
        initial_query: str = "",
        on_state: Callable[[str, list[SearchResult]], None] | None = None,
        *,
        occurrences: list[Occurrence] | None = None,
        unloaded: int = 0,
    ) -> None:
        super().__init__()
        self._forest = forest
        self._initial_query = initial_query
        self._results: list[SearchResult] = []
        self._on_search_state: Callable[[str, list[SearchResult]], None] | None = on_state
        self._occurrences = occurrences
        self._unloaded = unloaded

    def compose(self) -> ComposeResult:
        with Vertical():
            if self._unloaded:
                yield Static(f"Cached scope only: {self._unloaded} unloaded models", id="search-scope")
            yield Input(
                value=self._initial_query,
                placeholder="Search entities and signals…",
                id="search-input",
            )
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.focus()
        if self._initial_query:
            inp.action_select_all()
            self._run_search(self._initial_query)

    @on(Input.Changed, "#search-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._run_search(event.value)

    @on(Input.Submitted, "#search-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        self._select_current()

    @on(OptionList.OptionSelected, "#search-results")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._results):
            self.dismiss(self._results[idx])

    def _run_search(self, query: str) -> None:
        self._results = (
            search_occurrences(self._occurrences, query) if self._occurrences is not None
            else search_forest(self._forest, query)
        )
        option_list = self.query_one("#search-results", OptionList)
        option_list.clear_options()
        for result in self._results:
            option_list.add_option(Option(_format_result(result)))
        if self._results:
            option_list.highlighted = 0
        if callable(self._on_search_state):
            self._on_search_state(query, self._results)

    def _select_current(self) -> None:
        if not self._results:
            return
        option_list = self.query_one("#search-results", OptionList)
        idx = option_list.highlighted
        if idx is not None and 0 <= idx < len(self._results):
            self.dismiss(self._results[idx])

    def action_dismiss_search(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:  # noqa: ANN001
        """Route arrow keys from input to the option list."""
        option_list = self.query_one("#search-results", OptionList)
        if event.key == "down":
            option_list.action_cursor_down()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            option_list.action_cursor_up()
            event.prevent_default()
            event.stop()
