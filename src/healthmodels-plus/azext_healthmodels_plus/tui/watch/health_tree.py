"""Occurrence-aware tree presentation. No model cache or fetch-on-render."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.message import Message
from textual.geometry import Size
from textual.strip import Strip
from textual.widgets import Tree
from textual.widgets._tree import TreeNode, _TreeLine

from azext_healthmodels_plus.tui.domain.formatters import format_entity_label, format_signal_label
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.domain.occurrences import Occurrence, Path, project
from azext_healthmodels_plus.tui.domain.snapshot import build_change_map
from azext_healthmodels_plus.tui.models.domain import Forest, StateChange
from azext_healthmodels_plus.tui.models.enums import HealthState
from azext_healthmodels_plus.tui.watch.poller import MonitorView


@dataclass(frozen=True)
class EntityData:
    entity_name: str
    is_signal: bool = False
    owner_entity_name: str | None = None
    model: ModelKey | None = None
    path: Path = ()
    target: ModelKey | None = None
    cycle: bool = False
    state: HealthState | None = None


def guide_states(path: Sequence[HealthState | None]) -> list[HealthState | None]:
    """The health each guide of a line rolls up: a trunk its parent's, the connector its node's."""
    return [*path[:-2], path[-1]] if len(path) > 1 else []


class HealthTree(Tree[EntityData]):
    class Navigation(Message):
        def __init__(self, data: EntityData, *, expand: bool = False) -> None:
            super().__init__()
            self.data = data
            self.expand = expand

    def __init__(self, **kwargs: object) -> None:
        super().__init__("Health Model", **kwargs)
        self._node_map: dict[str, TreeNode[EntityData]] = {}
        self._occurrence_nodes: dict[Path, TreeNode[EntityData]] = {}
        self._expanded: set[Path] = set()
        self._collapsed: set[Path] = set()
        self._rendering = False
        self._restoring_cursor: Path | None = None
        self._unlinked_node: TreeNode[EntityData] | None = None

    def _build(self) -> None:
        """Replace the pinned Textual builder's recursive node walk."""
        lines: list[_TreeLine[EntityData]] = []
        work: list[tuple[TreeNode[EntityData], list[TreeNode[EntityData]], bool]] = (
            [(self.root, [], True)] if self.show_root else
            [(node, [], True) for node in reversed(self.root.children)]
        )
        while work:
            node, ancestors, last = work.pop()
            path = [*ancestors, node]
            node._line = len(lines)
            lines.append(_TreeLine(path, last))
            if node.is_expanded:
                children = node.children
                for index in range(len(children) - 1, -1, -1):
                    work.append((children[index], path, index == len(children) - 1))
        self._tree_lines_cached = lines
        width = max((
            self.get_label_width(line.node) + line._get_guide_width(self.guide_depth, self.show_root)
            for line in lines
        ), default=self.size.width)
        self.virtual_size = Size(width, len(lines))
        if self.cursor_line != -1 and self.cursor_node is not None:
            self.cursor_line = self.cursor_node.line
        if self.cursor_line >= len(lines):
            self.cursor_line = -1

    def _render_line(self, y: int, x1: int, x2: int, base_style: Style) -> Strip:
        strip = super()._render_line(y, 0, x2, base_style)
        if y < len(self._tree_lines):
            states = guide_states([node.data.state if node.data else None for node in self._tree_lines[y].path])
            depth = self.guide_depth
            cuts = [*range(depth, depth * len(states) + 1, depth), strip.cell_length]
            # The plain guide style first, so Unknown ("dim") does not keep the hover or cursor color.
            guide = self.get_component_rich_style("tree--guides", partial=True)
            strip = Strip.join(
                Strip(Segment.apply_style(part, post_style=guide + Style.parse(state.color)), part.cell_length)
                if state else part
                for part, state in zip(strip.divide(cuts), [*states, None])
            )
        return strip.crop(x1, x2)

    def apply_view(self, view: MonitorView) -> None:
        forests = {k: s.previous.forest for k, s in view.entries.items() if s.previous}
        rows = project(view.root, forests, self._expanded)
        changes = [c for s in view.entries.values() if s.previous for c in s.previous.changes]
        root = forests.get(view.root)
        self._apply(rows, changes, view, root.unlinked if root else ())

    def apply_forest(self, forest: Forest, changes: list[StateChange]) -> None:
        root = ModelKey("", "", "", "")
        self._apply(project(root, {root: forest}, set()), changes, None, forest.unlinked)

    def _apply(self, rows: list[Occurrence], changes: list[StateChange],
               view: MonitorView | None, unlinked: tuple[str, ...]) -> None:
        selected = self.cursor_node.data.path if self.cursor_node and self.cursor_node.data else None
        scroll_y = self.scroll_y
        self._rendering = True
        change_map = build_change_map(changes)
        expected = {row.path for row in rows} | {
            row.path + ("signals/" + sig.name.casefold(),) for row in rows for sig in row.entity.signals
        }
        rebuild = expected != set(self._occurrence_nodes)
        if rebuild:
            self.clear()
            self._node_map.clear()
            self._occurrence_nodes.clear()
            self._unlinked_node = None
        if unlinked and self._unlinked_node is None:
            self._unlinked_node = self.root.add(
                Text("⚠ Unlinked Entities", style="dim italic"), data=EntityData("__unlinked__"),
            )
            self._unlinked_node.expand()
        for row in rows:
            entity, path = row.entity, row.path
            parent = self._occurrence_nodes.get(row.parent, self.root)
            if not row.parent and entity.name in unlinked and self._unlinked_node is not None:
                parent = self._unlinked_node
            label = format_entity_label(entity, change_map.get(entity.entity_id))
            reference = entity.reference
            if reference:
                if reference.target:
                    label.append(f" -> {reference.target.name}", style="dim")  # full id is in the drawer
                state = reference.diagnostic or (view.status(reference.target) if view and reference.target else "unloaded")
                label.append(f" [{state}]", style="dim")
            if row.cycle:
                label.append(" [cycle link]", style="dim italic")
            data = EntityData(entity.name, model=row.model, path=path,
                              target=reference.target if reference else None, cycle=row.cycle,
                              state=entity.health_state)
            node = self._occurrence_nodes.get(path)
            if node is None:
                expandable = bool(entity.children or entity.signals or (reference and reference.target))
                node = parent.add(label, data=data, allow_expand=expandable and not row.cycle)
            else:
                node.set_label(label)
                node.data = data
            self._occurrence_nodes[path] = node
            self._node_map.setdefault(entity.entity_id, node)
            for sig in entity.signals:
                signal_path = path + ("signals/" + sig.name.casefold(),)
                leaf = self._occurrence_nodes.get(signal_path)
                signal_data = EntityData(sig.name, True, entity.name, row.model, signal_path,
                                         state=sig.health_state)
                if leaf is None:
                    leaf = node.add_leaf(format_signal_label(sig), data=signal_data)
                else:
                    leaf.set_label(format_signal_label(sig))
                    leaf.data = signal_data
                self._occurrence_nodes[signal_path] = leaf
            if (reference is None and path not in self._collapsed) or path in self._expanded:
                node.expand()
        self.root.expand()
        if rebuild:
            self._restoring_cursor = selected
            self.call_after_refresh(self._restore, selected, scroll_y)
        self._rendering = False

    def _restore(self, path: Path | None, scroll_y: float) -> None:
        node = self._occurrence_nodes.get(path) if path else None
        if node is not None:
            self.move_cursor(node)
        self.scroll_to(y=scroll_y, animate=False)

    def contains(self, path: Path) -> bool:
        return path in self._occurrence_nodes

    def reveal(self, path: Path, view: MonitorView) -> None:
        self._expanded.update(path[:i] for i in range(1, len(path)))
        self.apply_view(view)
        self.call_after_refresh(self._select_path, path)

    def _select_path(self, path: Path) -> None:
        node = self._occurrence_nodes.get(path)
        if node:
            self.move_cursor(node)
            self.scroll_to_node(node)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[EntityData]) -> None:
        data = event.node.data
        if self._rendering or not data or self._occurrence_nodes.get(data.path) is not event.node:
            return
        # Restored expansion events do not activate or fetch.
        if data.path in self._expanded:
            return
        if data.target and not data.cycle:
            self._expanded.add(data.path)
            self.post_message(self.Navigation(data, expand=True))
        self._collapsed.discard(data.path)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed[EntityData]) -> None:
        data = event.node.data
        if self._rendering or not data or self._occurrence_nodes.get(data.path) is not event.node:
            return
        self._expanded.discard(data.path)
        self._collapsed.add(data.path)
        self.post_message(self.Navigation(data))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[EntityData]) -> None:
        data = event.node.data
        if self._rendering or not data or self._occurrence_nodes.get(data.path) is not event.node:
            return
        if data.path == self._restoring_cursor:
            self._restoring_cursor = None
            return
        self.post_message(self.Navigation(data))

    def distances(self) -> dict[ModelKey, int]:
        cursor = self.cursor_node.data.path if self.cursor_node and self.cursor_node.data else ()
        distances: dict[ModelKey, int] = {}
        for path, node in self._occurrence_nodes.items():
            data = node.data
            if not data or not data.target:
                continue
            parent = node
            visible = True
            while parent:
                if not parent.is_expanded:
                    visible = False
                    break
                parent = parent.parent
            if not visible:
                continue
            common = 0
            for left, right in zip(cursor, path):
                if left != right:
                    break
                common += 1
            distance = len(cursor) + len(path) - 2 * common
            distances[data.target] = min(distance, distances.get(data.target, distance))
        return distances

    def scroll_to_entity(self, entity_id: str) -> None:
        node = self._node_map.get(entity_id)
        if node is not None:
            self._restoring_cursor = node.data.path if node.data else None
            self.scroll_to_node(node)
            self.move_cursor(node)
