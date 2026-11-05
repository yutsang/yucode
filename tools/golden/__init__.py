"""Capture scripts that freeze the Python implementation's behaviour.

These run against the Python tree while it still exists and write machine-
readable baselines under `tests/golden/`. The TypeScript rewrite compares
against those files; nothing here is imported by the agent itself.
"""
