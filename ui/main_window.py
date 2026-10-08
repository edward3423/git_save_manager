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
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
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
        # Set Up before this Machine is set up, Sign In after: never both, so one button.
        self.setup_button = button("Set Up...", self.set_up_or_sign_in)
        self.machines_button = button("Machines", self.show_machines)
        self.log_button = button("Git Log", self.show_git_log)
        self.redo_button = button("Redo Initialization...", self.redo_initialization)

    def _build_filter_row(self) -> QVBoxLayout:
        """The state filter and the sweep each checked state unlocks, above the sidebar.

        These sit over the list rather than in the toolbar because that is what they are
        scoped to: the toolbar holds actions for the whole application, while the filter
        narrows this list and each sweep acts on exactly what the narrowing left in it.

        A bulk button is *only* reachable through its own filter. That is not decoration - the
        sidebar is then showing precisely the Entries the button will act on, so "all" is never
        a set the user has to take on trust. Uncheck the filter and the button goes with it.
        """
        self.filter_states: set[EntryState] = set()
        self.filter_actions: dict[EntryState, QAction] = {}
        self.bulk_buttons: dict[EntryState, QPushButton] = {}

        self.filter_button = QPushButton("Filter")
        menu = QMenu(self.filter_button)
        for state in presenter.BULK_ACTIONS:
            action = QAction(presenter.STATE_CAPTIONS[state], menu)
            action.setCheckable(True)
            action.toggled.connect(lambda checked, s=state: self._set_filter(s, checked))
            menu.addAction(action)
            self.filter_actions[state] = action
        self.filter_button.setMenu(menu)

        filter_row = QHBoxLayout()
        filter_row.addWidget(self.filter_button)
        filter_row.addStretch()

        found = QVBoxLayout()
        found.addLayout(filter_row)
        # One row per sweep, each full width and each appearing only with its own filter. Two
        # sweeps side by side would sit under a sidebar too narrow to read either label.
        for state, (_, label) in presenter.BULK_ACTIONS.items():
            widget = QPushButton(label)
            widget.clicked.connect(lambda _=False, s=state: self.run_bulk(s))
            widget.setVisible(False)
            self.bulk_buttons[state] = widget
            found.addWidget(widget)
        return found

    def _set_filter(self, state: EntryState, checked: bool) -> None:
        if checked:
            self.filter_states.add(state)
        else:
            self.filter_states.discard(state)
        self.refresh()

    def _build_body(self) -> None:
        self.sidebar = QListWidget()
        self.sidebar.currentItemChanged.connect(lambda *_: self._show_selected())

        self.detail_name = QLabel()
        self.detail_name.setObjectName("detailName")
        self.detail_state = QLabel()
        self.detail_state.setObjectName("detailState")
        self.detail_path = QLabel()
        self.detail_path.setWordWrap(True)
        # A path is something the user copies into Finder or a terminal.
        self.detail_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_hint = QLabel()
        self.detail_hint.setWordWrap(True)
        # Other Machines' Bindings for an unbound Entry; rebuilt per selection, hidden otherwise.
        self.detail_hints_slot = QVBoxLayout()
        self.detail_hints_slot.setContentsMargins(0, 0, 0, 0)
        self.detail_hints: dialogs.BindHintStrip | None = None

        def action_button(label: str, slot) -> QPushButton:
            found = QPushButton(label)
            found.clicked.connect(slot)
            return found

        self.sync_button = action_button("Sync", self.sync_selected)
        self.restore_button = action_button("Restore", self.restore_selected)
        self.resolve_button = action_button("Resolve...", self.resolve_selected)
        # `clicked` carries a `checked` bool; the lambda keeps it out of `prefill`.
        self.bind_button = action_button("Bind...", lambda: self.bind_selected())
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
        details_layout.addLayout(self.detail_hints_slot)
        details_layout.addLayout(actions_row)
        details_layout.addLayout(views_row)
        details_layout.addStretch()

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setObjectName("console")

        sidebar_panel = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_panel)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addLayout(self._build_filter_row())
        sidebar_layout.addWidget(self.sidebar)

        top = QSplitter()
        top.addWidget(sidebar_panel)
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
        """Recompute every row and caption from disk. The only writer of the sidebar.

        `self.rows` stays the *whole* truth; the filter narrows only what is drawn. Every
        action still revalidates against the full set, so a filter can hide an Entry but never
        change what the state machine says about it.
        """
        selected = self.selected_entry_id()
        self.rows = presenter.rows(self.app.paths, self.app.the_ledger)
        self.shown = presenter.visible_rows(self.rows, self.filter_states)

        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        for row in self.shown:
            item = QListWidgetItem(f"{row.name}    -    {row.caption}")
            item.setData(Qt.ItemDataRole.UserRole, row.entry_id)
            self.sidebar.addItem(item)
            if row.entry_id == selected:
                self.sidebar.setCurrentItem(item)
        self.sidebar.blockSignals(False)

        self._show_selected()
        self._show_cloud()
        self._show_bulk()

    def _bulk_targets(self, state: EntryState) -> list[presenter.Row]:
        """The Entries a sweep would act on: in that state, and still offering the action."""
        action, _ = presenter.BULK_ACTIONS[state]
        return presenter.bulk_targets(presenter.visible_rows(self.rows, {state}), action)

    def _show_bulk(self) -> None:
        """A sweep button appears with its filter, and carries the count it would act on.

        The count is on the button rather than in a confirmation, so the number is visible
        *before* the click - and it agrees with the sidebar, which is showing that same set.
        """
        for state, (_, label) in presenter.BULK_ACTIONS.items():
            on = state in self.filter_states
            targets = self._bulk_targets(state) if on else []
            widget = self.bulk_buttons[state]
            widget.setVisible(on)
            widget.setText(f"{label} ({len(targets)})")
            widget.setEnabled(bool(targets))

    def selected_entry_id(self) -> str | None:
        item = self.sidebar.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_row(self) -> presenter.Row | None:
        wanted = self.selected_entry_id()
        return next((row for row in self.rows if row.entry_id == wanted), None)

    def _show_selected(self) -> None:
        self._show_hints([])
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
                "Other Machines keep it at - click one to bind here with the same folder:"
                if hints
                else "Bind it to a folder here to sync it."
            )
            self._show_hints(hints)
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
        # Signing in again is also the way back from "authentication failed".
        self.setup_button.setText("Sign In..." if ready else "Set Up...")
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

        threading.Thread(target=work, name="status-fetch", daemon=True).start()

    def _settled(self) -> None:
        """Redraw from disk, then refetch the Cloud status the action just moved.

        Every Entry action commits locally, which changes this Machine's ahead/behind count -
        but the status bar reads a cached CloudStatus that only a fetch recomputes, so without
        this it would sit stale until the next manual Check. The fetch is async: the window
        never blocks, and `fetched` carries the second redraw back when it completes.
        """
        self.refresh()
        self._fetch_soon()

    # --- operations: one at a time, each re-validated at the moment it runs ----------------

    def _pat(self) -> str | None:
        found = self.store.get_pat()
        if found is None:
            log().warning("This Machine is not signed in to GitHub. Use Sign In first.")
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
            self._settled()

    def set_up(self) -> None:
        if dialogs.SetupDialog(self.app, self.store, self).exec():
            self.refresh()
            self._fetch_soon()

    def set_up_or_sign_in(self) -> None:
        if self.app.config.is_set_up:
            self.sign_in()
        else:
            self.set_up()

    def sign_in(self) -> None:
        if dialogs.SignInDialog(self.app, self.store, self).exec():
            self.refresh()
            self._fetch_soon()

    def redo_initialization(self) -> None:
        if dialogs.run_redo(self.app, self.store, self):
            self.refresh()  # back to the first-run state, in the running window

    def sync_selected(self) -> None:
        row = self._revalidated(Action.SYNC_TO_VAULT)
        if row is None:
            return
        note = dialogs.SyncDialog.ask(row.name, self)
        if note is None:  # the Sync was cancelled
            return
        try:
            operations.sync_to_vault(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                row.entry_id,
                note=note or None,
            )
        except (operations.SyncAborted, operations.NothingToSync) as error:
            log().warning("%s", error)
        self._settled()

    # --- the sweeps: many Entries, one approval, each still revalidated ---------------------

    def run_bulk(self, state: EntryState) -> None:
        """Push All / Pull All. Dispatched by state so the two share one entry point."""
        if state is EntryState.LOCAL_AHEAD:
            self.push_all_to_vault()
        else:
            self.pull_all_from_vault()

    def push_all_to_vault(self) -> None:
        """Sync every Local Ahead Entry, one commit each, sharing one summary.

        Nothing is batched into a single commit: `sync_to_vault` stays the only writer of a
        Baseline and still writes one per Entry, only once that Entry's commit has landed. A
        sweep is therefore N ordinary Syncs, and a failure part-way through leaves the Entries
        already done correctly Synced rather than half of a batch.
        """
        self.refresh()  # the staleness rule, once for the whole sweep
        targets = self._bulk_targets(EntryState.LOCAL_AHEAD)
        if not targets:
            log().warning("No Entry is Local Ahead; nothing was done.")
            return

        note = dialogs.SyncDialog.ask_all(len(targets), self)
        if note is None:  # cancelled
            return

        done = 0
        for row in targets:
            try:
                operations.sync_to_vault(
                    self.app.paths,
                    self.app.config,
                    self.app.description,
                    self.app.the_ledger,
                    row.entry_id,
                    note=note or None,
                )
                done += 1
            except (operations.SyncAborted, operations.NothingToSync) as error:
                log().warning("%s was not Synced: %s", row.name, error)
        log().info("Pushed %d of %d Entries to the Vault.", done, len(targets))
        self._settled()

    def pull_all_from_vault(self) -> None:
        """Restore every Vault Ahead Entry, behind one preview that names all of them.

        Invariant 7 is not weakened by doing several at once: the approval lists every path
        every Entry will write, and each `restore_to_live` is handed the same `Preview` object
        the user approved, so an Entry whose Live Save moved between the dialog and the write
        is refused by the executor rather than silently overwritten.
        """
        self.refresh()
        targets = self._bulk_targets(EntryState.VAULT_AHEAD)
        if not targets:
            log().warning("No Entry is Vault Ahead; nothing was done.")
            return

        previews = [
            (row, operations.preview_restore(self.app.paths, self.app.the_ledger, row.entry_id))
            for row in targets
        ]
        lines = presenter.bulk_preview_lines([(row.name, preview) for row, preview in previews])
        if not dialogs.PreviewDialog.approve_lines("Pull All from Vault", lines, self):
            return

        done = 0
        for row, preview in previews:
            try:
                operations.restore_to_live(
                    self.app.paths,
                    self.app.config,
                    self.app.the_ledger,
                    row.entry_id,
                    approved=preview,
                )
                done += 1
            except Exception as error:  # noqa: BLE001 - surfaced per Entry, never hidden
                log().warning("%s was not Restored: %s", row.name, error)
        log().info("Pulled %d of %d Entries from the Vault.", done, len(targets))
        self._settled()

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
        self._settled()

    def resolve_selected(self) -> None:
        row = self._revalidated(Action.RESOLVE)
        if row is None:
            return
        dialogs.resolve_sync_conflict(self.app, row.entry_id, row.name, self)
        self._settled()

    def _show_hints(self, hints: list[presenter.BindHint]) -> None:
        if self.detail_hints is not None:
            self.detail_hints_slot.removeWidget(self.detail_hints)
            self.detail_hints.deleteLater()
            self.detail_hints = None
        if hints:
            self.detail_hints = dialogs.BindHintStrip(hints)
            self.detail_hints.use_requested.connect(self.bind_selected)
            self.detail_hints_slot.addWidget(self.detail_hints)

    def bind_selected(self, prefill: presenter.BindHint | None = None) -> None:
        """Bind the selected Entry here; `prefill` is another Machine's Binding to start from."""
        self.refresh()
        row = self._selected_row()
        if row is None or row.status is not None:
            return
        # Widening the sparse cone may lazily fetch this Entry's blobs from the Cloud.
        dialog = dialogs.BindDialog(
            self.app, row.entry_id, row.name, self, pat=self.store.get_pat(), prefill=prefill
        )
        if dialog.exec():
            self._settled()

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
        self._settled()

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
        self._settled()

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
