"""Every dialog in the application. Rendering only - decisions live in core and presenter.

The one component that matters most here is `PreviewDialog`: **every** destructive flow -
Restore, conflict resolution toward the Vault, Backup restore - renders the same
`transaction.Preview` through the same widget (Invariant 7), and the approved preview
object is handed back to the executor, which refuses if the world moved since.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionFrame,
    QVBoxLayout,
    QWidget,
)

from core import backups, cloud, device_flow, entries, github, ledger, operations, redo, vault
from core.backups import Backup
from core.cloud import Side
from core.credentials import CredentialStore
from core.entry_state import EntryState
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


# --- signing in, and first-run setup ------------------------------------------------------------


SIGN_IN_FIELD_WIDTH = 400  # the sign-in dialogs size to content; this sets their width


class GitHubSignIn(QWidget):
    """ "Sign in with GitHub" through the device flow, or a pasted token as the fallback.

    Signing in shows a short code, copies it to the clipboard, and opens github.com/login/device;
    the user approves there, and the token arrives by itself. The network work runs on a worker
    thread that touches no widget: each result crosses back through a signal tagged with the
    run it belongs to, so an answer from a cancelled or superseded run is simply dropped.

    The widget only *obtains* a token. Storing it is the caller's decision, after the caller
    has checked the token can do what it is for (`core.credentials` stores only good tokens).
    """

    PAT_OFF = "Use a personal access token instead"
    PAT_ON = "Sign in with GitHub instead"

    changed = pyqtSignal()
    """`token()` may have changed: signed in, signed out, or switched to the pasted token."""

    _code_ready = pyqtSignal(object, object)  # run, device_flow.DeviceCode
    _signed_in = pyqtSignal(object, str, str)  # run, token, login
    _failed = pyqtSignal(object, str)  # run, message

    def __init__(
        self,
        parent: QWidget | None = None,
        transport: device_flow.Transport | None = None,
        api: github.Api | None = None,
    ) -> None:
        super().__init__(parent)
        self.transport = transport or device_flow.FormTransport()
        self.api = api or github.RestApi()
        self._run: threading.Event | None = None
        self._token: str | None = None
        self._login: str | None = None

        self.sign_in_button = QPushButton("Sign in with GitHub")
        self.sign_in_button.clicked.connect(self.start)
        self.status = QLabel()
        self.status.setObjectName("signInStatus")
        self.status.setWordWrap(True)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.sign_in_button)
        top.addWidget(self.status, 1)

        # The code panel: shown only while waiting for the user to approve on GitHub.
        self.code_panel = QFrame()
        self.code_panel.setObjectName("signInCode")
        self.code_label = QLabel()
        self.code_label.setObjectName("signInCodeText")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_help = QLabel()
        self.code_help.setWordWrap(True)
        self.code_help.setObjectName("signInHelp")
        copy = QPushButton("Copy code")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.code_label.text()))
        self.open_button = QPushButton("Open GitHub")
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(copy)
        actions.addWidget(self.open_button)
        actions.addStretch()
        actions.addWidget(cancel)
        panel = QVBoxLayout(self.code_panel)
        panel.setContentsMargins(12, 10, 12, 10)
        panel.addWidget(self.code_help)
        panel.addWidget(self.code_label)
        panel.addLayout(actions)
        self.code_panel.hide()

        # A link-style toggle, not a checkbox: it names the other way, whichever is showing.
        self.use_pat = QPushButton(self.PAT_OFF)
        self.use_pat.setObjectName("linkButton")
        self.use_pat.setCheckable(True)
        self.use_pat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.use_pat.toggled.connect(self._toggle_pat)
        self.pat_edit = QLineEdit()
        self.pat_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat_edit.setPlaceholderText("classic PAT with the repo scope")
        self.pat_edit.textChanged.connect(self.changed)
        self.pat_edit.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.code_panel)
        layout.addWidget(self.use_pat, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.pat_edit)

        self._code_ready.connect(self._show_code)
        self._signed_in.connect(self._done)
        self._failed.connect(self._fail)
        self._show_idle()

    # --- what the caller reads -------------------------------------------------------------

    def token(self) -> str | None:
        if self.use_pat.isChecked():
            return self.pat_edit.text().strip() or None
        return self._token

    def login(self) -> str | None:
        """Who the signed-in token belongs to; None for a pasted token (validated later)."""
        return None if self.use_pat.isChecked() else self._login

    # --- the flow --------------------------------------------------------------------------

    def start(self) -> None:
        self.cancel()
        self._token = self._login = None
        run = threading.Event()
        self._run = run
        self.sign_in_button.setEnabled(False)
        self.status.setText("Asking GitHub for a code...")
        self.changed.emit()
        threading.Thread(target=self._work, args=(run,), name="github-sign-in", daemon=True).start()

    def cancel(self) -> None:
        """Stop waiting. Safe at any time, including after the dialog has closed."""
        if self._run is not None:
            self._run.set()
            self._run = None
            self._show_idle()

    def _work(self, run: threading.Event) -> None:
        try:
            code = device_flow.start(self.transport)
            self._emit(run, self._code_ready, code)
            token = device_flow.wait_for_token(self.transport, code, run.wait)
            if token is None:
                return  # cancelled
            self._emit(run, self._signed_in, token, github.viewer(self.api, token))
        except (device_flow.SignInFailed, github.GitHubUnreachable, github.BadToken) as error:
            self._emit(run, self._failed, str(error))

    @staticmethod
    def _emit(run: threading.Event, signal, *args) -> None:  # noqa: ANN001 - a bound signal
        if run.is_set():
            return
        # The dialog may close between the check and the emit; its signals go with it.
        with contextlib.suppress(RuntimeError):
            signal.emit(run, *args)

    def _show_code(self, run: threading.Event, code: device_flow.DeviceCode) -> None:
        if run is not self._run:
            return
        self.code_label.setText(code.user_code)
        QApplication.clipboard().setText(code.user_code)
        self.code_help.setText(
            f"Enter this code at {code.verification_uri} (it is already on your clipboard), "
            "then approve the request. This window carries on by itself."
        )
        with contextlib.suppress(TypeError):
            self.open_button.clicked.disconnect()
        url = QUrl(code.verification_uri)
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(url))
        QDesktopServices.openUrl(url)
        self.status.setText("Waiting for you to approve on GitHub...")
        self.code_panel.show()

    def _done(self, run: threading.Event, token: str, login: str) -> None:
        if run is not self._run:
            return
        self._run = None
        self._token, self._login = token, login
        self.code_panel.hide()
        self.sign_in_button.setText("Sign in again")
        self.sign_in_button.setEnabled(True)
        self.status.setText(f"Signed in as {login}.")
        self.changed.emit()

    def _fail(self, run: threading.Event, message: str) -> None:
        if run is not self._run:
            return
        self._run = None
        self._show_idle()
        log().warning("%s", message)
        self.status.setText(message)

    def _show_idle(self) -> None:
        self.code_panel.hide()
        self.sign_in_button.setText("Sign in with GitHub")
        self.sign_in_button.setEnabled(not self.use_pat.isChecked())
        self.status.setText("Not signed in.")

    def _toggle_pat(self, on: bool) -> None:
        self.cancel()
        self.use_pat.setText(self.PAT_ON if on else self.PAT_OFF)
        self.pat_edit.setVisible(on)
        self.sign_in_button.setEnabled(not on)
        self.status.setEnabled(not on)
        if on:
            self.pat_edit.setFocus()
        self.changed.emit()


class SetupDialog(QDialog):
    """Repo and a GitHub sign-in in, one of the four bootstrap paths out.

    The token reaches the keyring only after GitHub has accepted it and the bootstrap has
    completed - a token that failed validation is not worth storing.
    """

    def __init__(
        self,
        app: App,
        store: CredentialStore,
        parent: QWidget | None = None,
        sign_in: GitHubSignIn | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.store = store
        self.setWindowTitle("Set up the Cloud Vault")

        self.repo_edit = QLineEdit()
        self.repo_edit.setPlaceholderText("owner/repository")
        self.repo_edit.setMinimumWidth(SIGN_IN_FIELD_WIDTH)
        self.sign_in = sign_in or GitHubSignIn()

        form = QFormLayout()
        form.addRow("GitHub repository", self.repo_edit)
        form.addRow("GitHub account", self.sign_in)

        note = QLabel(
            "A private repository is created if it does not exist. An existing repository "
            "is joined only if it is a Vault; anything else is refused untouched."
        )
        note.setWordWrap(True)
        note.setObjectName("dimNote")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.run_bootstrap)
        buttons.rejected.connect(self.reject)
        self.repo_edit.textChanged.connect(self._refresh_ok)
        self.sign_in.changed.connect(self._refresh_ok)
        self._refresh_ok()

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(4)
        layout.addWidget(note)
        layout.addWidget(buttons)
        # The code panel appears and disappears; the dialog follows it instead of keeping
        # the waiting state's height as an empty gap once signed in.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

    def _refresh_ok(self) -> None:
        self.ok_button.setEnabled("/" in self.repo_edit.text() and self.sign_in.token() is not None)

    def done(self, result: int) -> None:
        self.sign_in.cancel()  # never leave a poll running behind a closed dialog
        super().done(result)

    def run_bootstrap(self) -> None:
        repo = self.repo_edit.text().strip()
        token = self.sign_in.token()
        if "/" not in repo or not token:
            QMessageBox.warning(self, "Set up", "Both owner/repository and a sign-in are needed.")
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


class SignInDialog(QDialog):
    """Sign this already set-up Machine in to GitHub again: a revoked or expired token, or a
    different account. The new token is stored only once it has reached the Cloud Vault, so a
    sign-in to the wrong account cannot replace a token that still works."""

    def __init__(
        self,
        app: App,
        store: CredentialStore,
        parent: QWidget | None = None,
        sign_in: GitHubSignIn | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.store = store
        self.setWindowTitle("Sign in to GitHub")

        intro = QLabel(
            f"Sign this Machine in to the GitHub account that can reach {app.config.repo}. "
            "Only the stored sign-in changes; the Vault and every save are untouched."
        )
        intro.setWordWrap(True)
        self.sign_in = sign_in or GitHubSignIn()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.run_save)
        buttons.rejected.connect(self.reject)
        self.sign_in.changed.connect(
            lambda: self.ok_button.setEnabled(self.sign_in.token() is not None)
        )
        self.ok_button.setEnabled(False)

        intro.setMinimumWidth(SIGN_IN_FIELD_WIDTH + 120)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.sign_in)
        layout.addWidget(buttons)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

    def done(self, result: int) -> None:
        self.sign_in.cancel()
        super().done(result)

    def run_save(self) -> None:
        token = self.sign_in.token()
        if token is None:
            return
        # The one probe that proves this token can reach this repository - and, succeeding,
        # it is also the explicit Check Connection that leaves Offline Mode.
        if not self.app.cloud.check_connection(token):
            who = self.sign_in.login()
            QMessageBox.warning(
                self,
                "Sign in",
                (f"Signed in as {who}, but" if who else "This token")
                + f" could not reach {self.app.config.repo}. The previous sign-in was kept."
                "\n\nSign in with the account that owns the repository (or has access to it).",
            )
            return
        self.store.set_pat(token)
        log().info(
            "Signed in to GitHub%s.", f" as {self.sign_in.login()}" if self.sign_in.login() else ""
        )
        self.accept()


# --- adding and binding -------------------------------------------------------------------------


def _pick_directory(parent: QWidget, title: str) -> Path | None:
    found = QFileDialog.getExistingDirectory(parent, title)
    return Path(found) if found else None


def _pick_file(parent: QWidget, title: str) -> Path | None:
    found, _ = QFileDialog.getOpenFileName(parent, title)
    return Path(found) if found else None


def _browse_button(
    parent: QWidget, target: QLineEdit, title: str, *, folder_only: bool = False
) -> QPushButton:
    """A Browse button that points `target` at a folder *or* a single file.

    A save is sometimes a directory and sometimes one file (a lone `.md`, an `.sav`), and the
    whole add/bind path downstream treats either the same. The native directory picker refuses
    files outright, so the choice is offered explicitly rather than guessed.

    `folder_only` collapses that choice to a plain directory picker, for when the caller already
    knows the file name and only needs the folder to put it in - see `BindDialog`. The directory
    picker is also the only one that can name a place that does not exist yet, via its own New
    Folder button; the file pickers can only open a file that is already there.
    """
    button = QPushButton("Browse...")

    def fill(picker: Callable[[QWidget, str], Path | None]) -> None:
        found = picker(parent, title)
        if found is not None:
            target.setText(str(found))

    if folder_only:
        button.clicked.connect(lambda: fill(_pick_directory))
        return button

    menu = QMenu(button)
    menu.addAction("Folder...", lambda: fill(_pick_directory))
    menu.addAction("File...", lambda: fill(_pick_file))
    button.setMenu(menu)
    return button


def _pick_files(parent: QWidget, title: str) -> list[Path]:
    found, _ = QFileDialog.getOpenFileNames(parent, title)
    return [Path(path) for path in found]


HIDDEN_FILES_TIP = (
    "Tip: dotfiles such as .yabairc are hidden in the file picker - press Cmd+Shift+. there to "
    "show them, or type the path."
    if sys.platform == "darwin"
    else "Tip: dotfiles such as .bashrc may be hidden in the file picker - you can type the path."
)


def _typed_path(text: str) -> Path:
    """A path as the user typed it, with `~` meaning their home, as it does in a terminal."""
    return Path(text.strip()).expanduser()


def _growing_form() -> QFormLayout:
    """A form whose fields take the dialog's whole width. On macOS a QFormLayout otherwise
    keeps every field at its natural width, leaving boxes too narrow to read a path in."""
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


class _PathRow(QWidget):
    """One path in the Add Entry dialog: a box, a Browse menu, and a way to take it out again."""

    def __init__(self, owner: AddEntryDialog) -> None:
        super().__init__()
        self.owner = owner
        self.edit = QLineEdit()
        self.edit.textChanged.connect(lambda _: owner.refresh())

        self.browse = QPushButton("Browse...")
        menu = QMenu(self.browse)
        self.folder_action = menu.addAction("Folder...", self._pick_folder)
        menu.addAction("Files...", self._pick_files)
        self.browse.setMenu(menu)

        self.remove = QPushButton("Remove")
        self.remove.setToolTip("Leave this file out of the Entry")
        self.remove.clicked.connect(lambda: owner.remove_row(self))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # One gap throughout. The macOS style otherwise puts more space between two buttons
        # than between the box and a button, so Remove stood apart from Browse....
        layout.setSpacing(8)
        layout.addWidget(self.edit)
        layout.addWidget(self.browse)
        layout.addWidget(self.remove)

    def _pick_folder(self) -> None:
        found = _pick_directory(self, "Where does the game keep this save?")
        if found is not None:
            self.edit.setText(str(found))

    def _pick_files(self) -> None:
        found = _pick_files(self, "Which file, or files, should this Entry keep?")
        if found:
            self.edit.setText(str(found[0]))
            for extra in found[1:]:
                self.owner.add_row().edit.setText(str(extra))


class AddEntryDialog(QDialog):
    """Name a save and point at where it lives on this Machine.

    One path is a file or folder Entry, as always. Several paths make a **File Set**: separate
    files, wherever they live, kept together as one Entry - a window manager's `.yabairc` and
    `.skhdrc`, say - so they are synced, restored and backed up as one.
    """

    def __init__(self, app: App, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Add Entry")

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Elden Ring")

        self.rows: list[_PathRow] = []
        # The rows live in a widget of their own, not a nested layout: a QFormLayout sizes a
        # widget field properly, but under-reports the height of a layout nested in it, and
        # the rows were squashed flat on macOS when a second one was added.
        rows_box = QWidget()
        self.rows_layout = QVBoxLayout(rows_box)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)

        self.add_row_button = QPushButton("Add Another File")
        self.add_row_button.setToolTip(
            "Keep several files - in any folders - together as this one Entry"
        )
        self.add_row_button.clicked.connect(lambda: self.add_row().edit.setFocus())

        self.set_note = QLabel(
            "These files become one Entry: synced, restored and backed up together. Each must "
            "be a file, and no two may share a name."
        )
        self.set_note.setObjectName("dimNote")
        self.set_note.setWordWrap(True)
        tip = QLabel(HIDDEN_FILES_TIP)
        tip.setObjectName("dimNote")
        tip.setWordWrap(True)
        self.notes = (self.set_note, tip)

        self.form = form = _growing_form()
        form.addRow("Name", self.name_edit)
        form.addRow("Live Save", rows_box)
        form.addRow("", self.add_row_button)
        # Direct form fields, so each wrapped note is given the height its wrapped lines need.
        form.addRow("", self.set_note)
        form.addRow("", tip)
        self.add_row_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.run_add)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(buttons)
        self.setMinimumWidth(680)

        self.add_row()

    def add_row(self) -> _PathRow:
        row = _PathRow(self)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        self.refresh()
        return row

    def remove_row(self, row: _PathRow) -> None:
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self.refresh()

    def refresh(self) -> None:
        """One row reads as a file or folder; more than one, as files only."""
        several = len(self.rows) > 1
        for row in self.rows:
            row.remove.setVisible(several)
            # A folder cannot be one of several files, so it is only offered alone.
            row.folder_action.setVisible(not several)
            row.edit.setPlaceholderText(
                "a file to keep in this Entry" if several else "the folder or file the game writes"
            )
        # The whole row, not just the label: a hidden field alone leaves its row's spacing.
        self.form.setRowVisible(self.set_note, several)
        if self.isVisible():
            # Once Qt has finished laying out the rows that just came or went.
            QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        """Size every wrapped note to the lines it wraps to at its actual width, then the
        window to its contents, keeping the width the user chose.

        Done by hand because Qt does neither here: a form estimates a wrapped label's height at
        a guessed width, so on macOS notes were clipped mid-line, and a window does not resize
        itself when rows come and go after it is shown.
        """
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        for note in self.notes:
            if note.width() > 0:
                note.setFixedHeight(note.heightForWidth(note.width()))
        layout.activate()
        self.resize(self.width(), layout.totalMinimumSize().height())

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        super().showEvent(event)
        self._fit_height()

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._fit_height()  # a new width wraps the notes differently

    def chosen(self) -> list[Path]:
        return [_typed_path(row.edit.text()) for row in self.rows if row.edit.text().strip()]

    def run_add(self) -> None:
        name = self.name_edit.text().strip()
        chosen = self.chosen()
        if not name or not chosen:
            QMessageBox.warning(self, "Add Entry", "Both a name and a path are needed.")
            return
        try:
            if len(chosen) == 1:
                operations.add_entry(
                    self.app.paths,
                    self.app.config,
                    self.app.description,
                    self.app.the_ledger,
                    name,
                    chosen[0],
                )
            else:
                operations.add_file_set(
                    self.app.paths,
                    self.app.config,
                    self.app.description,
                    self.app.the_ledger,
                    name,
                    chosen,
                )
        except (
            vault.FileTooLarge,
            operations.GitRepoNotAllowed,
            operations.InvalidFileSet,
            OSError,
        ) as error:
            _fail(self, "Add Entry", error)
            return
        QMessageBox.information(
            self,
            "Add Entry",
            f"{name} was created, but no save data has been copied into the Vault yet. "
            "Adding an Entry only records where the save lives; Sync is what captures it.",
        )
        self.accept()


class _StackScrollArea(QScrollArea):
    """A scrolling area sized to its content up to a cap, scrolling either way as needed.

    The height is a live size hint rather than a number captured at construction, so it is
    right after the stylesheet has been applied and whenever the fonts change. Beyond
    `max_height` the content scrolls vertically; wider than the viewport, it scrolls
    horizontally - the inner labels never wrap or elide, so a long path is always readable.
    """

    def __init__(self, max_height: int) -> None:
        super().__init__()
        self.max_height = max_height
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        inner = self.widget().sizeHint() if self.widget() else QSize(0, 0)
        bar = self.horizontalScrollBar().sizeHint().height()
        return QSize(inner.width(), min(inner.height() + bar, self.max_height))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())


class _HintCard(QFrame):
    """One other Machine's Binding, clickable as a whole: machine name over its path.

    The card is the button - mouse, Enter, and Space all emit `clicked` - so the path a user
    reads is the thing they press. The labels inside stay non-interactive on purpose: a
    selectable label would swallow the click. Copying the path moves to the context menu.
    """

    clicked = pyqtSignal()

    def __init__(self, hint: presenter.BindHint, tooltip: str) -> None:
        super().__init__()
        self.hint = hint
        self.setObjectName("bindHintCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip(tooltip)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.set_chosen(False)

        machine = QLabel(hint.machine)
        machine.setObjectName("bindHintMachine")
        path = QLabel(hint.path)
        path.setObjectName("bindHintPath")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(1)
        layout.addWidget(machine)
        layout.addWidget(path)

    def set_chosen(self, chosen: bool) -> None:
        """Mark the card whose path is the one now filled in. Styled by `[chosen="true"]`."""
        self.setProperty("chosen", chosen)
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def _menu(self, at) -> None:  # noqa: ANN001 - QPoint
        menu = QMenu(self)
        menu.addAction("Copy path", lambda: QApplication.clipboard().setText(self.hint.path))
        menu.exec(self.mapToGlobal(at))


class BindHintStrip(QWidget):
    """The other Machines' Bindings for one Entry, as a vertical stack of clickable cards.

    Clicking a card emits `use_requested` with that Machine's hint so the caller can pre-fill
    a Bind here with the same folder. The strip itself never binds anything - it only reports
    which hint was picked, and shows which one is currently in use (`mark_chosen`).
    """

    use_requested = pyqtSignal(object)  # presenter.BindHint
    MAX_HEIGHT = 200  # about four cards; more than that scrolls

    def __init__(
        self,
        hints: list[presenter.BindHint],
        tooltip: str = "Click to bind here using this folder",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        stack = QWidget()
        stack_layout = QVBoxLayout(stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(6)
        self.cards: list[_HintCard] = []
        for hint in hints:
            card = _HintCard(hint, tooltip)
            card.clicked.connect(lambda h=hint: self.use_requested.emit(h))
            stack_layout.addWidget(card)
            self.cards.append(card)
        stack_layout.addStretch()

        scroll = _StackScrollArea(self.MAX_HEIGHT)
        scroll.setObjectName("bindHints")
        scroll.setWidget(stack)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def mark_chosen(self, is_chosen: Callable[[presenter.BindHint], bool]) -> None:
        """Highlight every card `is_chosen` accepts - typed text can match a hint too."""
        for card in self.cards:
            card.set_chosen(is_chosen(card.hint))


def _line_edit_width_for(edit: QLineEdit, text: str) -> int:
    """The width `edit` needs to show `text` whole: Qt's own arithmetic for a line edit's size
    hint (text, its 2px side margins, the cursor), with the frame and stylesheet padding added
    by the style. Measured after polishing, so in the stylesheet's font, not the default."""
    edit.ensurePolished()
    metrics = edit.fontMetrics()
    margins = edit.textMargins()
    inner = QSize(
        metrics.horizontalAdvance(text) + 2 * 2 + 1 + margins.left() + margins.right(),
        metrics.height(),
    )
    option = QStyleOptionFrame()
    edit.initStyleOption(option)
    return (
        edit.style().sizeFromContents(QStyle.ContentsType.CT_LineEdit, option, inner, edit).width()
    )


