import pytest

from core.paths import Paths, WorkspaceNotWritable, check_writable, is_root_user


def test_all_runtime_state_lives_under_data(tmp_path):
    paths = Paths(root=tmp_path)
    for path in (
        paths.config_file,
        paths.ledger_file,
        paths.journal_file,
        paths.lock_file,
        paths.backups_dir,
        paths.vault_dir,
    ):
        assert paths.data_dir in path.parents


def test_vault_is_a_sibling_of_local_state_not_its_parent(tmp_path):
    """ADR-0005: deleting or re-cloning the Vault must not take the Ledger or backups."""
    paths = Paths(root=tmp_path)
    assert paths.vault_dir not in paths.ledger_file.parents
    assert paths.vault_dir not in paths.backups_dir.parents
    assert paths.vault_dir not in paths.journal_file.parents
    assert paths.vault_dir.parent == paths.ledger_file.parent


def test_an_entrys_sidecar_is_a_sibling_of_its_content_not_inside_it(tmp_path):
    """ADR-0004, and Invariant 5. Restoring copies the content directory into the game's save
    folder verbatim, so anything we keep in there gets injected into the game's save folder
    on every restore."""
    paths = Paths(root=tmp_path)
    entry = "3f2a1b7c-0000-4000-8000-000000000001"

    content = paths.entry_content_dir(entry)
    sidecar = paths.entry_sidecar(entry)

    assert sidecar.parent == content.parent
    assert content not in sidecar.parents


def test_the_vault_layout_is_addressed_by_uuid(tmp_path):
    paths = Paths(root=tmp_path)
    entry = "3f2a1b7c-0000-4000-8000-000000000001"
    machine = "9c8b7a65-0000-4000-8000-000000000002"

    assert paths.entry_content_dir(entry).name == entry
    assert paths.machine_file(machine).name == f"{machine}.json"
    for path in (paths.entry_content_dir(entry), paths.machine_file(machine), paths.vault_marker):
        assert paths.vault_dir in path.parents


def test_check_writable_creates_the_data_dir(tmp_path):
    paths = Paths(root=tmp_path)
    check_writable(paths)
    assert paths.data_dir.is_dir()


@pytest.mark.skipif(is_root_user(), reason="root bypasses file permissions")
def test_check_writable_refuses_a_read_only_workspace(tmp_path):
    workspace = tmp_path / "readonly"
    workspace.mkdir()
    workspace.chmod(0o500)
    try:
        with pytest.raises(WorkspaceNotWritable):
            check_writable(Paths(root=workspace))
    finally:
        workspace.chmod(0o700)
