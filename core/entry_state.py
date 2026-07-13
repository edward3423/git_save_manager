"""What state is this Entry in, and what may we do about it?

An Entry's state is *derived*, never remembered (ADR-0001). Three inputs, all content
hashes, any of which may be `None` for "no content":

- **Live** - what is on disk in the game's save folder, right now.
- **Vault** - what is checked out in the local Git repo, right now.
- **Baseline** - what the content was at the last completed Sync. The only thing we
  *remember*, and the only thing that lets us attribute a difference to a side.

Comparing only Live against Vault can tell you *that* they differ but never *which one
moved*, and moving the wrong one destroys a save. The Baseline is what turns "they differ"
into "the Live Save changed and the Vault did not".

## Absence never propagates

The table in the plan treats absence as just another hash value. That is right while there
is no Baseline - a brand-new Entry has no Vault content yet, and a freshly bound Entry on a
second Machine has no Live Save yet - but it is *dangerous* once a Baseline exists, because
then absence means something was **destroyed or disconnected**, and the plain three-way
answer is to propagate that destruction to the other copy:

- Live Save gone (external drive unplugged, game uninstalled), Vault unchanged. The
  three-way table sees "Live changed, Vault did not" and reports **Local Ahead**, offering
  to Sync - which would commit the absence and erase the Entry's content in the Vault.
  Violates Invariant 3, the Vault is append-only.
- Vault content gone (another Machine removed the Entry), Live unchanged. The table sees
  "Vault changed, Live did not" and reports **Vault Ahead**, offering to Restore - which
  would write nothing over a real Live Save and delete it. Violates Invariant 1, the
  application never deletes a Live Save.

So absence, where a Baseline says there was once content, is its own state and never
evidence to change the other side. Content is only ever destroyed by an explicit act:
Remove from Vault, or the user's own hand. Two rules make this structural rather than a
property of six separate branches, and they are enforced last, for every state without
exception:

    Sync to Vault is never offered when the Live Save has no content.
    Restore to Live is never offered when the Vault has no content.

The state space here is finite and tiny - only the *equality pattern* of three values
matters - so the tests enumerate it exhaustively rather than by example.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from core.hashing import HashCache, content_hash


class EntryState(StrEnum):
    IN_SYNC = "in_sync"
    LOCAL_AHEAD = "local_ahead"
    VAULT_AHEAD = "vault_ahead"
    CONFLICT = "conflict"

    # The three states absence produces. All of them mean "something is gone"; none of them
    # may be answered by making it gone somewhere else too.
    LIVE_SAVE_MISSING = "live_save_missing"
    REMOVED_FROM_VAULT = "removed_from_vault"
    NO_CONTENT = "no_content"


class Action(StrEnum):
    SYNC_TO_VAULT = "sync_to_vault"
    RESTORE_TO_LIVE = "restore_to_live"
    RESOLVE = "resolve"  # a Sync Conflict: the human picks a side, at Entry granularity
    UNBIND = "unbind"  # drops the Binding only; never touches a Live Save or the Vault


@dataclass(frozen=True)
class EntryStatus:
    """The derived state of one Entry, plus what the user may do about it."""

    state: EntryState
    live: str | None
    vault: str | None
    baseline: str | None

    offered: tuple[Action, ...]
    """Actions this state offers, most relevant first. Everything else stays greyed out."""

    recommended: Action | None
    """The safe default, or `None` where no default is safe and the human must choose."""

    baseline_repair: str | None = None
    """Set when a lost Baseline should be healed to this value. See `evaluate`."""


EVERYTHING = (Action.SYNC_TO_VAULT, Action.RESTORE_TO_LIVE, Action.UNBIND)
"""What the three absence states offer *before* the guard subtracts the impossible.

