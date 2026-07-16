from __future__ import annotations

import argparse
from pathlib import Path

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.config.settings import (
    BUNDLED_CONFIG_PATH,
    _coerce_streaming_mode,
    app_config_from_dict,
    state_dir,
    workspace_key,
)
from coding_agent.config.simple_yaml import load_yaml
from coding_agent.core.session import AssistantResponse, Usage
from coding_agent.interface import cli
from coding_agent.interface.cli import (
    _apply_cli_overrides,
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


def test_app_config_from_dict_reads_verify_tls() -> None:
    config = app_config_from_dict({
        "provider": {
            "base_url": "https://api.example.com",
            "api_key": "key",
            "model": "demo-model",
            "verify_tls": False,
        }
    })
    assert config.provider.verify_tls is False


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


def test_to_control_dict_includes_verify_tls() -> None:
    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.example.com",
            api_key="key",
            model="demo-model",
            verify_tls=False,
        ),
        runtime=RuntimeOptions(),
    )
    assert config.to_control_dict()["provider"]["verify_tls"] is False


def test_app_config_from_dict_reads_api_version() -> None:
    config = app_config_from_dict({
        "provider": {
            "base_url": "https://api.workbench.example",
            "api_key": "key",
            "model": "gpt-5-5",
            "api_version": "2024-12-01-preview",
        }
    })
    assert config.provider.api_version == "2024-12-01-preview"


def test_app_config_from_dict_api_version_defaults_empty() -> None:
    config = app_config_from_dict({
        "provider": {
            "base_url": "https://api.example.com",
            "api_key": "key",
            "model": "demo-model",
        }
    })
    assert config.provider.api_version == ""


def test_to_control_dict_includes_api_version() -> None:
    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.workbench.example",
            api_key="key",
            model="gpt-5-5",
            api_version="2024-12-01-preview",
        ),
        runtime=RuntimeOptions(),
    )
    assert config.to_control_dict()["provider"]["api_version"] == "2024-12-01-preview"


def test_app_config_from_dict_reads_omit_params() -> None:
    config = app_config_from_dict({
        "provider": {
            "base_url": "https://api.example.com",
            "api_key": "key",
            "model": "demo-model",
            "omit_params": ["temperature"],
        }
    })
    assert config.provider.omit_params == ["temperature"]


def test_to_control_dict_includes_omit_params() -> None:
    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.example.com",
            api_key="key",
            model="demo-model",
            omit_params=["temperature"],
        ),
        runtime=RuntimeOptions(),
    )
    assert config.to_control_dict()["provider"]["omit_params"] == ["temperature"]


def test_bundled_config_defaults_to_workbench() -> None:
    """WI-3: the bundled config.yml (becomes ~/.yucode/settings.yml on first
    run) ships workbench as the default provider, not deepseek."""
    text = BUNDLED_CONFIG_PATH.read_text(encoding="utf-8")
    raw = load_yaml(text)
    config = app_config_from_dict(raw)
    assert config.provider.name == "workbench"
    assert config.provider.api_version == "2024-12-01-preview"
    assert config.provider.omit_params == ["temperature"]
    assert config.provider.streaming_mode == "no_stream"
    assert config.provider.request_timeout_seconds == 300
    assert config.provider.verify_tls is False
    assert config.runtime.orchestration_mode == "single"
    assert config.provider.resolved_tier() == "strong"


def test_bundled_config_builds_expected_azure_url_and_headers() -> None:
    import dataclasses

    from coding_agent.core.providers import OpenAICompatibleProvider

    raw = load_yaml(BUNDLED_CONFIG_PATH.read_text(encoding="utf-8"))
    config = app_config_from_dict(raw)
    provider = OpenAICompatibleProvider(config=config.provider)
    assert provider._build_url() == (
        "https://api.workbench.kpmg/genai/azure/openai"
        "/deployments/gpt-5-5-2026-04-24-gs-sdc/chat/completions"
        "?api-version=2024-12-01-preview"
    )
    # api_key ships empty in the bundled default (real value lives only in
    # the user's settings.yml) — no Authorization header without one either
    # way, so verify the api-key-vs-Bearer switch with a filled-in copy.
    headers_unfilled = provider._headers(stream=False)
    assert "Authorization" not in headers_unfilled
    assert "Ocp-Apim-Subscription-Key" in headers_unfilled

    filled_provider = OpenAICompatibleProvider(config=dataclasses.replace(config.provider, api_key="TESTKEY"))
    headers_filled = filled_provider._headers(stream=False)
    assert headers_filled["api-key"] == "TESTKEY"
    assert "Authorization" not in headers_filled

    body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
    assert "temperature" not in body
    assert body["reasoning_effort"] == "medium"


