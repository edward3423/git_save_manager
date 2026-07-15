"""What happens before the window opens, in order, and why the order.

1. **Writability** - refuse to run from somewhere read-only. Failing at the start of an
   operation is safe; failing in the middle is what the journal exists to clean up.
2. **The lock** - one instance only. Two instances running Git against one Vault and both
   writing the Ledger corrupts state.
3. **Recovery** - finish or undo a Live Save write a crash interrupted, from the journal.
4. **Invariant 2** - discard whatever a crashed operation left in the Vault's working tree.

An operation that crashed cannot be trusted to have cleaned up after itself - that is what
crashing means - so startup re-establishes every guarantee instead of assuming the last run
kept its promises.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config as config_module
from core import ledger, lock, transaction, vault
from core import paths as paths_module
from core.cloud import Cloud
from core.config import Config, MachineDescription
from core.ledger import Ledger
from core.lock import Lock
from core.paths import Paths
from core.transaction import Recovered


@dataclass
class App:
    """Everything the window needs, assembled once and shared by every operation.

    One `Cloud` per application: Offline Mode's stickiness lives in that object, and a
    caller constructing a fresh one per operation would silently disable it.
    """

    paths: Paths
    config: Config
    description: MachineDescription
    the_ledger: Ledger
    cloud: Cloud
    held: Lock
    recovered: Recovered | None

    def shutdown(self) -> None:
        self.held.release()


def start(paths: Paths, description: MachineDescription | None = None) -> App:
    """Bring the application up, or raise before anything is touched.

    `WorkspaceNotWritable` and `AlreadyRunning` are the two refusals; both fire before any
    state is read, let alone written.
    """
    paths_module.check_writable(paths)
    held = lock.acquire(paths)

    try:
        config = config_module.load(paths)
        recovered = transaction.recover(paths)

        # Only where a Vault already exists. `git init` here would make every later
        # bootstrap path think it is joining an existing repository.
        if (paths.vault_dir / ".git").exists():
            vault.ensure_clean(paths)

        return App(
            paths=paths,
            config=config,
            description=description or MachineDescription.detect(),
            the_ledger=ledger.load(paths),
            cloud=Cloud(paths=paths, config=config),
            held=held,
            recovered=recovered,
        )
    except BaseException:
        # Half a startup must not hold the lock: the user's next attempt has to run.
        held.release()
        raise
