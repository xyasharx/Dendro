# dendro/ui/main_window_inspector.py
"""MainWindow mixin handling package inspector synchronization and worker dispatch."""

from __future__ import annotations

import sys
from typing import Any

from PyQt6.QtCore import QItemSelection, QModelIndex, QPersistentModelIndex

from dendro.core.backend import (
    DependencyNode,
    DependencyTreeWorker,
    PackageChangelogWorker,
    PackageFilesWorker,
    PackageInfo,
    PackageScriptletsWorker,
    PackageVerifyWorker,
    ReverseDependencyWorker,
)
from dendro.core.models import TreeItem
from dendro.ui.window_presentation import (
    window_host_environment as _window_host_environment,
)


def _mw_hook(name: str, default: Any) -> Any:
    """Resolve optional monkeypatches applied to ``ui.main_window`` in tests."""
    module = sys.modules.get("dendro.ui.main_window")
    return getattr(module, name, default) if module is not None else default


def _inspector_pane(window) -> object:
    """Return the widget owned by the splitter for the inspector slot."""
    return getattr(window, "_inspector_host", None) or window.inspector_panel


def _inspector_open_min_width(window, pane: object) -> int:
    """Return the width threshold above which the inspector counts as open."""
    if pane is window.inspector_panel:
        return window.inspector_panel.minimumWidth()
    return max(pane.minimumWidth(), 1)


def _refresh_responsive_panes(window) -> None:
    update_panes = getattr(window, "_update_responsive_panes", None)
    if callable(update_panes):
        update_panes()


