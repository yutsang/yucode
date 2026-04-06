from __future__ import annotations

from pathlib import Path

from coding_agent.interface.cli import _ensure_project_support_files, _has_configured_api_key


def test_ensure_project_support_files_creates_expected_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    created = _ensure_project_support_files(tmp_path)

    env_example = tmp_path / ".env.example"
    gitignore = tmp_path / ".gitignore"
    local_overlay = tmp_path / ".yucode" / "settings.local.yml"

    assert env_example in created
    assert gitignore in created
    assert local_overlay in created
    assert env_example.is_file()
    assert local_overlay.is_file()
    gitignore_text = gitignore.read_text(encoding="utf-8")
    assert ".yucode/settings.local.yml" in gitignore_text
    assert ".env" in gitignore_text


def test_has_configured_api_key_reads_env(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / ".yucode" / "settings.yml"
    monkeypatch.setenv("YUCODE_API_KEY", "env-test-key")
    assert _has_configured_api_key(str(config_path), workspace=tmp_path) is True
