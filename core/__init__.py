"""Core logic: state, hashing, Vault (Git), and journaled writes to Live Saves.

Nothing in this package may import from `ui`. The core is headless by design so that
the paths capable of losing data can be tested without a GUI, a network, or a game.
"""
