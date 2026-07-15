"""The main window: sidebar, details panel, toolbar, status bar, log console.

Everything here renders; nothing here decides. States, captions, and what each state offers
come from `ui.presenter` and `core.entry_state`, so the window can never disagree with the
state machine about what an Entry is.

Two rules from the plan are enforced structurally in this file:

- **Staleness**: an Entry's state is recomputed on window focus and immediately before any
  operation runs - never cached across either. No filesystem watchers (Section 6).
- **Offline Mode greys out Push and Pull, and nothing else.** Sync, history, and everything
  purely local keep working; the way back is the Check Connection button, and only that.

The buttons whose flows need dialogs (Add, Bind, Restore, Resolve, Setup) are present but
disabled until the dialogs slice lands: Restore without its preview would violate
Invariant 7, and a hidden button would suggest the capability does not exist.
"""

from __future__ import annotations

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
from core.cloud import CloudOffline, PushRejected
from core.credentials import CredentialStore, KeyringCredentialStore
from core.entry_state import Action
from core.logger import FanoutHandler, log
from core.startup import App
from ui import presenter

DIALOGS_PENDING = "Arrives with the dialogs slice."


class MainWindow(QMainWindow):
    """One window, one `App`, one operation at a time."""

    log_line = pyqtSignal(str)
    """Log records cross into Qt through this signal, so a record emitted from any thread
    lands on the UI thread before it touches a widget."""

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

        self.refresh()

    # --- layout ---------------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = QToolBar("Actions")
        bar.setMovable(False)
        self.addToolBar(bar)

        def button(label: str, slot=None, tooltip: str | None = None) -> QPushButton:
            found = QPushButton(label)
            if slot is not None:
                found.clicked.connect(slot)
            else:
                found.setEnabled(False)
                found.setToolTip(tooltip or DIALOGS_PENDING)
            bar.addWidget(found)
            return found

        self.add_button = button("Add Entry")
        self.sync_button = button("Sync", self.sync_selected)
        self.restore_button = button("Restore")
        self.pull_button = button("Pull", self.pull)
        self.push_button = button("Push", self.push)
        self.fetch_button = button("Fetch Status", self.fetch_status)
        self.check_button = button("Check Connection", self.check_connection)

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

        details = QWidget()
        details_layout = QVBoxLayout(details)
        for widget in (self.detail_name, self.detail_state, self.detail_path, self.detail_hint):
            details_layout.addWidget(widget)
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
        if row is None:
            self.detail_name.setText("No Entry selected")
            self.detail_state.setText("")
            self.detail_path.setText("")
            self.detail_hint.setText(
                "Set up a Cloud Vault and add an Entry to begin."
                if not self.app.config.is_set_up
                else ""
            )
            self.sync_button.setEnabled(False)
            return

        self.detail_name.setText(row.name)
        self.detail_state.setText(row.caption)

        if row.status is None:
            self.detail_path.setText("Not bound on this Machine.")
            self.detail_hint.setText("Bind it to a folder here to sync it.")
            self.sync_button.setEnabled(False)
            return

        binding = self.app.the_ledger.require(row.entry_id)
        self.detail_path.setText(str(binding.live))
        self.detail_hint.setText("")
        self.sync_button.setEnabled(Action.SYNC_TO_VAULT in row.status.offered)

    def _show_cloud(self) -> None:
        self.cloud_label.setText(presenter.cloud_caption(self.app.cloud, now=datetime.now(UTC)))
        online = self.app.cloud.offline is None
        ready = self.app.config.is_set_up
        self.pull_button.setEnabled(online and ready)
        self.push_button.setEnabled(online and ready)
        self.fetch_button.setEnabled(online and ready)
        self.check_button.setEnabled(ready)  # explicitly allowed while offline: the way back

    # --- operations: one at a time, each re-validated at the moment it runs ----------------

    def _pat(self) -> str | None:
        found = self.store.get_pat()
        if found is None:
            log().warning("No GitHub token is stored. Setup arrives with the dialogs slice.")
        return found

    def sync_selected(self) -> None:
        self.refresh()  # re-validate: the world may have moved since the button was drawn
        row = self._selected_row()
        if row is None or row.status is None:
            return
        if Action.SYNC_TO_VAULT not in row.status.offered:
            log().warning("%s no longer offers Sync; nothing was done.", row.name)
            return
        try:
            operations.sync_to_vault(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                row.entry_id,
            )
        except operations.SyncAborted as error:
            log().warning("%s", error)
        self.refresh()

    def _cloud_call(self, action) -> None:
        pat = self._pat()
        if pat is None:
            return
        try:
            action(pat)
        except (CloudOffline, PushRejected) as error:
            log().warning("%s", error)
        self.refresh()

    def fetch_status(self) -> None:
        self._cloud_call(self.app.cloud.fetch_status)

    def pull(self) -> None:
        def run(pat: str) -> None:
            pulled = self.app.cloud.pull(pat, self.app.description)
            if pulled.conflicts:
                log().warning(
                    "Merge Conflict on %d Entr%s. Resolution arrives with the dialogs slice.",
                    len(pulled.conflicts),
                    "y" if len(pulled.conflicts) == 1 else "ies",
                )
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
