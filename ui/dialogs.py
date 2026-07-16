"""Every dialog in the application. Rendering only - decisions live in core and presenter.

The one component that matters most here is `PreviewDialog`: **every** destructive flow -
Restore, conflict resolution toward the Vault, Backup restore - renders the same
`transaction.Preview` through the same widget (Invariant 7), and the approved preview
object is handed back to the executor, which refuses if the world moved since.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import backups, cloud, github, operations, redo, vault
from core.backups import Backup
from core.cloud import Side
from core.credentials import CredentialStore
from core.git import GitError, GitTimeout
from core.logger import log
from core.startup import App
from core.transaction import Preview
from ui import presenter


def _fail(parent: QWidget | None, title: str, error: Exception) -> None:
    log().warning("%s", error)
    QMessageBox.warning(parent, title, str(error))


# --- the preview component (Invariant 7) --------------------------------------------------------


class PreviewDialog(QDialog):
    """Exactly what will happen, before it does. OK means "do exactly this, or nothing"."""

    def __init__(self, title: str, lines: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 360)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(lines))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(buttons)

    @staticmethod
    def approve(title: str, preview: Preview, parent: QWidget | None = None) -> bool:
        """Approve a Live Save write: Restore, conflict-resolve, Backup restore."""
        return PreviewDialog.approve_lines(title, presenter.preview_lines(preview), parent)

    @staticmethod
    def approve_lines(title: str, lines: list[str], parent: QWidget | None = None) -> bool:
        """Approve any pre-rendered change list - used by the Vault-side Rollback preview,
        whose wording is not a Live Save write and so cannot go through `preview_lines`."""
        return PreviewDialog(title, lines, parent).exec() == QDialog.DialogCode.Accepted


# --- first-run setup ----------------------------------------------------------------------------


class SetupDialog(QDialog):
    """Repo and PAT in, one of the four bootstrap paths out.

    The PAT reaches the keyring only after GitHub has accepted it and the bootstrap has
    completed - a token that failed validation is not worth storing.
    """

    def __init__(self, app: App, store: CredentialStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.store = store
        self.setWindowTitle("Set up the Cloud Vault")

        self.repo_edit = QLineEdit()
        self.repo_edit.setPlaceholderText("owner/repository")
        self.pat_edit = QLineEdit()
        self.pat_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat_edit.setPlaceholderText("classic PAT with the repo scope")

        form = QFormLayout()
        form.addRow("GitHub repository", self.repo_edit)
        form.addRow("Personal access token", self.pat_edit)

        note = QLabel(
            "A private repository is created if it does not exist. An existing repository "
            "is joined only if it is a Vault; anything else is refused untouched."
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.run_bootstrap)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def run_bootstrap(self) -> None:
        repo = self.repo_edit.text().strip()
        token = self.pat_edit.text().strip()
        if "/" not in repo or not token:
            QMessageBox.warning(self, "Set up", "Both owner/repository and a token are needed.")
            return

        try:
            done = github.bootstrap(
                self.app.paths,
                self.app.config,
                self.app.description,
                api=github.RestApi(),
                token=token,
                repo=repo,
                adopt=self._offer_adoption,
            )
        except (
            github.BadToken,
            github.GitHubError,
            github.GitHubUnreachable,
            vault.NotAVault,
            vault.VaultTooNew,
            GitError,
            GitTimeout,
        ) as error:
            _fail(self, "Set up", error)
            return

        self.store.set_pat(token)
        if done.adopted is not None:
            operations.adopt_bindings(
                self.app.paths,
                self.app.the_ledger,
                done.adopted.get("bindings", {}),
                pat=token,
            )
        outcome = {
            github.BootstrapOutcome.CREATED: "Created a new private Cloud Vault.",
            github.BootstrapOutcome.ADOPTED_EMPTY: "Adopted the empty repository as the Vault.",
            github.BootstrapOutcome.JOINED: "Joined the existing Vault as a second Machine.",
        }[done.outcome]
        log().info("%s (branch: %s)", outcome, done.branch)
        self.accept()

    def _offer_adoption(self, ghost: dict) -> bool:
        """The Vault already lists a Machine with this hostname - almost always this very
        Machine, before a Redo Initialization. Reclaiming it avoids leaving a ghost."""
        bound = len(ghost.get("bindings", {}))
        answer = QMessageBox.question(
            self,
            "Adopt the old identity?",
            f"This Vault already lists a Machine named {ghost.get('hostname')} with {bound} "
            "published Binding(s). It is probably this Machine, before a Redo "
            "Initialization.\n\nReclaim that identity and its Bindings, instead of "
            "registering a new Machine beside it?",
        )
        return answer is QMessageBox.StandardButton.Yes


# --- adding and binding -------------------------------------------------------------------------


def _pick_directory(parent: QWidget, title: str) -> Path | None:
    found = QFileDialog.getExistingDirectory(parent, title)
    return Path(found) if found else None


def _pick_file(parent: QWidget, title: str) -> Path | None:
    found, _ = QFileDialog.getOpenFileName(parent, title)
    return Path(found) if found else None


def _browse_button(parent: QWidget, target: QLineEdit, title: str) -> QPushButton:
    """A Browse button that points `target` at a folder *or* a single file.

    A save is sometimes a directory and sometimes one file (a lone `.md`, an `.sav`), and the
    whole add/bind path downstream treats either the same. The native directory picker refuses
    files outright, so the choice is offered explicitly rather than guessed.
    """
    button = QPushButton("Browse...")
    menu = QMenu(button)

    def fill(picker: Callable[[QWidget, str], Path | None]) -> None:
        found = picker(parent, title)
        if found is not None:
            target.setText(str(found))

    menu.addAction("Folder...", lambda: fill(_pick_directory))
    menu.addAction("File...", lambda: fill(_pick_file))
    button.setMenu(menu)
    return button


class AddEntryDialog(QDialog):
    """Name a save and point at where it lives on this Machine."""

    def __init__(self, app: App, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Add Entry")

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Elden Ring")
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("the folder (or file) the game writes")
        browse = _browse_button(self, self.path_edit, "Where does the game keep this save?")

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit)
        path_row.addWidget(browse)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Live Save", path_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.run_add)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def run_add(self) -> None:
        name = self.name_edit.text().strip()
        raw = self.path_edit.text().strip()
        if not name or not raw:
            QMessageBox.warning(self, "Add Entry", "Both a name and a path are needed.")
            return
        try:
            operations.add_entry(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                name,
                Path(raw),
            )
        except (vault.FileTooLarge, OSError) as error:
            _fail(self, "Add Entry", error)
            return
        QMessageBox.information(
            self,
            "Add Entry",
            f"{name} was created, but no save data has been copied into the Vault yet. "
            "Adding an Entry only records where the save lives; Sync is what captures it.",
        )
        self.accept()


class BindDialog(QDialog):
    """Bind an Unlinked Entry here, with the other Machines' paths as read-only hints."""

    def __init__(
        self,
        app: App,
        entry_id: str,
        name: str,
        parent: QWidget | None = None,
        pat: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.pat = pat
        self.setWindowTitle(f"Bind {name}")
        self.setMinimumWidth(520)

        hints = presenter.bind_hints(app.paths, app.config, entry_id)
        hint_label = QLabel(
            "Other Machines keep this save at:\n" + "\n".join(f"  {h}" for h in hints)
            if hints
            else "No other Machine has published a path for this Entry."
        )
        hint_label.setWordWrap(True)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("where this Machine keeps (or will keep) the save")
        browse = _browse_button(self, self.path_edit, "Where does this Machine keep the save?")

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit)
        path_row.addWidget(browse)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.run_bind)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint_label)
        layout.addLayout(path_row)
        layout.addWidget(buttons)

    def run_bind(self) -> None:
        raw = self.path_edit.text().strip()
        if not raw:
            QMessageBox.warning(self, "Bind", "A path is needed.")
            return
        try:
            operations.bind_entry(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                self.entry_id,
                Path(raw),
                pat=self.pat,
            )
        except OSError as error:
            _fail(self, "Bind", error)
            return
        self.accept()