They do not hand-pick their actions. They ask for everything and let `_guard` remove what
has no content to act on, which is the whole of what makes those states safe - so the guard
is load-bearing rather than a backstop that never fires and is therefore never tested.
Delete it and all three states immediately offer to destroy data.
"""


def _guard(offered: tuple[Action, ...], live: str | None, vault: str | None) -> tuple[Action, ...]:
    """Strip any action that would propagate an absence. Invariants 1 and 3, structurally."""
    forbidden = set()
    if live is None:
        forbidden.add(Action.SYNC_TO_VAULT)
    if vault is None:
        forbidden.add(Action.RESTORE_TO_LIVE)
    return tuple(action for action in offered if action not in forbidden)


def _status(
    state: EntryState,
    live: str | None,
    vault: str | None,
    baseline: str | None,
    offered: tuple[Action, ...],
    recommended: Action | None = None,
    baseline_repair: str | None = None,
) -> EntryStatus:
    offered = _guard(offered, live, vault)
    if recommended is not None and recommended not in offered:
        recommended = None
    return EntryStatus(
        state=state,
        live=live,
        vault=vault,
        baseline=baseline,
        offered=offered,
        recommended=recommended,
        baseline_repair=baseline_repair,
    )


def evaluate(live: str | None, vault: str | None, baseline: str | None) -> EntryStatus:
    """Derive an Entry's state from three content hashes. Pure: reads nothing, writes nothing.

    The order of the checks is load-bearing.
    """
    # Nothing exists anywhere. Not In Sync - there is no content to be in sync *about* - and
    # emphatically not an invitation to copy that nothingness in either direction. Usually a
    # Binding pointing at a path the game has not created yet, or a wrong path.
    if live is None and vault is None:
        # The guard leaves only Unbind standing: there is nothing to copy in either direction.
        return _status(EntryState.NO_CONTENT, live, vault, baseline, offered=EVERYTHING)

    # The equality short-circuit, and it runs *first*.
    #
    # Live == Vault is definitionally the post-condition of a completed Sync, so if we see it
    # while the Baseline says otherwise, the Baseline is not stale evidence - it is a *lost
    # record* of a Sync that demonstrably finished (a crash between "files written" and
    # "Baseline recorded"). Healing it is recovering what was lost, not inventing one, which
    # is why this does not breach Invariant 6: a completed Sync remains the only writer of a
    # *new* Baseline. Without this, that crash resurfaces forever as a false Conflict.
    #
    # It also means binding an Entry whose live path already matches the Vault needs no
    # prompt at all: the answer is simply "these are the same, nothing to do".
    if live is not None and live == vault:
        return _status(
            EntryState.IN_SYNC,
            live,
            vault,
            baseline,
            offered=(),
            baseline_repair=None if baseline == live else live,
        )

    # From here the two sides genuinely differ, so exactly one of them may be absent.

    if vault is None:
        if baseline is None:
            # Never synced, so the Vault is not missing anything - it has simply never held
            # this Entry. A new Entry, waiting for its first Sync.
            return _status(
                EntryState.LOCAL_AHEAD,
                live,
                vault,
                baseline,
                offered=(Action.SYNC_TO_VAULT,),
                recommended=Action.SYNC_TO_VAULT,
            )
        # A Baseline exists, so the Vault *did* hold this Entry and now does not: another
        # Machine removed it. Re-adding and unbinding are both defensible and we cannot know
        # which was meant, so nothing is recommended and nothing is cleaned up silently.
        #
        # The guard removes Restore to Live, which would delete the Live Save (Invariant 1),
        # leaving re-add and unbind.
        return _status(EntryState.REMOVED_FROM_VAULT, live, vault, baseline, offered=EVERYTHING)

    if live is None:
        if baseline is None:
            # Never synced here: an Entry another Machine published, freshly bound on this
            # one, whose save folder does not exist yet. The ordinary second-machine path.
            return _status(
                EntryState.VAULT_AHEAD,
                live,
                vault,
                baseline,
                offered=(Action.RESTORE_TO_LIVE,),
                recommended=Action.RESTORE_TO_LIVE,
            )
        # A Baseline exists, so this Live Save was there and now is not. An unplugged drive
        # and a deliberate deletion look identical from here, and restoring over the former
        # would be wrong, so we recommend nothing and let the human decide.
        #
        # The guard removes Sync to Vault, which would commit the absence and erase the
        # Entry's content in the Vault (Invariant 3), leaving restore and unbind.
        return _status(EntryState.LIVE_SAVE_MISSING, live, vault, baseline, offered=EVERYTHING)

    # Both sides have content and they differ. Now the Baseline earns its keep: it says which
    # side moved. With no Baseline at all, both sides read as changed, which is the honest
    # answer - binding onto a non-empty live path whose content differs is a genuine Conflict,
    # and the user is asked for a direction.
    live_moved = live != baseline
    vault_moved = vault != baseline

    if live_moved and not vault_moved:
        return _status(
            EntryState.LOCAL_AHEAD,
            live,
            vault,
            baseline,
            offered=(Action.SYNC_TO_VAULT,),
            recommended=Action.SYNC_TO_VAULT,
        )

    if vault_moved and not live_moved:
        # The scenario a naive design gets fatally wrong. The Live Save is untouched since
        # the last Sync and the Vault has moved on - because a Pull brought down another
        # Machine's work. A design that advanced the Baseline on Pull would land here with
        # Baseline == Vault instead, read "Local Ahead", and offer to Sync the stale Live
        # Save over the other Machine's progress - and then push it.
        return _status(
            EntryState.VAULT_AHEAD,
            live,
            vault,
            baseline,
            offered=(Action.RESTORE_TO_LIVE,),
            recommended=Action.RESTORE_TO_LIVE,
        )

    # Both moved. Only a human can say which save is the one they want to keep, and the
    # choice is all-of-one-side or all-of-the-other: a per-file mixture yields a save that
    # never existed on any machine.
    return _status(
        EntryState.CONFLICT,
        live,
        vault,
        baseline,
        offered=(Action.RESOLVE,),
        recommended=Action.RESOLVE,
    )


def evaluate_entry(
    live_path: Path,
    vault_path: Path,
    baseline: str | None,
    cache: HashCache | None = None,
) -> EntryStatus:
    """Read both sides from disk and derive the state.

    The caller decides whether to pass a cache: the status refresh does, so that focusing the
    window does not re-read every managed byte; the Sync path never does, because its
    stable-read guard exists to observe the bytes as they are right now.
    """
    return evaluate(
        live=content_hash(live_path, cache),
        vault=content_hash(vault_path, cache),
        baseline=baseline,
    )