class MainWindowInspectorMixin:
    """Inspector selection, lazy dependency tree loading, and detail workers."""

    def _set_inspector_package_info(self, package: PackageInfo) -> None:
        """Replace inspector content and invalidate every older RPM-detail query."""
        self._inspector_generation += 1
        self.inspector_panel.set_package_info(package)

    def _refresh_active_inspector_status(self) -> None:
        """Update status, provenance, and queue actions without reloading tabs."""
        inspector = getattr(self, "inspector_panel", None)
        tree_model = getattr(self, "tree_model", None)
        if inspector is None or tree_model is None:
            return
        current = getattr(inspector, "_current_package", None)
        if current is None:
            return
        item = tree_model.item_for_package(current, allow_name_arch_fallback=True)
        if item and isinstance(item.payload, PackageInfo):
            refresh = getattr(inspector, "refresh_package_status", None)
            if callable(refresh):
                refresh(item.payload)

    def _clear_inspector_panel(self) -> None:
        """Invalidate RPM-detail callbacks before clearing the selected package."""
        self._inspector_generation += 1
        self.inspector_panel.clear()

    def _begin_inspector_request(self, kind: str) -> tuple[int, int]:
        """Identify one detail query so a newer click wins over old callbacks."""
        request_serial = self._inspector_request_serials.get(kind, 0) + 1
        self._inspector_request_serials[kind] = request_serial
        return self._inspector_generation, request_serial

    def _inspector_package_target(
        self, package: PackageInfo | str
    ) -> tuple[str, PackageInfo | None]:
        """Preserve an exact selected RPM record for inspector workers."""
        if isinstance(package, PackageInfo):
            return package.name, package
        package_name = package
        current = self.inspector_panel._current_package
        if current is not None and current.name == package_name:
            return package_name, current
        return package_name, None

    def _is_current_inspector_request(
        self,
        kind: str,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
    ) -> bool:
        current_package = self.inspector_panel._current_package
        return (
            inspector_generation == self._inspector_generation
            and request_serial == self._inspector_request_serials.get(kind)
            and current_package is not None
            and current_package.name == package_name
        )

    def _on_inspector_query_error(
        self,
        kind: str,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
        detail: str,
        renderer,
    ) -> None:
        if not self._is_current_inspector_request(
            kind, inspector_generation, request_serial, package_name
        ):
            return
        self._on_query_error(package_name, detail)
        renderer(package_name, detail)

    def _on_inspector_query_result(
        self,
        kind: str,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
        result: object,
        renderer,
    ) -> None:
        if self._is_current_inspector_request(
            kind, inspector_generation, request_serial, package_name
        ):
            renderer(package_name, result)

    def _start_inspector_worker(
        self,
        worker: object,
        kind: str,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
        error_renderer,
    ) -> None:
        """Surface an executor rejection rather than leaving an inspector loading."""
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            self._on_inspector_query_error(
                kind,
                inspector_generation,
                request_serial,
                package_name,
                f"Could not start RPM inspection: {type(error).__name__}: {error}",
                error_renderer,
            )

    def _on_tree_item_expanded(self, proxy_index: QModelIndex):
        source_index = self.proxy_model.mapToSource(proxy_index)
        if not source_index.isValid():
            return
        item: TreeItem = source_index.internalPointer()
        if (
            item
            and isinstance(item.payload, PackageInfo)
            and not item.dependencies_loaded
            and not item.is_loading_dependencies
        ):
            item.is_loading_dependencies = True
            pindex = QPersistentModelIndex(source_index)
            self._on_fetch_dependencies_requested(item.name, pindex)

    def _on_fetch_dependencies_requested(
        self, pkg_name: str, target_index: QPersistentModelIndex
    ):
        tree_generation = self._package_tree_generation
        target_item = (
            QModelIndex(target_index).internalPointer()
            if target_index.isValid()
            else None
        )
        package_info = (
            target_item.payload
            if isinstance(target_item, TreeItem)
            and isinstance(target_item.payload, PackageInfo)
            else None
        )
        worker_cls = _mw_hook("DependencyTreeWorker", DependencyTreeWorker)
        worker = worker_cls(
            root_package=pkg_name,
            max_depth=1,
            target_index=target_index,
            package_info=package_info,
        )
        worker.signals.dependencies_resolved.connect(
            lambda root, dependencies, index, generation=tree_generation: (
                self._on_dependencies_resolved_for_current_tree(
                    generation, root, dependencies, index
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=tree_generation, index=target_index: (
                self._on_dependency_query_error(generation, package, detail, index)
            )
        )
        try:
            self.thread_pool.start(worker)
        except Exception as error:
            self._on_dependency_query_error(
                tree_generation,
                pkg_name,
                "Could not start dependency inspection: "
                f"{type(error).__name__}: {error}",
                target_index,
            )

    def _on_dependencies_resolved_for_current_tree(
        self,
        tree_generation: int,
        root_pkg_name: str,
        dependencies: list[DependencyNode],
        target_index: QPersistentModelIndex | None,
    ) -> None:
        if tree_generation != self._package_tree_generation:
            return
        self.tree_model.attach_dependencies(root_pkg_name, dependencies, target_index)

    def _on_dependency_query_error(
        self,
        tree_generation: int,
        pkg_name: str,
        message: str,
        target_index: QPersistentModelIndex | None = None,
    ) -> None:
        if tree_generation != self._package_tree_generation:
            return
        if target_index is None:
            self._on_query_error(pkg_name, message)
            return
        self.status_bar.showMessage(f"Error: {message}", 8000)
        self.tree_model.reset_loading_state_for_index(target_index)

    def _on_fetch_reverse_deps_requested(self, package: PackageInfo | str):
        pkg_name, package_info = self._inspector_package_target(package)
        inspector_generation, request_serial = self._begin_inspector_request(
            "reverse_dependencies"
        )
        worker_cls = _mw_hook("ReverseDependencyWorker", ReverseDependencyWorker)
        worker = worker_cls(target_package=pkg_name, package_info=package_info)
        worker.signals.reverse_dependencies_resolved.connect(
            lambda package, dependencies, generation=inspector_generation, serial=request_serial: (
                self._on_reverse_dependencies_resolved(
                    generation, serial, package, dependencies
                )
            )
        )
        worker.signals.status_update.connect(
            lambda message, generation=inspector_generation, serial=request_serial, package=pkg_name: (
                self._on_inspector_status(
                    "reverse_dependencies", generation, serial, package, message
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_error(
                    "reverse_dependencies",
                    generation,
                    serial,
                    package,
                    detail,
                    self.inspector_panel.set_reverse_dependencies_error,
                )
            )
        )
        self._start_inspector_worker(
            worker,
            "reverse_dependencies",
            inspector_generation,
            request_serial,
            pkg_name,
            self.inspector_panel.set_reverse_dependencies_error,
        )

    def _on_inspector_status(
        self,
        kind: str,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
        message: str,
    ) -> None:
        if self._is_current_inspector_request(
            kind, inspector_generation, request_serial, package_name
        ):
            self.status_bar.showMessage(message)

    def _on_reverse_dependencies_resolved(
        self,
        inspector_generation: int,
        request_serial: int,
        package_name: str,
        dependencies: list[DependencyNode],
    ) -> None:
        if self._is_current_inspector_request(
            "reverse_dependencies",
            inspector_generation,
            request_serial,
            package_name,
        ):
            self.inspector_panel.set_reverse_dependencies(package_name, dependencies)

    def _on_inspect_files_requested(self, package: PackageInfo | str):
        pkg_name, package_info = self._inspector_package_target(package)
        inspector_generation, request_serial = self._begin_inspector_request("files")
        worker_cls = _mw_hook("PackageFilesWorker", PackageFilesWorker)
        worker = worker_cls(package_name=pkg_name, package_info=package_info)
        worker.signals.package_files_loaded.connect(
            lambda package, files, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_result(
                    "files",
                    generation,
                    serial,
                    package,
                    files,
                    self.inspector_panel.set_package_files,
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_error(
                    "files",
                    generation,
                    serial,
                    package,
                    detail,
                    self.inspector_panel.set_package_files_error,
                )
            )
        )
        self._start_inspector_worker(
            worker,
            "files",
            inspector_generation,
            request_serial,
            pkg_name,
            self.inspector_panel.set_package_files_error,
        )

    def _on_fetch_changelog_requested(self, package: PackageInfo | str):
        pkg_name, package_info = self._inspector_package_target(package)
        inspector_generation, request_serial = self._begin_inspector_request(
            "changelog"
        )
        worker_cls = _mw_hook("PackageChangelogWorker", PackageChangelogWorker)
        worker = worker_cls(package_name=pkg_name, package_info=package_info)
        worker.signals.package_changelog_loaded.connect(
            lambda package, entries, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_result(
                    "changelog",
                    generation,
                    serial,
                    package,
                    entries,
                    self.inspector_panel.set_package_changelog,
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_error(
                    "changelog",
                    generation,
                    serial,
                    package,
                    detail,
                    self.inspector_panel.set_package_changelog_error,
                )
            )
        )
        self._start_inspector_worker(
            worker,
            "changelog",
            inspector_generation,
            request_serial,
            pkg_name,
            self.inspector_panel.set_package_changelog_error,
        )

    def _on_fetch_scriptlets_requested(self, package: PackageInfo | str):
        pkg_name, package_info = self._inspector_package_target(package)
        inspector_generation, request_serial = self._begin_inspector_request(
            "scriptlets"
        )
        worker_cls = _mw_hook("PackageScriptletsWorker", PackageScriptletsWorker)
        worker = worker_cls(package_name=pkg_name, package_info=package_info)
        worker.signals.package_scriptlets_loaded.connect(
            lambda package, scripts, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_result(
                    "scriptlets",
                    generation,
                    serial,
                    package,
                    scripts,
                    self.inspector_panel.set_package_scriptlets,
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_error(
                    "scriptlets",
                    generation,
                    serial,
                    package,
                    detail,
                    self.inspector_panel.set_package_scriptlets_error,
                )
            )
        )
        self._start_inspector_worker(
            worker,
            "scriptlets",
            inspector_generation,
            request_serial,
            pkg_name,
            self.inspector_panel.set_package_scriptlets_error,
        )

    def _on_verify_package_files_requested(self, package: PackageInfo | str):
        pkg_name, package_info = self._inspector_package_target(package)
        inspector_generation, request_serial = self._begin_inspector_request(
            "verification"
        )
        worker_cls = _mw_hook("PackageVerifyWorker", PackageVerifyWorker)
        host_env_fn = _mw_hook("_window_host_environment", _window_host_environment)
        worker = worker_cls(
            package_name=pkg_name,
            package_info=package_info,
            ignore_mtime=host_env_fn(self).normalizes_rpm_mtimes,
        )
        worker.signals.package_verification_finished.connect(
            lambda package, results, source=worker, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_result(
                    "verification",
                    generation,
                    serial,
                    package,
                    results,
                    lambda verified_pkg, verified_results: (
                        self.inspector_panel.set_package_verification(
                            verified_pkg,
                            verified_results,
                            ignored_mtime=source.ignored_mtime,
                        )
                    ),
                )
            )
        )
        worker.signals.error_occurred.connect(
            lambda package, detail, generation=inspector_generation, serial=request_serial: (
                self._on_inspector_query_error(
                    "verification",
                    generation,
                    serial,
                    package,
                    detail,
                    self.inspector_panel.set_package_verification_error,
                )
            )
        )
        self._start_inspector_worker(
            worker,
            "verification",
            inspector_generation,
            request_serial,
            pkg_name,
            self.inspector_panel.set_package_verification_error,
        )

    def _on_tree_selection_changed(
        self, selected: QItemSelection, _deselected: QItemSelection
    ):
        indexes = selected.indexes()
        if not indexes:
            return

        proxy_idx = indexes[0]
        source_idx = self.proxy_model.mapToSource(proxy_idx)
        if not source_idx.isValid():
            return

        item: TreeItem = source_idx.internalPointer()
        if item is None:
            return

        root_item = item.get_root_package_item()
        if root_item and isinstance(root_item.payload, PackageInfo):
            self._set_inspector_package_info(root_item.payload)
            if not self._inspector_panel_is_open():
                self._show_inspector_panel()

    def _inspector_panel_is_open(self) -> bool:
        """Return whether the inspector is visible inside the splitter bounds."""
        pane = _inspector_pane(self)
        splitter_sizes = self.main_splitter.sizes()
        return (
            pane.isVisible()
            and self.inspector_panel.isVisible()
            and len(splitter_sizes) == 3
            and splitter_sizes[2] >= _inspector_open_min_width(self, pane)
        )

    def _hide_inspector_panel(self) -> None:
        """Hide the inspector pane and update responsive splitter panes."""
        _inspector_pane(self).hide()
        _refresh_responsive_panes(self)

    def _show_inspector_panel(self) -> None:
        """Show the inspector and recover it from a collapsed saved splitter state."""
        pane = _inspector_pane(self)
        pane.show()
        self.inspector_panel.show()
        _refresh_responsive_panes(self)
        splitter_sizes = self.main_splitter.sizes()
        minimum_width = self.inspector_panel.minimumWidth()
        if pane is not self.inspector_panel and len(splitter_sizes) == 3:
            available = sum(splitter_sizes)
            if available > 0:
                sidebar_widget = getattr(self, "sidebar", None)
                sidebar_reserved = (
                    splitter_sizes[0]
                    if sidebar_widget is not None and sidebar_widget.isVisible()
                    else 0
                )
                minimum_width = min(
                    minimum_width,
                    max(pane.minimumWidth(), available - sidebar_reserved - 136),
                )
        if len(splitter_sizes) != 3 or splitter_sizes[2] >= minimum_width:
            return

        missing_width = minimum_width - splitter_sizes[2]
        from_workspace = min(missing_width, splitter_sizes[1])
        splitter_sizes[1] -= from_workspace
        splitter_sizes[0] = max(0, splitter_sizes[0] - (missing_width - from_workspace))
        splitter_sizes[2] = minimum_width
        self.main_splitter.setSizes(splitter_sizes)

    def _toggle_inspector_panel(self):
        if self._inspector_panel_is_open():
            _inspector_pane(self).hide()
            _refresh_responsive_panes(self)
        else:
            self._show_inspector_panel()


__all__ = ("MainWindowInspectorMixin",)
