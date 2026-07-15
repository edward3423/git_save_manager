"""The main window: sidebar, details panel, toolbar, status bar, log console.

Everything here renders; nothing here decides. States, captions, and what each state offers
come from `ui.presenter` and `core.entry_state`, so the window can never disagree with the
state machine about what an Entry is.

Rules from the plan enforced structurally in this file:

- **Staleness**: an Entry's state is recomputed on window focus and immediately before any
  operation runs - never cached across either. No filesystem watchers (Section 6).
- **Offline Mode greys out Push, Pull and Fetch, and nothing else.** Everything purely local
  keeps working; the way back is the Check Connection button, and only that.
- **Every destructive flow goes through the preview dialog** (Invariant 7) and hands the
  approved preview to the executor, which refuses if the world has moved since.
- **The startup fetch is asynchronous.** A GUI that blocks on the network for up to five
  minutes is indistinguishable from a crashed one.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core import operations
from core.cloud import CloudOffline, ForeignConflict, PushRejected
from core.credentials import CredentialStore, KeyringCredentialStore
from core.entry_state import Action, EntryState
from core.logger import FanoutHandler, log
from core.startup import App
from ui import dialogs, presenter


class MainWindow(QMainWindow):
    """One window, one `App`, one operation at a time."""

    log_line = pyqtSignal(str)
    """Log records cross into Qt through this signal, so a record emitted from any thread
    lands on the UI thread before it touches a widget."""

    fetched = pyqtSignal()
    """The async startup fetch finished (either way); refresh on the UI thread."""

    def __init__(
        self,
        app: App,
        fanout: FanoutHandler,
        store: CredentialStore | None = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.store = store or KeyringCredentialStore()

        self.setWindowTitle("Git Save Manager")
        self.resize(980, 640)

        self._build_toolbar()
        self._build_body()
        self._build_status_bar()

        self.log_line.connect(self.console.appendPlainText)
        fanout.add_listener(self.log_line.emit)
        self.fetched.connect(self.refresh)

        self.refresh()
        self._fetch_soon()

    # --- layout ---------------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Actions")
        bar.setMovable(False)
        self.addToolBar(bar)

        def button(label: str, slot) -> QPushButton:
            found = QPushButton(label)
            found.clicked.connect(slot)
            bar.addWidget(found)
            return found

        self.add_button = button("Add Entry", self.add_entry)
        self.pull_button = button("Pull", self.pull)
        self.push_button = button("Push", self.push)
        self.fetch_button = button("Fetch Status", self.fetch_status)
        self.check_button = button("Check Connection", self.check_connection)
        self.setup_button = button("Set Up...", self.set_up)
        self.machines_button = button("Machines", self.show_machines)
        self.log_button = button("Git Log", self.show_git_log)

    def _build_body(self) -> None:
        self.sidebar = QListWidget()
        self.sidebar.currentItemChanged.connect(lambda *_: self._show_selected())

        self.detail_name = QLabel()
        self.detail_name.setObjectName("detailName")
        self.detail_state = QLabel()
        self.detail_state.setObjectName("detailState")
        self.detail_path = QLabel()
        self.detail_path.setWordWrap(True)
        self.detail_hint = QLabel()
        self.detail_hint.setWordWrap(True)

        def action_button(label: str, slot) -> QPushButton:
            found = QPushButton(label)
            found.clicked.connect(slot)
            return found

        self.sync_button = action_button("Sync", self.sync_selected)
        self.restore_button = action_button("Restore", self.restore_selected)
        self.resolve_button = action_button("Resolve...", self.resolve_selected)
        self.bind_button = action_button("Bind...", self.bind_selected)
        self.unbind_button = action_button("Unbind", self.unbind_selected)
        self.history_button = action_button("History...", self.show_history)
        self.backups_button = action_button("Backups...", self.show_backups)
        self.remove_button = action_button("Remove from Vault", self.remove_selected)

        actions_row = QHBoxLayout()
        for widget in (
            self.sync_button,
            self.restore_button,
            self.resolve_button,
            self.bind_button,
            self.unbind_button,
        ):
            actions_row.addWidget(widget)
        actions_row.addStretch()

        views_row = QHBoxLayout()
        for widget in (self.history_button, self.backups_button, self.remove_button):
            views_row.addWidget(widget)
        views_row.addStretch()

        details = QWidget()
        details_layout = QVBoxLayout(details)
        for widget in (self.detail_name, self.detail_state, self.detail_path, self.detail_hint):
            details_layout.addWidget(widget)
        details_layout.addLayout(actions_row)
        details_layout.addLayout(views_row)
        details_layout.addStretch()

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setObjectName("console")

        top = QSplitter()
        top.addWidget(self.sidebar)
        top.addWidget(details)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 2)

        body = QSplitter(Qt.Orientation.Vertical)
        body.addWidget(top)
        body.addWidget(self.console)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)

        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.addWidget(body)
        self.setCentralWidget(wrapper)

    def _build_status_bar(self) -> None:
        self.cloud_label = QLabel()
        bar = QStatusBar()
        bar.addPermanentWidget(self.cloud_label)
        self.setStatusBar(bar)

    # --- staleness: recompute on focus and before every operation --------------------------

    def event(self, event) -> bool:  # noqa: N802 - Qt's name
        if event.type() == event.Type.WindowActivate:
            self.refresh()
        return super().event(event)

    def refresh(self) -> None:
        """Recompute every row and caption from disk. The only writer of the sidebar."""
        selected = self.selected_entry_id()
        self.rows = presenter.rows(self.app.paths, self.app.the_ledger)

        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        for row in self.rows:
            item = QListWidgetItem(f"{row.name}    -    {row.caption}")
            item.setData(Qt.ItemDataRole.UserRole, row.entry_id)
            self.sidebar.addItem(item)
            if row.entry_id == selected:
                self.sidebar.setCurrentItem(item)
        self.sidebar.blockSignals(False)

        self._show_selected()
        self._show_cloud()

    def selected_entry_id(self) -> str | None:
        item = self.sidebar.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_row(self) -> presenter.Row | None:
        wanted = self.selected_entry_id()
        return next((row for row in self.rows if row.entry_id == wanted), None)

    def _show_selected(self) -> None:
        row = self._selected_row()
        entry_buttons = (
            self.sync_button,
            self.restore_button,
            self.resolve_button,
            self.bind_button,
            self.unbind_button,
            self.history_button,
            self.backups_button,
            self.remove_button,
        )

        if row is None:
            self.detail_name.setText("No Entry selected")
            self.detail_state.setText("")
            self.detail_path.setText("")
            self.detail_hint.setText(
                "Set up a Cloud Vault, then add an Entry to begin."
                if not self.app.config.is_set_up
                else ""
            )
            for widget in entry_buttons:
                widget.setEnabled(False)
            return

        self.detail_name.setText(row.name)
        self.detail_state.setText(row.caption)
        self.history_button.setEnabled(True)
        self.backups_button.setEnabled(True)
        self.remove_button.setEnabled(True)

        if row.status is None:
            self.detail_path.setText("Not bound on this Machine.")
            hints = presenter.bind_hints(self.app.paths, self.app.config, row.entry_id)
            self.detail_hint.setText(
                "Other Machines keep it at: " + "; ".join(hints)
                if hints
                else "Bind it to a folder here to sync it."
            )
            self.sync_button.setEnabled(False)
            self.restore_button.setEnabled(False)
            self.resolve_button.setEnabled(False)
            self.bind_button.setEnabled(True)
            self.unbind_button.setEnabled(False)
            return

        binding = self.app.the_ledger.require(row.entry_id)
        self.detail_path.setText(str(binding.live))
        self.detail_hint.setText(
            "This Entry was removed from the Vault by another Machine. Unbind it here, or "
            "Sync to re-add it."
            if row.status.state is EntryState.REMOVED_FROM_VAULT
            else ""
        )
        self.sync_button.setEnabled(Action.SYNC_TO_VAULT in row.status.offered)
        self.restore_button.setEnabled(Action.RESTORE_TO_LIVE in row.status.offered)
        self.resolve_button.setEnabled(Action.RESOLVE in row.status.offered)
        self.bind_button.setEnabled(False)
        # Unbind moves no data and touches no save, so unlike Sync and Restore it is not
        # gated on the state machine's offers: a bound Entry can always be unbound.
        self.unbind_button.setEnabled(True)

    def _show_cloud(self) -> None:
        self.cloud_label.setText(presenter.cloud_caption(self.app.cloud, now=datetime.now(UTC)))
        online = self.app.cloud.offline is None
        ready = self.app.config.is_set_up
        self.add_button.setEnabled(ready)
        self.pull_button.setEnabled(online and ready)
        self.push_button.setEnabled(online and ready)
        self.fetch_button.setEnabled(online and ready)
        self.check_button.setEnabled(ready)  # explicitly allowed while offline: the way back
        self.setup_button.setEnabled(not ready)
        self.machines_button.setEnabled(ready)
        self.log_button.setEnabled(ready)

    # --- the async startup fetch ------------------------------------------------------------

    def _fetch_soon(self) -> None:
        """Fetch the Cloud status without blocking the window (plan: async, non-blocking).

        The worker touches no widget: it runs the fetch, and the `fetched` signal carries
        the refresh back onto the UI thread.
        """
        if not self.app.config.is_set_up:
            return
        pat = self.store.get_pat()
        if pat is None:
            return

        def work() -> None:
            with contextlib.suppress(CloudOffline):  # the caption shows it; nothing else to do
                self.app.cloud.fetch_status(pat)
            self.fetched.emit()

        threading.Thread(target=work, name="startup-fetch", daemon=True).start()

    # --- operations: one at a time, each re-validated at the moment it runs ----------------

    def _pat(self) -> str | None:
        found = self.store.get_pat()
        if found is None:
            log().warning("No GitHub token is stored. Run Set Up first.")
        return found

    def _revalidated(self, action: Action) -> presenter.Row | None:
        """The staleness rule: recompute, and only proceed if the state still offers this."""
        self.refresh()
        row = self._selected_row()
        if row is None or row.status is None:
            return None
        if action not in row.status.offered:
            log().warning("%s no longer offers %s; nothing was done.", row.name, action.value)
            return None
        return row

    def add_entry(self) -> None:
        if dialogs.AddEntryDialog(self.app, self).exec():
            self.refresh()

    def set_up(self) -> None:
        if dialogs.SetupDialog(self.app, self.store, self).exec():
            self.refresh()
            self._fetch_soon()

    def sync_selected(self) -> None:
        row = self._revalidated(Action.SYNC_TO_VAULT)
        if row is None:
            return
        try:
            operations.sync_to_vault(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                row.entry_id,
            )
        except (operations.SyncAborted, operations.NothingToSync) as error:
            log().warning("%s", error)
        self.refresh()

    def restore_selected(self) -> None:
        row = self._revalidated(Action.RESTORE_TO_LIVE)
        if row is None:
            return
        preview = operations.preview_restore(self.app.paths, self.app.the_ledger, row.entry_id)
        if dialogs.PreviewDialog.approve(f"Restore {row.name}", preview, self):
            try:
                operations.restore_to_live(
                    self.app.paths,
                    self.app.config,
                    self.app.the_ledger,
                    row.entry_id,
                    approved=preview,
                )
            except Exception as error:  # noqa: BLE001 - surfaced, not hidden
                log().warning("%s", error)
        self.refresh()

    def resolve_selected(self) -> None:
        row = self._revalidated(Action.RESOLVE)
        if row is None:
            return
        dialogs.resolve_sync_conflict(self.app, row.entry_id, row.name, self)
        self.refresh()

    def bind_selected(self) -> None:
        self.refresh()
        row = self._selected_row()
        if row is None or row.status is not None:
            return
        if dialogs.BindDialog(self.app, row.entry_id, row.name, self).exec():
            self.refresh()

    def unbind_selected(self) -> None:
        self.refresh()
        row = self._selected_row()
        if row is None or row.status is None:
            return
        operations.unbind_entry(
            self.app.paths,
            self.app.config,
            self.app.description,
            self.app.the_ledger,
            row.entry_id,
        )
        self.refresh()

    def remove_selected(self) -> None:
        self.refresh()
        row = self._selected_row()
        if row is None:
            return
        from PyQt6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self,
            "Remove from Vault",
            f"Remove {row.name} from the Vault, for every Machine?\n\n"
            "A forward commit: the content stays recoverable from history, and no Live "
            "Save anywhere is touched.",
        )
        if answer is QMessageBox.StandardButton.Yes:
            operations.remove_from_vault(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                row.entry_id,
            )
        self.refresh()

    def show_history(self) -> None:
        row = self._selected_row()
        if row is not None:
            dialogs.HistoryDialog(self.app, row.entry_id, row.name, self).exec()
            self.refresh()

    def show_backups(self) -> None:
        row = self._selected_row()
        if row is not None:
            dialogs.BackupsDialog(self.app, row.entry_id, row.name, self).exec()
            self.refresh()

    def show_machines(self) -> None:
        dialogs.MachinesDialog(self.app, self).exec()

    def show_git_log(self) -> None:
        dialogs.GitLogDialog(self.app, self).exec()

    def _cloud_call(self, action) -> None:
        pat = self._pat()
        if pat is None:
            return
        try:
            action(pat)
        except (CloudOffline, PushRejected, ForeignConflict) as error:
            log().warning("%s", error)
        self.refresh()

    def fetch_status(self) -> None:
        self._cloud_call(self.app.cloud.fetch_status)

    def pull(self) -> None:
        def run(pat: str) -> None:
            pulled = self.app.cloud.pull(pat, self.app.description)
            if pulled.conflicts:
                dialogs.resolve_merge_conflicts(self.app, pulled.conflicts, self)
            else:
                log().info("Pulled %d commit(s).", pulled.commits)

        self._cloud_call(run)

    def push(self) -> None:
        def run(pat: str) -> None:
            self.app.cloud.push(pat)
            log().info("Pushed.")

        self._cloud_call(run)

    def check_connection(self) -> None:
        def run(pat: str) -> None:
            if self.app.cloud.check_connection(pat):
                log().info("Connection restored.")
            else:
                log().warning("Still offline.")

        self._cloud_call(run)
