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
