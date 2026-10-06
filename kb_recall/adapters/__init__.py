"""Platform adapters — one subpackage per non-Claude harness.

Each adapter reuses the shared `kb_recall.hooks.hook_helpers` functions and calls
`kb_recall.server`'s tools directly, so every platform renders KB context through
the same code path and cannot drift from Claude Code's output.
"""
