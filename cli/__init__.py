"""The command line: `gsm <command>`, for people at a terminal and for scripts and agents.

Depends on `core`, and on `ui.presenter` - the Qt-free half of the GUI - so that the command
line and the window describe states, previews and confirmations in the same words, from the
same code. Nothing here imports Qt.

The command line is not a second set of rules. Every command runs the same core operation the
window runs, under the same lock, after the same startup recovery, and re-validates the
Entry's state immediately before acting exactly as the window does. Where the window asks for
confirmation, the command line does too: it prints the very same preview and then asks, or
takes `--yes`, or - for a write over a Live Save - an `--approve` token that only matches the
preview it was issued for (Invariant 7).
"""
