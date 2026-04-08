from __future__ import annotations

from pathlib import Path

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.config.settings import (
    _coerce_streaming_mode,
    state_dir,
    workspace_key,
)
from coding_agent.core.session import AssistantResponse, Usage
from coding_agent.interface import cli
from coding_agent.interface.cli import (
    _ensure_project_support_files,
    _has_configured_api_key,
    _probe_provider_connection,
)


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


def test_probe_provider_connection_reports_non_stream_success(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://example.com",
            model="demo-model",
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        return AssistantResponse(text="OK", usage=Usage(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=False)
    assert ok is True
    assert status == "ok"
    assert "Non-streaming request succeeded" in message


def test_probe_provider_connection_reports_stream_warning(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://example.com",
            model="demo-model",
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        if stream_callback is not None:
            stream_callback({
                "type": "warning",
                "warning": "Provider streaming completed with no text and no tool calls.",
                "category": "provider_streaming_empty_response",
            })
        return AssistantResponse(text="", usage=Usage())

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=True)
    assert ok is False
    assert status == "warning"
    assert "provider.stream: false" in message


# ---------------------------------------------------------------------------
# workspace_key / state_dir
# ---------------------------------------------------------------------------


def test_workspace_key_is_deterministic(tmp_path: Path) -> None:
    k1 = workspace_key(tmp_path)
    k2 = workspace_key(tmp_path)
    assert k1 == k2
    assert len(k1) == 12


def test_workspace_key_different_for_different_paths(tmp_path: Path) -> None:
    a = tmp_path / "project_a"
    b = tmp_path / "project_b"
    a.mkdir()
    b.mkdir()
    assert workspace_key(a) != workspace_key(b)


def test_state_dir_returns_home_based_path(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coding_agent.config.settings._HOME_YUCODE", fake_home / ".yucode")
    project = tmp_path / "my_project"
    project.mkdir()
    sd = state_dir(project)
    assert str(sd).startswith(str(fake_home / ".yucode" / "projects"))


# ---------------------------------------------------------------------------
# _coerce_streaming_mode
# ---------------------------------------------------------------------------


def test_coerce_streaming_mode_valid_values() -> None:
    assert _coerce_streaming_mode("stream") == "stream"
    assert _coerce_streaming_mode("no_stream") == "no_stream"
    assert _coerce_streaming_mode("hybrid") == "hybrid"


def test_coerce_streaming_mode_empty_defaults_to_hybrid() -> None:
    assert _coerce_streaming_mode("") == "hybrid"


def test_coerce_streaming_mode_boolean_strings() -> None:
    assert _coerce_streaming_mode("true") == "stream"
    assert _coerce_streaming_mode("false") == "no_stream"


def test_coerce_streaming_mode_auto_maps_to_hybrid() -> None:
    assert _coerce_streaming_mode("auto") == "hybrid"


def test_coerce_streaming_mode_unknown_defaults_to_hybrid() -> None:
    assert _coerce_streaming_mode("foobar") == "hybrid"