def test_workbench_settings_template_parses_and_matches_bundled_shape() -> None:
    """docs/settings.workbench.yml is the full copy-paste template for the
    company PC — it must parse and produce the same request shape as the
    bundled default (WI-3 verify step)."""
    from coding_agent.core.providers import OpenAICompatibleProvider

    template_path = Path(__file__).resolve().parents[1] / "docs" / "settings.workbench.yml"
    raw = load_yaml(template_path.read_text(encoding="utf-8"))
    config = app_config_from_dict(raw)
    assert config.provider.omit_params == ["temperature"]  # regression: inline `[x]` mis-parses as a string
    assert config.runtime.orchestration_mode == "single"
    provider = OpenAICompatibleProvider(config=config.provider)
    assert "/deployments/" in provider._build_url()
    assert "api-version=2024-12-01-preview" in provider._build_url()
    body = provider._build_body([{"role": "user", "content": "hi"}], [], False)
    assert "temperature" not in body


def test_cli_model_override_preserves_workbench_provider_fields() -> None:
    """Regression: --model must not silently drop omit_params/api_version/
    intelligence_tier — a workbench user switching GPT-5.5 <-> 5.4 on the CLI
    would otherwise lose the Azure URL building and the temperature omission."""
    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.workbench.kpmg/genai/azure/openai",
            api_key="key",
            model="gpt-5-5-2026-04-24-gs-sdc",
            api_version="2024-12-01-preview",
            omit_params=["temperature"],
            intelligence_tier="strong",
            extra_headers={"Ocp-Apim-Subscription-Key": "key"},
            extra_body={"reasoning_effort": "medium"},
        ),
        runtime=RuntimeOptions(),
    )
    args = argparse.Namespace(model="gpt-5-4-2026-03-05-gs-sdc", permission_mode=None, allowed_tools=None)
    updated = _apply_cli_overrides(config, args)
    assert updated.provider.model == "gpt-5-4-2026-03-05-gs-sdc"
    assert updated.provider.api_version == "2024-12-01-preview"
    assert updated.provider.omit_params == ["temperature"]
    assert updated.provider.intelligence_tier == "strong"
    assert updated.provider.extra_headers == {"Ocp-Apim-Subscription-Key": "key"}
    assert updated.provider.extra_body == {"reasoning_effort": "medium"}


def test_api_version_and_omit_params_round_trip_through_yaml(tmp_path: Path) -> None:
    from coding_agent.config.simple_yaml import dump_yaml

    config = AppConfig(
        provider=ProviderConfig(
            base_url="https://api.workbench.example",
            api_key="key",
            model="gpt-5-5",
            api_version="2024-12-01-preview",
            omit_params=["temperature"],
        ),
        runtime=RuntimeOptions(),
    )
    text = dump_yaml(config.to_control_dict())
    raw = load_yaml(text)
    reloaded = app_config_from_dict(raw)
    assert reloaded.provider.api_version == "2024-12-01-preview"
    assert reloaded.provider.omit_params == ["temperature"]


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


def test_probe_provider_connection_preserves_verify_tls(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig(
        provider=ProviderConfig(
            name="test",
            api_key="key",
            base_url="https://example.com",
            model="demo-model",
            verify_tls=False,
            stream=True,
        ),
        runtime=RuntimeOptions(),
    )

    monkeypatch.setattr(cli, "load_app_config", lambda *args, **kwargs: config)

    def fake_complete(self, messages, tools, stream_callback=None):
        assert self.config.verify_tls is False
        return AssistantResponse(text="OK", usage=Usage(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(cli.OpenAICompatibleProvider, "complete", fake_complete)

    ok, status, message = _probe_provider_connection(None, workspace=tmp_path, stream=False)
    assert ok is True
    assert status == "ok"
    assert "Non-streaming request succeeded" in message


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
        if label.startswith("provider.verify_tls"):
            return False
        return current

    monkeypatch.setattr(cli, "_prompt", fake_prompt)
    monkeypatch.setattr(cli, "_test_api_connection", lambda *args, **kwargs: (True, "ok"))

    result = cli.handle_init_config(argparse.Namespace(config_path=str(config_path)))
    saved = load_yaml(config_path.read_text(encoding="utf-8"))

    assert result == 0
    assert isinstance(saved, dict)
    assert saved["provider"]["append_chat_path"] is False
    assert saved["provider"]["verify_tls"] is False