class SyncDialog(QDialog):
    """A one-line summary of what this Sync captures, before it is committed to the Vault.

    The summary is optional and only ever a note: it becomes the commit body, and History and
    the log surface it. The Sync itself is unchanged whether or not one is written.
    """

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sync {name} to the Vault")
        self.setMinimumWidth(460)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("boss slain, graphics tweaked, ... (optional)")

        form = QFormLayout()
        form.addRow("Summary", self.note_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @staticmethod
    def ask(name: str, parent: QWidget | None = None) -> str | None:
        """The user's summary (possibly empty) to Sync with, or None if they cancelled."""
        dialog = SyncDialog(name, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.note_edit.text().strip()


# --- conflicts: always two buttons, always the whole Entry --------------------------------------


def resolve_sync_conflict(app: App, entry_id: str, name: str, parent: QWidget) -> None:
    """A Sync Conflict: Live and Vault both moved since the Baseline. The human picks a side.

    Toward the Live Save is non-destructive (the Vault's version stays in history). Toward
    the Vault overwrites the Live Save, so that side goes through the preview and a Backup.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(f"Conflict: {name}")
    box.setText(
        f"Both this Machine's save and the Vault's version of {name} have changed.\n"
        "Take one side, whole. The losing side stays recoverable either way."
    )
    keep_live = box.addButton("Keep this Machine's save", QMessageBox.ButtonRole.AcceptRole)
    take_vault = box.addButton("Take the Vault's version", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()

    clicked = box.clickedButton()
    try:
        if clicked is keep_live:
            operations.resolve_conflict_toward_live(
                app.paths, app.config, app.description, app.the_ledger, entry_id
            )
        elif clicked is take_vault:
            preview = operations.preview_restore(
                app.paths, app.the_ledger, entry_id, reason=operations.CONFLICT_VAULT
            )
            if PreviewDialog.approve(f"Take the Vault's version of {name}", preview, parent):
                operations.resolve_conflict_toward_vault(
                    app.paths, app.config, app.the_ledger, entry_id, approved=preview
                )
    except (operations.SyncAborted, Exception) as error:  # noqa: BLE001 - surfaced, not hidden
        _fail(parent, "Resolve", error)


def resolve_merge_conflicts(app: App, conflicts: tuple[str, ...], parent: QWidget) -> None:
    """A Merge Conflict from a Pull: contested Entries, each taken whole from one side.

    Cancelling any choice aborts the whole merge - a half-resolved merge is never committed
    (and never left open, either).
    """
    from core import entries as entries_module

    for entry_id in conflicts:
        entry = entries_module.read(app.paths, entry_id)
        name = entry.name if entry else entry_id

        box = QMessageBox(parent)
        box.setWindowTitle(f"Merge Conflict: {name}")
        box.setText(
            f"Another Machine synced {name} while this one did too.\n"
            "Take one side, whole. Both lines of progress stay in history."
        )
        vault_side = box.addButton("Keep this Machine's line", QMessageBox.ButtonRole.AcceptRole)
        cloud_side = box.addButton("Take the other Machine's", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is vault_side:
            cloud.resolve_merge(app.paths, entry_id, Side.VAULT)
        elif clicked is cloud_side:
            cloud.resolve_merge(app.paths, entry_id, Side.CLOUD)
        else:
            cloud.abort_merge(app.paths)
            log().info("Merge aborted; nothing has changed. Pull again when ready.")
            return

    try:
        cloud.finish_merge(app.paths, app.config, app.description)
        log().info("Merge finished. Contested Entries now read Vault Ahead or In Sync.")
    except cloud.MergeUnfinished as error:
        _fail(parent, "Merge", error)


# --- Redo Initialization ------------------------------------------------------------------------


def run_redo(app: App, store: CredentialStore, parent: QWidget) -> bool:
    """The most destructive button in the app, and therefore the most explicit.

    The confirmation enumerates every path and keyring entry it will delete. A Vault that
    is Ahead of the Cloud refuses outright, and discarding those commits - the only place
    in the design where committed content can vanish - is a second, separate choice.
    """
    the_plan = redo.plan(app.paths)

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Redo Initialization")
    box.setText("\n".join(presenter.redo_lines(the_plan)))
    wipe = box.addButton("Delete and start over", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    if box.clickedButton() is not wipe:
        return False

    try:
        redo.execute(app.paths, store)
    except redo.VaultAhead as refusal:
        second = QMessageBox(parent)
        second.setIcon(QMessageBox.Icon.Critical)
        second.setWindowTitle("Unpushed commits")
        second.setText(
            f"{refusal}\n\nDiscarding destroys them permanently - they exist nowhere else."
        )
        discard = second.addButton("Discard the commits", QMessageBox.ButtonRole.DestructiveRole)
        second.addButton(QMessageBox.StandardButton.Cancel)
        second.exec()
        if second.clickedButton() is not discard:
            log().info("Redo Initialization refused: unpushed commits. Nothing was touched.")
            return False
        redo.execute(app.paths, store, discard_unpushed=True)

    app.reset()
    return True


# --- history, backups, machines, the log --------------------------------------------------------


class HistoryDialog(QDialog):
    """Every commit that touched this Entry, and Rollback as a forward commit."""

    def __init__(self, app: App, entry_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.name = name
        self.setWindowTitle(f"History: {name}")
        self.resize(640, 420)

        self.commits = operations.history(app.paths, entry_id)
        unpushed = operations.unpushed_commits(app.paths)
        self.listing = QListWidget()
        for commit in self.commits:
            message = f" {commit.body} " if commit.body else ""
            ahead = commit.sha in unpushed
            # A leading [unpushed] tag marks the local-ahead commits without relying on colour
            # alone; pushed rows carry no tag, so everything sits flush left rather than padded.
            marker = "[unpushed] " if ahead else ""
            item = QListWidgetItem(
                f"{marker}{commit.when:%Y-%m-%d %H:%M} |{message}| "
                f"{commit.machine} | {commit.subject} | [{commit.short}]"
            )
            if ahead:
                item.setForeground(QColor("#3d8bfd"))
                item.setToolTip("Local Ahead: this commit has not been pushed to the Cloud Vault.")
            self.listing.addItem(item)

        rollback = QPushButton("Roll back to selected...")
        rollback.clicked.connect(self.run_rollback)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(rollback)
        row.addStretch()
        row.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.listing)
        layout.addLayout(row)

    def run_rollback(self) -> None:
        index = self.listing.currentRow()
        if index < 0:
            return
        commit = self.commits[index]

        changes = operations.preview_rollback(self.app.paths, self.entry_id, commit.sha)
        if not changes:
            QMessageBox.information(
                self,
                "Roll back",
                f"{self.name} already holds {commit.short} ({commit.subject}). "
                "Nothing to roll back.",
            )
            return

        lines = presenter.rollback_lines(self.name, commit.short, changes)
        if PreviewDialog.approve_lines(f"Roll back {self.name} to {commit.short}", lines, self):
            operations.rollback(
                self.app.paths, self.app.config, self.app.description, self.entry_id, commit.sha
            )
            self.accept()


class BackupsDialog(QDialog):
    """Per-Entry Backups: restore through the one preview path, or reveal the raw zip."""

    def __init__(self, app: App, entry_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.name = name
        self.setWindowTitle(f"Backups: {name}")
        self.resize(560, 360)

        self.found: list[Backup] = backups.list_for(app.paths, entry_id)
        self.listing = QListWidget()
        for backup in self.found:
            self.listing.addItem(
                f"{backup.taken_at:%Y-%m-%d %H:%M}  {backup.reason:<16}  "
                f"{presenter.size_text(backup.size_bytes)}"
            )

        restore = QPushButton("Restore...")
        restore.clicked.connect(self.run_restore)
        reveal = QPushButton("Reveal in Explorer")
        reveal.clicked.connect(self.run_reveal)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(restore)
        row.addWidget(reveal)
        row.addStretch()
        row.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.listing)
        layout.addLayout(row)

    def _selected(self) -> Backup | None:
        index = self.listing.currentRow()
        return self.found[index] if 0 <= index < len(self.found) else None

    def run_restore(self) -> None:
        backup = self._selected()
        if backup is None:
            return
        try:
            preview = operations.preview_backup_restore(
                self.app.paths, self.app.the_ledger, self.entry_id, backup
            )
            if PreviewDialog.approve(f"Restore a Backup of {self.name}", preview, self):
                operations.restore_backup(
                    self.app.paths,
                    self.app.config,
                    self.app.the_ledger,
                    self.entry_id,
                    backup,
                    approved=preview,
                )
                self.accept()
        except Exception as error:  # noqa: BLE001 - surfaced, not hidden
            _fail(self, "Restore Backup", error)

    def run_reveal(self) -> None:
        backup = self._selected()
        if backup is None:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(backup.path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(backup.path)])
        else:
            subprocess.Popen(["xdg-open", str(backup.path.parent)])


class MachinesDialog(QDialog):
    """Every Machine that has published itself, and what it holds."""

    def __init__(self, app: App, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Machines")
        self.resize(560, 320)

        listing = QListWidget()
        for machine in vault.list_machines(app.paths):
            bound = len(machine.get("bindings", {}))
            marker = (
                "  (this Machine)" if machine.get("machine_id") == app.config.machine_id else ""
            )
            listing.addItem(
                f"{machine.get('hostname', '?')}  ({machine.get('os', '?')})  -  "
                f"{bound} bound  -  {machine.get('machine_id', '?')}{marker}"
            )

        layout = QVBoxLayout(self)
        layout.addWidget(listing)


class GitLogDialog(QDialog):
    """The Vault's full log, verbatim. The escape hatch view: nothing is hidden."""

    def __init__(self, app: App, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Full Git Log")
        self.resize(720, 480)

        # `%b` appends the author's note inline; commits without one leave only trailing
        # whitespace, so the note-carrying Syncs stand out without a blank line per commit.
        raw = vault.git(app.paths).run(
            "log",
            "--graph",
            "--date=short",
            "--format=%h %ad %an  %s  %b",
            "-100",
        )
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(raw)

        layout = QVBoxLayout(self)
        layout.addWidget(text)