class BindDialog(QDialog):
    """Bind an Unlinked Entry here; clicking another Machine's path fills it in."""

    MAX_PATH_WIDTH = 760  # beyond this the dialog would outgrow a laptop screen; the box scrolls

    def __init__(
        self,
        app: App,
        entry_id: str,
        name: str,
        parent: QWidget | None = None,
        pat: str | None = None,
        prefill: presenter.BindHint | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.pat = pat
        self.setWindowTitle(f"Bind {name}")

        hints = presenter.bind_hints(app.paths, app.config, entry_id)
        hint_label = QLabel(
            "Other Machines keep this save at - click one to use the same folder here:"
            if hints
            else "No other Machine has published a path for this Entry."
        )
        hint_label.setWordWrap(True)

        # A single-file save is bound as folder + the name the Vault already holds: the file
        # usually does not exist here yet, and a file picker can only open one that does.
        self.file_name = presenter.bind_file_name(app.paths, entry_id)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(
            "the folder this Machine keeps (or will keep) the file in"
            if self.file_name
            else "where this Machine keeps (or will keep) the save"
        )
        browse = _browse_button(
            self,
            self.path_edit,
            "Which folder does this Machine keep the file in?"
            if self.file_name
            else "Where does this Machine keep the save?",
            folder_only=bool(self.file_name),
        )

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
        self.strip: BindHintStrip | None = None
        if hints:
            self.strip = BindHintStrip(hints, tooltip="Click to fill in this folder")
            self.strip.use_requested.connect(self.use_hint)
            self.path_edit.textChanged.connect(self._mark_chosen_hint)
            layout.addWidget(self.strip)
            # Wide enough that a clicked hint reads whole in the box, not scrolled to its tail.
            longest = max(
                _line_edit_width_for(self.path_edit, h.suggested_folder(self.file_name))
                for h in hints
            )
            self.path_edit.setMinimumWidth(min(longest, self.MAX_PATH_WIDTH))
        if self.file_name:
            shape_label = QLabel(
                f"This save is a single file named {self.file_name}. Choose the folder this "
                "Machine keeps it in - the name is kept as it is."
            )
            shape_label.setWordWrap(True)
            layout.addWidget(shape_label)
        layout.addLayout(path_row)
        if self.file_name:
            # The full path is derived, not typed, so it is shown rather than made editable.
            self.preview_label = QLabel()
            self.preview_label.setObjectName("bindPreview")
            self.preview_label.setWordWrap(True)
            self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.path_edit.textChanged.connect(self._refresh_preview)
            self._refresh_preview()
            layout.addWidget(self.preview_label)
        layout.addWidget(buttons)
        # An explicit minimum on a window overrides the one its layout would impose, so take
        # the larger: a floor for short paths, and never narrower than the contents need.
        self.setMinimumWidth(max(520, layout.totalMinimumSize().width()))
        if prefill is not None:
            self.use_hint(prefill)

    def use_hint(self, hint: presenter.BindHint) -> None:
        """Pre-fill the folder box from another Machine's Binding. Still confirmed by OK."""
        self.path_edit.setText(hint.suggested_folder(self.file_name))
        self.path_edit.setFocus()

    def _mark_chosen_hint(self, text: str) -> None:
        typed = text.strip()
        self.strip.mark_chosen(lambda hint: hint.suggested_folder(self.file_name) == typed)

    def _refresh_preview(self) -> None:
        chosen = self.chosen_path()
        self.preview_label.setText(
            f"Live Save: {chosen}" if chosen else "Live Save: (choose a folder above)"
        )

    def chosen_path(self) -> Path | None:
        """The Live Save path this dialog would bind, or None while nothing is chosen."""
        raw = self.path_edit.text().strip()
        if not raw:
            return None
        return Path(raw) / self.file_name if self.file_name else Path(raw)

    def run_bind(self) -> None:
        raw = self.chosen_path()
        if raw is None:
            QMessageBox.warning(self, "Bind", "A path is needed.")
            return
        if self.file_name and raw.exists():
            # Both cases are legitimate: the save is already here and is being adopted, or it is
            # a stale copy about to be replaced. Say which way the data will move, and go on.
            answer = QMessageBox.question(
                self,
                "Bind",
                f"{raw} already exists.\n\nBinding does not move any data. Afterwards, Sync "
                "uploads this file to the Vault and Restore overwrites it with the Vault's "
                "copy.\n\nBind here anyway?",
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        try:
            operations.bind_entry(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                self.entry_id,
                raw,
                pat=self.pat,
            )
        except OSError as error:
            _fail(self, "Bind", error)
            return
        self.accept()


class _MemberRow(QWidget):
    """One File Set member in the Bind dialog: its name, where it lives here, and a Browse."""

    def __init__(self, member: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.member = member
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(f"where this Machine keeps (or will keep) {member}")

        browse = QPushButton("Browse...")
        menu = QMenu(browse)
        # The folder picker is the one that can name a file that does not exist here yet: the
        # member's name is appended, which is what every other Machine's copy is called.
        menu.addAction("Folder (keeps the name)...", self._pick_folder)
        menu.addAction("File...", self._pick_file)
        browse.setMenu(menu)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        layout.addWidget(browse)

    def _pick_folder(self) -> None:
        found = _pick_directory(self, f"Which folder does this Machine keep {self.member} in?")
        if found is not None:
            self.edit.setText(str(found / self.member))

    def _pick_file(self) -> None:
        found = _pick_file(self, f"Which file is {self.member} on this Machine?")
        if found is not None:
            self.edit.setText(str(found))

    def chosen(self) -> Path | None:
        return _typed_path(self.edit.text()) if self.edit.text().strip() else None


class BindFilesDialog(QDialog):
    """Bind a File Set here, one path per file; clicking another Machine fills them all in.

    Each file is bound separately because each may live somewhere else on this Machine - the
    same `skhdrc` sits at `~/.skhdrc` on one Machine and `~/.config/skhd/skhdrc` on another.
    For a File Set already bound here that Needs Binding, only the files without a path are
    asked for; the rest are listed, and kept.
    """

    MAX_PATH_WIDTH = BindDialog.MAX_PATH_WIDTH
    MAX_KEPT_WIDTH = 520

    def __init__(
        self,
        app: App,
        entry_id: str,
        name: str,
        parent: QWidget | None = None,
        pat: str | None = None,
        prefill: presenter.BindHint | None = None,
    ) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.pat = pat
        self.setWindowTitle(f"Bind {name}")

        entry = entries.require(app.paths, entry_id)
        binding = app.the_ledger.get(entry_id)
        bound = ledger.live_members(entry, binding) if binding is not None else {}
        wanted = [member for member in entry.files or () if member not in bound]

        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{name} is {len(entry.files or ())} files kept together. Choose where this Machine "
            "keeps each one - they can be in different folders, and need not exist yet."
            if not bound
            else f"{', '.join(wanted)} {'was' if len(wanted) == 1 else 'were'} added to {name} "
            "on another Machine. Choose where this Machine keeps "
            f"{'it' if len(wanted) == 1 else 'them'}."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        hints = [h for h in presenter.bind_hints(app.paths, app.config, entry_id) if h.files]
        self.strip: BindHintStrip | None = None
        if hints:
            hint_label = QLabel("Other Machines keep these files at - click one to use the same:")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)
            self.strip = BindHintStrip(hints, tooltip="Click to fill in these paths")
            self.strip.use_requested.connect(self.use_hint)
            layout.addWidget(self.strip)

        form = _growing_form()
        for member, path in bound.items():
            # Already bound and kept as is: shown, cut in the middle when long, never edited.
            kept = QLabel()
            kept.setObjectName("bindPreview")
            kept.ensurePolished()
            kept.setText(
                kept.fontMetrics().elidedText(
                    str(path), Qt.TextElideMode.ElideMiddle, self.MAX_KEPT_WIDTH
                )
            )
            kept.setToolTip(str(path))
            form.addRow(member, kept)
        self.rows = [_MemberRow(member, self) for member in wanted]
        for row in self.rows:
            row.edit.textChanged.connect(self._mark_chosen_hint)
            form.addRow(row.member, row)
        layout.addLayout(form)

        if hints and self.rows:
            longest = max(
                _line_edit_width_for(self.rows[0].edit, path)
                for hint in hints
                for path in (hint.files or {}).values()
            )
            for row in self.rows:
                row.edit.setMinimumWidth(min(longest, self.MAX_PATH_WIDTH))

        tip = QLabel(HIDDEN_FILES_TIP)
        tip.setObjectName("dimNote")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.run_bind)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(max(560, layout.totalMinimumSize().width()))

        if prefill is not None:
            self.use_hint(prefill)

    def use_hint(self, hint: presenter.BindHint) -> None:
        """Fill every file's box from another Machine's Binding. Still confirmed by OK."""
        for row in self.rows:
            if hint.files and row.member in hint.files:
                row.edit.setText(hint.files[row.member])
        if self.rows:
            self.rows[0].edit.setFocus()

    def _mark_chosen_hint(self, _text: str = "") -> None:
        if self.strip is None:
            return
        typed = {row.member: row.edit.text().strip() for row in self.rows}
        self.strip.mark_chosen(
            lambda hint: all((hint.files or {}).get(m) == text for m, text in typed.items())
        )

    def run_bind(self) -> None:
        chosen = {row.member: row.chosen() for row in self.rows}
        missing = [member for member, path in chosen.items() if path is None]
        if missing:
            QMessageBox.warning(self, "Bind", f"A path is needed for {', '.join(missing)}.")
            return
        existing = [str(path) for path in chosen.values() if path is not None and path.exists()]
        if existing:
            answer = QMessageBox.question(
                self,
                "Bind",
                "Already here:\n" + "\n".join(existing) + "\n\nBinding does not move any data. "
                "Afterwards, Sync uploads these files to the Vault and Restore overwrites them "
                "with the Vault's copies.\n\nBind here anyway?",
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        try:
            operations.bind_entry(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                self.entry_id,
                {member: path for member, path in chosen.items() if path is not None},
                pat=self.pat,
            )
        except (operations.InvalidFileSet, vault.FileTooLarge, OSError) as error:
            _fail(self, "Bind", error)
            return
        self.accept()


class FilesDialog(QDialog):
    """The files an Entry keeps together: add one, or take one out.

    Each change is its own commit, made the moment it is confirmed - like Rename, it moves no
    save data. A file added here is captured by the next Sync. A file taken out leaves the
    Vault for every Machine but stays in History, and the file itself is never touched.
    """

    def __init__(self, app: App, entry_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.entry_id = entry_id
        self.name = name
        self.setWindowTitle(f"Files of {name}")

        self.listing = QListWidget()
        self.listing.setObjectName("filesList")
        # Paths of one Entry often share a long prefix, so the middle is what gets cut: each
        # row keeps its file name in front and the path's own tail, with the rest on hover.
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listing.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.listing.currentRowChanged.connect(lambda _: self._refresh_buttons())

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("a file to keep in this Entry too")
        self.path_edit.textChanged.connect(lambda _: self._refresh_buttons())
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        self.add_button = QPushButton("Add File")
        self.add_button.clicked.connect(self.run_add)
        add_row = QHBoxLayout()
        add_row.addWidget(self.path_edit)
        add_row.addWidget(browse)
        add_row.addWidget(self.add_button)

        tip = QLabel(HIDDEN_FILES_TIP)
        tip.setObjectName("dimNote")
        tip.setWordWrap(True)

        self.remove_button = QPushButton("Remove from Entry")
        self.remove_button.clicked.connect(self.run_remove)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.remove_button)
        bottom.addStretch()
        bottom.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.listing)
        layout.addLayout(add_row)
        layout.addWidget(tip)
        layout.addLayout(bottom)
        self.resize(620, 340)
        self._load()

    def _load(self) -> None:
        self.members: list[str] = []
        self.listing.clear()
        entry = entries.require(self.app.paths, self.entry_id)
        lines = presenter.live_lines(self.app.paths, self.app.the_ledger, self.entry_id)
        if entry.is_file_set:
            self.members = list(entry.files or ())
            names = self.members
        else:
            names = [entry.content_name or Path(lines[0]).name]
        for name, line in zip(names, lines, strict=True):
            shown = line if line.startswith(name) else f"{name}    {line}"
            item = QListWidgetItem(shown)
            item.setToolTip(line)
            self.listing.addItem(item)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.add_button.setEnabled(bool(self.path_edit.text().strip()))
        # The last file stays: an Entry with none is Remove from Vault, which says so.
        self.remove_button.setEnabled(len(self.members) > 1 and self.listing.currentRow() >= 0)

    def _browse(self) -> None:
        found = _pick_file(self, f"Which file should {self.name} keep too?")
        if found is not None:
            self.path_edit.setText(str(found))

    def run_add(self) -> None:
        path = _typed_path(self.path_edit.text())
        try:
            operations.add_file(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                self.entry_id,
                path,
            )
        except (operations.InvalidFileSet, vault.FileTooLarge, OSError) as error:
            _fail(self, "Add File", error)
            return
        self.path_edit.clear()
        self._load()

    def run_remove(self) -> None:
        index = self.listing.currentRow()
        if not 0 <= index < len(self.members):
            return
        member = self.members[index]
        answer = QMessageBox.question(
            self,
            "Remove from Entry",
            f"Take {member} out of {self.name}, for every Machine?\n\n"
            "The file itself is not touched, here or anywhere. Its copy leaves the Vault and "
            "stays recoverable from History.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        try:
            operations.remove_file(
                self.app.paths,
                self.app.config,
                self.app.description,
                self.app.the_ledger,
                self.entry_id,
                member,
            )
        except operations.InvalidFileSet as error:
            _fail(self, "Remove from Entry", error)
            return
        self._load()


class SyncDialog(QDialog):
    """A one-line summary of what this Sync captures, before it is committed to the Vault.

    The summary is optional and only ever a note: it becomes the commit body, and History and
    the log surface it. The Sync itself is unchanged whether or not one is written.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("boss slain, graphics tweaked, ... (optional)")

        form = _growing_form()
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
        return SyncDialog._ask(f"Sync {name} to the Vault", parent)

    @staticmethod
    def ask_all(count: int, parent: QWidget | None = None) -> str | None:
        """One summary for a whole sweep, asked once rather than once per Entry.

        Each Entry still gets its own commit; they simply share a body. Asking per Entry would
        make the note the slowest part of a bulk action and train the user to empty it.
        """
        plural = "Entry" if count == 1 else "Entries"
        return SyncDialog._ask(f"Push {count} {plural} to the Vault", parent)

    @staticmethod
    def _ask(title: str, parent: QWidget | None = None) -> str | None:
        dialog = SyncDialog(title, parent)
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


# --- showing a file in the platform's file manager -----------------------------------------------


REVEAL_LABEL = {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(
    sys.platform, "Open Folder"
)


def reveal(path: Path) -> None:
    """Select `path` in Finder or Explorer; elsewhere there is no portable "select", so the
    folder holding it is opened instead (hence the label)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _home_relative(path: Path) -> str:
    """`~/Downloads/Hades` rather than the full path: short enough to read in a message box."""
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def show_downloaded(parent: QWidget, name: str, landed: Path) -> None:
    """Say where a Download landed, and that it is now just a copy the app will not touch."""
    box = QMessageBox(parent)
    box.setWindowTitle("Downloaded")
    box.setText(f"{name} was downloaded.")
    box.setInformativeText(
        f"Saved as {_home_relative(landed)}\n\nThis is a plain copy of the Vault's version. It "
        "is not bound, and Sync and Restore will never touch it."
    )
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    show = box.addButton(REVEAL_LABEL, QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is show:
        reveal(landed)


# --- history, backups, machines, the log --------------------------------------------------------


class _RowColorDelegate(QStyledItemDelegate):
    """Paint each row's own Background/Foreground brush.

    Qt drops a model's per-item brushes the moment a `QListWidget::item` stylesheet rule
    exists (as the app theme has one), so `setBackground`/`setForeground` are otherwise
    silently ignored and every row falls back to the theme colour. This draws the row itself -
    selection, per-row background, per-row text colour - restoring those brushes.
    """

    _SELECTED_BG = QColor("#4f8cc9")
    _SELECTED_FG = QColor("#ffffff")
    _DEFAULT_FG = QColor("#d6d8dc")

    def paint(self, painter, option, index):  # noqa: ANN001 - Qt override
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, self._SELECTED_BG)
            pen = self._SELECTED_FG
        else:
            background = index.data(Qt.ItemDataRole.BackgroundRole)
            if background is not None:
                painter.fillRect(option.rect, background.color())
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            pen = foreground.color() if foreground is not None else self._DEFAULT_FG
        painter.setPen(pen)
        text_rect = option.rect.adjusted(8, 0, -8, 0)  # the QSS item padding, kept
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            index.data(Qt.ItemDataRole.DisplayRole),
        )
        painter.restore()

    def sizeHint(self, option, index):  # noqa: ANN001 - Qt override
        size = super().sizeHint(option, index)
        size.setHeight(size.height() + 10)  # match the theme's roomy item padding
        return size


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

        # The Live Save's standing against the Vault, when this Entry is bound here: In Sync
        # highlights the current commit, Local Ahead shows an uncommitted row above it all.
        state = None
        if app.the_ledger.is_bound(entry_id):
            state = ledger.refresh(app.paths, app.the_ledger, entry_id).state
        in_sync = state is EntryState.IN_SYNC
        local_ahead = state is EntryState.LOCAL_AHEAD
        current = self.commits[0] if self.commits else None

        # One slot per list row, holding the Commit it represents - or None for the synthetic
        # "uncommitted" row, which is not a commit and so can never be a Rollback target.
        self.rows: list[operations.Commit | None] = []
        self.listing = QListWidget()
        self.listing.setItemDelegate(_RowColorDelegate(self.listing))

        if local_ahead:
            uncommitted = QListWidgetItem(
                "[uncommitted]  the Live Save has changes not yet Synced to the Vault"
            )
            uncommitted.setForeground(QColor("#e5484d"))
            self.listing.addItem(uncommitted)
            self.rows.append(None)

        for commit in self.commits:
            message = f" {commit.body} " if commit.body else ""
            ahead = commit.sha in unpushed
            is_current = commit is current
            # Tags sit on the left, without relying on colour alone: [unpushed] for a commit the
            # Cloud Vault lacks, [current] for the version the Vault holds now.
            tags = ("[unpushed] " if ahead else "") + ("[current] " if is_current else "")
            item = QListWidgetItem(
                f"{tags}{commit.when:%Y-%m-%d %H:%M} |{message}| "
                f"{commit.machine} | {commit.subject} | [{commit.short}]"
            )
            if is_current and in_sync:
                # In Sync: the Live Save matches this commit exactly. Colour the text orange.
                item.setForeground(QColor("#f5a623"))
                item.setToolTip("In Sync: the Live Save matches this commit.")
            elif ahead:
                item.setForeground(QColor("#3d8bfd"))
                item.setToolTip("Local Ahead: this commit has not been pushed to the Cloud Vault.")
            self.listing.addItem(item)
            self.rows.append(commit)

        self.rollback_button = QPushButton("Roll back to selected...")
        self.rollback_button.setEnabled(False)  # nothing selected yet
        self.rollback_button.clicked.connect(self.run_rollback)
        self.listing.currentRowChanged.connect(self._refresh_rollback_enabled)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(self.rollback_button)
        row.addStretch()
        row.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.listing)
        layout.addLayout(row)

    def _selected_commit(self) -> operations.Commit | None:
        index = self.listing.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _refresh_rollback_enabled(self) -> None:
        """Rollback is offered only for an *older* commit: never the uncommitted row (not a
        commit), and never the current one (rolling back to where the Vault already is)."""
        commit = self._selected_commit()
        current = self.commits[0] if self.commits else None
        self.rollback_button.setEnabled(commit is not None and commit is not current)

    def run_rollback(self) -> None:
        commit = self._selected_commit()
        if commit is None or (self.commits and commit is self.commits[0]):
            return

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
        reveal = QPushButton(REVEAL_LABEL)
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
        if backup is not None:
            reveal(backup.path)


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
