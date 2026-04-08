from __future__ import annotations

import argparse
from pathlib import Path

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.config.settings import (
    _coerce_streaming_mode,
    app_config_from_dict,
    state_dir,
    workspace_key,
)
from coding_agent.config.simple_yaml import load_yaml
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


# ---------------------------------------------------------------------------
# _probe_provider_connection — non-OpenAI envelope
# ---------------------------------------------------------------------------


def test_probe_provider_connection_reports_gateway_error(tmp_path: Path, monkeypatch) -> None:
    """When the provider raises RuntimeError (non-OpenAI envelope), doctor
    should report an error with the effective URL."""
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://gateway.example.com",
            model="demo-model",
            chat_path="/v1/chat/completions",
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        raise RuntimeError(
            "The provider endpoint (https://gateway.example.com/v1/chat/completions) "
            "returned a non-OpenAI response. Payload keys: ['code', 'flag', 'msg', 'ts']. "
            "Server message: auth failed"
        )

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=False)
    assert ok is False
    assert status == "error"
    assert "gateway.example.com" in message
    assert "non-OpenAI" in message


def test_probe_provider_connection_error_includes_url(tmp_path: Path, monkeypatch) -> None:
    """Doctor error messages now include the effective URL."""
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://example.com",
            model="demo-model",
            chat_path="/chat/completions",
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=False)
    assert ok is False
    assert status == "error"
    assert "example.com/chat/completions" in message


def test_app_config_from_dict_reads_append_chat_path() -> None:
    config = app_config_from_dict({
        "provider": {
            "base_url": "https://api.example.com/v1/chat/completions",
            "api_key": "key",
            "model": "demo-model",
            "chat_path": "/chat/completions",
            "append_chat_path": False,
        }
    })
    assert config.provider.append_chat_path is False


def test_to_control_dict_includes_append_chat_path() -> None:
    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.example.com",
            api_key="key",
            model="demo-model",
            append_chat_path=False,
        ),
        runtime=RuntimeOptions(),
    )
    assert config.to_control_dict()["provider"]["append_chat_path"] is False


def test_probe_provider_connection_uses_base_url_when_append_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://example.com/v1/chat/completions",
            model="demo-model",
            chat_path="/chat/completions",
            append_chat_path=False,
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=False)
    assert ok is False
    assert status == "error"
    assert "https://example.com/v1/chat/completions" in message
    assert "https://example.com/v1/chat/completions/chat/completions" not in message


def test_handle_init_config_saves_append_chat_path(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / ".yucode" / "settings.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "provider:\n"
        "  base_url: https://api.example.com\n"
        "  api_key: key\n"
        "  model: demo-model\n"
        "  chat_path: /chat/completions\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def fake_prompt(label, current, *, secret=False, choices=None):
        if label.startswith("provider.append_chat_path"):
            return False
        return current

    monkeypatch.setattr(cli, "_prompt", fake_prompt)
    monkeypatch.setattr(cli, "_test_api_connection", lambda *args, **kwargs: (True, "ok"))

    result = cli.handle_init_config(argparse.Namespace(config_path=str(config_path)))
    saved = load_yaml(config_path.read_text(encoding="utf-8"))

    assert result == 0
    assert isinstance(saved, dict)
    assert saved["provider"]["append_chat_path"] is False
