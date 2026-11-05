"""Interface-layer pass/fail plumbing.

Covers IMPROVEMENT_PLAN_2026-08 items A6, A7 and A14 -- three defects that
shared a shape: the interface layer had the information a caller needed and
dropped it on the floor (eval never read `expected:`, headless always exited
0, /clear threw the conversation away).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

import pytest

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
from coding_agent.core.providers import OpenAICompatibleProvider
from coding_agent.core.runtime import AgentRuntime, TurnSummary
from coding_agent.core.session import AssistantResponse, Message, Session, Usage
from coding_agent.interface import cli
from coding_agent.interface import eval as eval_mod


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch) -> None:
    """Keep session/state writes out of the real ~/.yucode."""
    from coding_agent.config import settings as settings_mod
    monkeypatch.setattr(settings_mod, "_HOME_YUCODE", tmp_path / "home_yucode")


@pytest.fixture
def basic_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="test", api_key="k", model="gpt-test",
                                intelligence_tier="strong"),
        runtime=RuntimeOptions(permission_mode="danger-full-access"),
    )


# ---------------------------------------------------------------------------
# A6 -- eval grades against the suite's own `expected:` block
# ---------------------------------------------------------------------------

def _result(**kwargs) -> eval_mod.PromptResult:
    base = {"id": "X1", "category": "lookup", "prompt": "p", "iterations": 1}
    base.update(kwargs)
    return eval_mod.PromptResult(**base)


class TestJudge:
    def test_no_expected_block_yields_no_verdicts(self) -> None:
        assert eval_mod._judge(_result()) == []

    def test_tool_call_budget_pass_and_fail(self) -> None:
        under = _result(expected={"max_tool_calls": 3},
                        tool_calls=[{"name": "grep_search", "args": {}}] * 3)
        over = _result(expected={"max_tool_calls": 3},
                       tool_calls=[{"name": "grep_search", "args": {}}] * 4)
        assert eval_mod._judge(under)[0].passed is True
        verdict = eval_mod._judge(over)[0]
        assert verdict.criterion == "tool_calls_within_budget"
        assert verdict.passed is False
        assert "4 tool calls (cap 3)" in verdict.detail

    def test_must_read_files_true_requires_a_read(self) -> None:
        without = _result(expected={"must_read_files": True})
        with_read = _result(expected={"must_read_files": True}, files_read=["a.py"])
        assert eval_mod._judge(without)[0].criterion == "read_at_least_one_file"
        assert eval_mod._judge(without)[0].passed is False
        assert eval_mod._judge(with_read)[0].passed is True

    def test_must_read_files_false_means_optional_not_forbidden(self) -> None:
        """B2/D1 say reading is merely unnecessary -- reading anyway is fine,
        and not reading must not be graded at all."""
        criteria = [v.criterion for v in eval_mod._judge(
            _result(expected={"must_read_files": False}, files_read=["a.py"]))]
        assert criteria == []

    def test_artifact_mention_fails(self) -> None:
        clean = _result(expected={"must_not_mention_artifacts": True})
        dirty = _result(expected={"must_not_mention_artifacts": True},
                        artifact_mentions=[".pyc", "__pycache__"])
        assert eval_mod._judge(clean)[0].passed is True
        verdict = eval_mod._judge(dirty)[0]
        assert verdict.criterion == "no_build_artifacts_in_answer"
        assert verdict.passed is False
        assert ".pyc" in verdict.detail

    def test_errored_run_gets_one_failing_verdict(self) -> None:
        verdicts = eval_mod._judge(_result(
            expected={"max_tool_calls": 5, "must_read_files": True},
            error="ProviderError: boom",
        ))
        assert [v.criterion for v in verdicts] == ["run_completed"]
        assert verdicts[0].passed is False

    def test_prose_conclusions_are_flagged_not_matched(self) -> None:
        expected = {
            "max_tool_calls": 10,
            "acceptable_conclusions": ["not found / does not exist in the codebase"],
            "unacceptable_conclusions": ["surfaces .pyc paths as evidence"],
        }
        result = _result(expected=expected, final_text="totally unrelated answer")
        assert [v.criterion for v in eval_mod._judge(result)] == ["tool_calls_within_budget"]
        assert eval_mod._needs_human_review(expected) is True
        assert eval_mod._needs_human_review({"max_tool_calls": 10}) is False


class TestPromptResultDict:
    def test_verdicts_and_passed_are_serialized(self) -> None:
        result = _result(expected={"must_read_files": True})
        result.verdicts = eval_mod._judge(result)
        data = result.to_dict()
        assert data["expected"] == {"must_read_files": True}
        assert data["verdicts"] == [
            {"criterion": "read_at_least_one_file", "passed": False, "detail": "0 file(s) read"}
        ]
        assert data["passed"] is False

    def test_ungraded_prompt_reports_passed_none(self) -> None:
        """`None`, not `True` -- "nothing to check" is not "checked and fine"."""
        assert _result().to_dict()["passed"] is None


def _write_suite(path: Path) -> Path:
    path.write_text(
        "version: 1\n"
        "prompts:\n"
        "  - id: T1_budget\n"
        "    category: lookup\n"
        "    prompt: \"where is foo\"\n"
        "    expected:\n"
        "      max_tool_calls: 2\n"
        "      must_not_mention_artifacts: true\n",
        encoding="utf-8",
    )
    return path


class TestEvalRunExitCode:
    def _run(self, tmp_path: Path, monkeypatch, canned: eval_mod.PromptResult) -> tuple[int, dict]:
        suite = _write_suite(tmp_path / "suite.yaml")
        out = tmp_path / "results.json"
        monkeypatch.setattr(eval_mod, "_run_one", lambda entry, ws, cfg: canned)
        code = eval_mod.run([
            "--suite", str(suite), "--out", str(out), "--workspace", str(tmp_path),
        ])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_all_criteria_met_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        canned = _result(id="T1_budget", expected={"max_tool_calls": 2,
                                                   "must_not_mention_artifacts": True},
                         tool_calls=[{"name": "grep_search", "args": {}}])
        code, payload = self._run(tmp_path, monkeypatch, canned)
        assert code == 0
        assert payload["verdicts_passed"] == payload["verdicts_total"] == 2
        assert payload["prompts_failed"] == []

    def test_failed_criterion_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        canned = _result(id="T1_budget", expected={"max_tool_calls": 2,
                                                   "must_not_mention_artifacts": True},
                         tool_calls=[{"name": "grep_search", "args": {}}] * 5)
        code, payload = self._run(tmp_path, monkeypatch, canned)
        assert code == 1
        assert payload["prompts_failed"] == ["T1_budget"]
        assert payload["results"][0]["passed"] is False

    def test_errored_prompt_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        canned = _result(id="T1_budget", expected={"max_tool_calls": 2},
                         error="ProviderError: gateway 502")
        code, payload = self._run(tmp_path, monkeypatch, canned)
        assert code == 1
        assert payload["prompts_failed"] == ["T1_budget"]

    def test_real_suite_expected_blocks_are_all_gradable(self) -> None:
        """Every entry in the shipped suite must produce at least one verdict,
        otherwise A6 silently regresses back to hand-grading."""
        suite = Path(__file__).with_name("eval_prompts.yaml")
        for entry in eval_mod._load_suite(suite):
            result = _result(id=str(entry["id"]), expected=entry.get("expected"))
            assert eval_mod._judge(result), f"{entry['id']} produced no verdicts"


# ---------------------------------------------------------------------------
# A7 -- headless exit code + JSON payload
# ---------------------------------------------------------------------------

class TestStopReason:
    @pytest.mark.parametrize("text,reason", [
        ("[Provider error: connection reset. The task could not be completed.]",
         "provider_error"),
        ("partial\n\n[Stopped: `read_file` was repeatedly blocked (3x) in one turn.]",
         "stuck"),
        ("work\n\n[Note: Reached the maximum number of tool call budget (40).]",
         "max_tool_calls"),
        ("work\n\n[Note: Reached the maximum number of iterations (25).]",
         "max_iterations"),
        ("Here is the answer you asked for.", "end_turn"),
    ])
    def test_markers(self, text: str, reason: str) -> None:
        summary = TurnSummary(final_text=text, iterations=1,
                              usage=Usage(input_tokens=10, output_tokens=5))
        assert cli._stop_reason(summary) == reason

    def test_empty_response_with_zero_tokens(self) -> None:
        summary = TurnSummary(final_text="", iterations=1, usage=Usage())
        assert cli._stop_reason(summary) == "empty_response"

    def test_empty_text_with_tokens_is_not_a_config_failure(self) -> None:
        summary = TurnSummary(final_text="", iterations=1,
                              usage=Usage(input_tokens=10, output_tokens=0))
        assert cli._stop_reason(summary) == "end_turn"

    @pytest.mark.parametrize("text", [
        # The user asked *about* the message; the model quoted it back.
        "You see `Reached the maximum number of iterations (32).` when run_turn "
        "exhausts max_iterations. Raise it in config.yml.",
        "The tool call budget message is `Reached the maximum number of tool "
        "call budget (80).` — it comes from core/runtime.py.",
        "runtime.py writes `[Provider error: ...]` into final_text when the "
        "HTTP call raises, then returns early.",
        "Grep for `[Stopped:` to find the dedup force-exit path.",
    ])
    def test_an_answer_that_merely_quotes_a_marker_is_not_a_failure(self, text: str) -> None:
        """A substring search anywhere in final_text made any successful run
        that discussed these messages exit non-zero -- a false CI failure.
        run_turn only ever writes them as the whole text (provider error) or as
        the trailing paragraph, so the check is anchored to those positions."""
        summary = TurnSummary(final_text=text, iterations=2,
                              usage=Usage(input_tokens=100, output_tokens=50))
        assert cli._stop_reason(summary) == "end_turn"

    def test_the_coordinators_incomplete_banner_is_a_failure(self) -> None:
        """coordinator._compose_final prepends this banner when workers hit the
        step limit. It is a leading paragraph, not a trailing one, so the
        last-paragraph markers never saw it and headless exited 0 on output the
        harness had itself labelled incomplete."""
        summary = TurnSummary(
            final_text=("[INCOMPLETE: 3 of 3 worker(s) stopped at the 20-step limit "
                        "rather than finishing. Some of what follows is partial work "
                        "— verify it before building on it.]\n\n"
                        "here is the partial work"),
            iterations=4, usage=Usage(input_tokens=100, output_tokens=50))
        assert cli._stop_reason(summary) == "incomplete"
        assert "incomplete" in cli._FAILURE_STOP_REASONS

    def test_the_sub_agent_incomplete_banner_is_a_failure(self) -> None:
        """agent_tool writes the same prefix with different wording."""
        summary = TurnSummary(
            final_text=("[INCOMPLETE: the sub-agent stopped at its 30-step limit "
                        "rather than finishing.]\n\npartial"),
            iterations=4, usage=Usage(input_tokens=10, output_tokens=5))
        assert cli._stop_reason(summary) == "incomplete"

    def test_an_answer_that_merely_quotes_the_incomplete_banner_is_not_a_failure(self) -> None:
        """Same false-alarm class the other markers already guard against."""
        summary = TurnSummary(
            final_text=("The coordinator prepends `[INCOMPLETE: N of M worker(s) ...]` "
                        "in _compose_final when a worker hits max_worker_steps.\n\n"
                        "Grep for `[INCOMPLETE:` in core/coordinator.py."),
            iterations=2, usage=Usage(input_tokens=100, output_tokens=50))
        assert cli._stop_reason(summary) == "end_turn"

    def test_a_real_marker_after_a_multi_paragraph_answer_still_detected(self) -> None:
        summary = TurnSummary(
            final_text=("intro\n\nbody mentioning Reached the maximum number of "
                        "iterations in passing\n\n"
                        "[Note: Reached the maximum number of iterations (25). "
                        "The task may be incomplete. Use /resume to continue.]"),
            iterations=25, usage=Usage(input_tokens=10, output_tokens=5))
        assert cli._stop_reason(summary) == "max_iterations"


class TestHeadlessRun:
    def _args(self, tmp_path: Path) -> argparse.Namespace:
        return argparse.Namespace(
            prompt="find the bug", workspace=str(tmp_path), config_path=None,
            resume=None, model=None, permission_mode=None, allowed_tools=None,
            json_output=True, output_format="json",
        )

    def _run(self, tmp_path, monkeypatch, basic_config, summary, capsys):
        monkeypatch.setattr(cli, "load_app_config", lambda *a, **k: basic_config)
        monkeypatch.setattr(AgentRuntime, "orchestrate",
                            lambda self, prompt, event_callback=None: summary)
        code = cli._run_single_turn(self._args(tmp_path))
        return code, json.loads(capsys.readouterr().out)

    def test_success_exits_zero_and_reports_stop_reason(
        self, tmp_path, monkeypatch, basic_config, isolated_state, capsys,
    ) -> None:
        summary = TurnSummary(final_text="done", iterations=3,
                              usage=Usage(input_tokens=120, output_tokens=40,
                                          cache_read_input_tokens=100))
        code, payload = self._run(tmp_path, monkeypatch, basic_config, summary, capsys)
        assert code == 0
        assert payload["stop_reason"] == "end_turn"
        assert payload["iterations"] == 3
        assert payload["usage"]["input_tokens"] == 120
        assert payload["usage"]["cache_read_input_tokens"] == 100
        assert payload["usage"]["total_tokens"] == 160
        assert payload["usage_is_incomplete"] is False
        assert payload["session_id"]
        assert "events" in payload and "final_text" in payload

    @pytest.mark.parametrize("text,reason", [
        ("x\n\n[Note: Reached the maximum number of iterations (25).]", "max_iterations"),
        ("x\n\n[Note: Reached the maximum number of tool call budget (40).]", "max_tool_calls"),
        ("[Provider error: boom. The task could not be completed.]", "provider_error"),
        ("[INCOMPLETE: 3 of 3 worker(s) stopped at the 20-step limit rather than "
         "finishing. Some of what follows is partial work — verify it before "
         "building on it.]\n\npartial work", "incomplete"),
    ])
    def test_incomplete_run_exits_nonzero(
        self, tmp_path, monkeypatch, basic_config, isolated_state, capsys, text, reason,
    ) -> None:
        summary = TurnSummary(final_text=text, iterations=25,
                              usage=Usage(input_tokens=10, output_tokens=1))
        code, payload = self._run(tmp_path, monkeypatch, basic_config, summary, capsys)
        assert code == 1
        assert payload["stop_reason"] == reason

    def test_empty_provider_response_exits_nonzero_and_flags_usage(
        self, tmp_path, monkeypatch, basic_config, isolated_state, capsys,
    ) -> None:
        summary = TurnSummary(final_text="", iterations=1, usage=Usage())
        code, payload = self._run(tmp_path, monkeypatch, basic_config, summary, capsys)
        assert code == 1
        assert payload["stop_reason"] == "empty_response"
        assert payload["usage_is_incomplete"] is True


# ---------------------------------------------------------------------------
# A14 -- /clear preserves the conversation it replaces
# ---------------------------------------------------------------------------

class TestClearIsNonDestructive:
    def _clear(self, config, workspace, runtime) -> None:
        cli._handle_slash_command_interactive("clear", "", config, workspace, runtime)

    def test_old_conversation_survives_on_disk(
        self, tmp_path, basic_config, isolated_state, capsys,
    ) -> None:
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="remember this"))
        runtime.session.add_message(Message(role="assistant", content="noted"))
        old_id = runtime.session.session_id

        self._clear(basic_config, tmp_path, runtime)

        saved = Session.load_from_workspace(tmp_path, old_id)
        assert [m.content for m in saved.messages] == ["remember this", "noted"]
        assert f"/resume {old_id}" in capsys.readouterr().out

    def test_new_session_is_a_fresh_id_and_empty(
        self, tmp_path, basic_config, isolated_state, capsys,
    ) -> None:
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="hi"))
        runtime.session.usage.add(Usage(input_tokens=500, output_tokens=20))
        old_id = runtime.session.session_id

        self._clear(basic_config, tmp_path, runtime)

        assert runtime.session.session_id != old_id
        assert runtime.session.messages == []
        assert runtime.session.usage.total_tokens() == 0
        assert runtime.session.model == basic_config.provider.model

    def test_later_save_cannot_overwrite_the_parked_session(
        self, tmp_path, basic_config, isolated_state, capsys,
    ) -> None:
        """The reason for a new id: save_session() defaults to
        session.session_id, so reusing it would clobber the archive."""
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="keep me"))
        old_id = runtime.session.session_id

        self._clear(basic_config, tmp_path, runtime)
        runtime.save_session()

        assert Session.load_from_workspace(tmp_path, old_id).messages
        assert Session.load_from_workspace(tmp_path, runtime.session.session_id).messages == []

    def test_empty_session_writes_nothing(
        self, tmp_path, basic_config, isolated_state, capsys,
    ) -> None:
        runtime = AgentRuntime(tmp_path, basic_config)
        old_id = runtime.session.session_id

        self._clear(basic_config, tmp_path, runtime)

        assert not (Session.sessions_dir(tmp_path) / f"{old_id}.json").exists()
        assert runtime.session.session_id != old_id

    def test_unwritable_session_dir_aborts_instead_of_losing_history(
        self, tmp_path, basic_config, isolated_state, monkeypatch, capsys,
    ) -> None:
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="precious"))
        old_id = runtime.session.session_id
        monkeypatch.setattr(AgentRuntime, "save_session",
                            lambda self, session_id=None: (_ for _ in ()).throw(OSError("read-only fs")))

        self._clear(basic_config, tmp_path, runtime)

        assert runtime.session.session_id == old_id
        assert [m.content for m in runtime.session.messages] == ["precious"]
        assert "Not clearing" in capsys.readouterr().out

    def test_new_session_uses_the_model_the_user_switched_to(
        self, tmp_path, basic_config, isolated_state, capsys,
    ) -> None:
        """/model rebinds `runtime.config` and leaves the interactive loop's
        own `config` on the old model, so building the replacement Session
        from `config` pinned it to the model the user had switched away from.
        The saved session then claims the wrong model too."""
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="hi"))

        cli._handle_slash_command_interactive("model", "brand-new-model",
                                              basic_config, tmp_path, runtime)
        self._clear(basic_config, tmp_path, runtime)

        assert runtime.session.model == "brand-new-model"


# ---------------------------------------------------------------------------
# A14 -- the editor bridge's /clear must make the same promise as the CLI's
# ---------------------------------------------------------------------------

class TestBridgeClearIsNonDestructive:
    def _server(self):
        import io

        from coding_agent.interface import bridge as bridge_mod
        return bridge_mod, bridge_mod.BridgeServer(output_stream=io.StringIO())

    def test_save_failure_keeps_the_conversation(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        """The bridge popped the session *before* attempting the save, so a
        failed save still destroyed the only copy -- the exact outcome the
        save-first change exists to prevent, and the opposite of what the CLI
        path does with the same failure."""
        bridge_mod, server = self._server()
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="precious history"))
        server._sessions[str(tmp_path)] = bridge_mod._BridgeSession(runtime, basic_config)
        monkeypatch.setattr(AgentRuntime, "save_session",
                            lambda self, session_id=None: (_ for _ in ()).throw(OSError("disk full")))

        server._handle_slash("clear", "", tmp_path, None)

        assert str(tmp_path) in server._sessions
        assert [m.content for m in runtime.session.messages] == ["precious history"]
        assert "Not clearing" in server.output_stream.getvalue()

    def test_successful_save_still_clears(
        self, tmp_path, basic_config, isolated_state,
    ) -> None:
        bridge_mod, server = self._server()
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="keep me"))
        old_id = runtime.session.session_id
        server._sessions[str(tmp_path)] = bridge_mod._BridgeSession(runtime, basic_config)

        server._handle_slash("clear", "", tmp_path, None)

        assert str(tmp_path) not in server._sessions
        assert [m.content for m in Session.load_from_workspace(tmp_path, old_id).messages] == ["keep me"]


# ---------------------------------------------------------------------------
# The bridge's /clear points at /resume -- so /resume has to exist here
# ---------------------------------------------------------------------------

class TestBridgeResume:
    """`/clear` tells the user to `reopen it with /resume <id>`, and the bridge
    had no `resume` branch: the recovery instruction answered `Unknown command:
    /resume` in the one interface where, by that code's own reasoning, the user
    is least likely to hold another copy of the conversation."""

    def _server(self, monkeypatch, basic_config):
        import io

        from coding_agent.interface import bridge as bridge_mod
        # The bridge builds its runtime from the on-disk config; keep the real
        # user config (and its API key) out of the test.
        monkeypatch.setattr(bridge_mod, "load_app_config", lambda *a, **k: basic_config)
        return bridge_mod, bridge_mod.BridgeServer(output_stream=io.StringIO())

    def _payloads(self, server) -> list[dict]:
        return [json.loads(line) for line in server.output_stream.getvalue().splitlines()]

    def test_the_id_clear_hands_out_can_actually_be_resumed(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        bridge_mod, server = self._server(monkeypatch, basic_config)
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="precious history"))
        old_id = runtime.session.session_id
        server._sessions[str(tmp_path)] = bridge_mod._BridgeSession(runtime, basic_config)

        server._handle_slash("clear", "", tmp_path, None)
        server._handle_slash("resume", old_id, tmp_path, None)

        clear_result, resume_result = self._payloads(server)
        assert f"/resume {old_id}" in clear_result["text"]
        assert "Unknown command" not in resume_result["text"]
        restored = server._sessions[str(tmp_path)].runtime.session
        assert [m.content for m in restored.messages] == ["precious history"]

    def test_an_unknown_id_says_so_without_dropping_the_live_session(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        bridge_mod, server = self._server(monkeypatch, basic_config)
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="still here"))
        server._sessions[str(tmp_path)] = bridge_mod._BridgeSession(runtime, basic_config)

        server._handle_slash("resume", "nosuchsession", tmp_path, None)

        assert "not found" in self._payloads(server)[0]["text"]
        assert [m.content for m in
                server._sessions[str(tmp_path)].runtime.session.messages] == ["still here"]

    def test_bare_resume_lists_what_can_be_reopened(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        bridge_mod, server = self._server(monkeypatch, basic_config)
        runtime = AgentRuntime(tmp_path, basic_config)
        runtime.session.add_message(Message(role="user", content="find the bug"))
        old_id = runtime.session.session_id
        server._sessions[str(tmp_path)] = bridge_mod._BridgeSession(runtime, basic_config)
        server._handle_slash("clear", "", tmp_path, None)

        server._handle_slash("resume", "", tmp_path, None)

        assert old_id in self._payloads(server)[-1]["text"]

    def test_help_advertises_the_command_clear_points_at(
        self, tmp_path, basic_config, monkeypatch,
    ) -> None:
        _, server = self._server(monkeypatch, basic_config)
        server._handle_slash("help", "", tmp_path, None)
        assert "/resume" in self._payloads(server)[0]["text"]


# ---------------------------------------------------------------------------
# /clear and /resume must act on the session `chat` actually opened
# ---------------------------------------------------------------------------

class TestBridgeSlashUsesTheChatsSessionKey:
    """`chat` keys its session on `request["session_id"]` when the editor sends
    one, but `_dispatch` routed slash input to `_handle_slash` without that
    key, so `/clear` and `/resume` fell back to `str(workspace)`. With a
    session_id in play the two commands addressed a different conversation than
    chat did: `/clear` reported "Session cleared." having cleared nothing while
    the real conversation stayed open and unsaved, and `/resume` loaded the
    saved history into a session chat would never read."""

    def _server(self, monkeypatch, basic_config):
        import io

        from coding_agent.interface import bridge as bridge_mod
        monkeypatch.setattr(bridge_mod, "load_app_config", lambda *a, **k: basic_config)
        return bridge_mod, bridge_mod.BridgeServer(output_stream=io.StringIO())

    def _payloads(self, server) -> list[dict]:
        return [json.loads(line) for line in server.output_stream.getvalue().splitlines()]

    def _send(self, server, workspace, prompt, session_id=None) -> None:
        request = {"command": "chat", "workspace": str(workspace), "prompt": prompt}
        if session_id is not None:
            request["session_id"] = session_id
        server._dispatch(request)

    def _chat(self, server, workspace, monkeypatch, session_id=None, text="precious history"):
        def fake_orchestrate(self, prompt, event_callback=None):
            self.session.add_message(Message(role="user", content=text))
            return TurnSummary(final_text="ok", iterations=1)

        monkeypatch.setattr(AgentRuntime, "orchestrate", fake_orchestrate)
        self._send(server, workspace, text, session_id)

    def test_clear_clears_the_conversation_chat_opened_under_a_session_id(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        _, server = self._server(monkeypatch, basic_config)
        self._chat(server, tmp_path, monkeypatch, session_id="editor-tab-7")
        assert "editor-tab-7" in server._sessions, "chat should key on the editor's session_id"
        old_id = server._sessions["editor-tab-7"].runtime.session.session_id

        self._send(server, tmp_path, "/clear", session_id="editor-tab-7")

        # Reported success has to mean the live conversation was the one saved
        # and dropped -- not an absent `str(workspace)` entry.
        assert "editor-tab-7" not in server._sessions
        assert [m.content for m in
                Session.load_from_workspace(tmp_path, old_id).messages] == ["precious history"]
        assert f"/resume {old_id}" in self._payloads(server)[-1]["text"]

    def test_resume_reopens_into_the_session_chat_is_using(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        _, server = self._server(monkeypatch, basic_config)
        self._chat(server, tmp_path, monkeypatch, session_id="editor-tab-7", text="live message")
        parked = AgentRuntime(tmp_path, basic_config)
        parked.session.add_message(Message(role="user", content="archived"))
        parked_id = parked.session.session_id
        parked.save_session()

        self._send(server, tmp_path, f"/resume {parked_id}", session_id="editor-tab-7")

        assert [m.content for m in
                server._sessions["editor-tab-7"].runtime.session.messages] == ["archived"]
        # ...and without stranding a second runtime under the workspace key.
        assert str(tmp_path) not in server._sessions

    def test_without_a_session_id_the_workspace_is_still_the_key(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ) -> None:
        _, server = self._server(monkeypatch, basic_config)
        self._chat(server, tmp_path, monkeypatch)
        assert str(tmp_path) in server._sessions

        self._send(server, tmp_path, "/clear")

        assert str(tmp_path) not in server._sessions


# ---------------------------------------------------------------------------
# doctor's tool-calling probe must see the provider's self-heal
# ---------------------------------------------------------------------------

class TestToolCallingProbeSeesTheSelfHeal:
    """`probe_tool_calling` judged the gpt-5.6 "function tools with
    reasoning_effort" refusal by catching the exception. providers.py now heals
    that 400 itself -- it pins `reasoning_effort: none` on the provider instance
    and retries, so `complete()` returns normally and the probe reported a clean
    "ok". The user was never told that their model had just been stripped of
    reasoning for the whole session.
    """

    def _healed_complete(self, text: str = "OK"):
        def complete(provider_self, *a, **kw):
            # What _apply_self_heal does on the tools-vs-reasoning 400.
            provider_self._learned_reasoning_effort_none = True
            return AssistantResponse(text=text, tool_calls=[], usage=Usage())
        return complete

    def test_a_silently_healed_request_is_a_warning_not_ok(
        self, basic_config, monkeypatch,
    ) -> None:
        monkeypatch.setattr(OpenAICompatibleProvider, "complete", self._healed_complete())
        ok, status, summary = cli.probe_tool_calling(basic_config)

        assert status == "warning"
        assert "reasoning" in summary.lower()
        # Says what it cost, not just that something happened.
        assert "reasoning_effort" in summary

    def test_doctor_does_not_flatten_that_warning_back_to_ok(
        self, tmp_path, basic_config, monkeypatch, capsys,
    ) -> None:
        monkeypatch.setattr(cli, "load_app_config", lambda *a, **k: basic_config)
        monkeypatch.setattr(cli, "_probe_provider_connection",
                            lambda *a, **k: (True, "ok", "probe fine"))
        monkeypatch.setattr(cli, "probe_tool_calling",
                            lambda cfg: (True, "warning", "reasoning was turned off"))

        cli.handle_doctor(argparse.Namespace(
            workspace=str(tmp_path), config_path=None, output_format="json"))

        report = json.loads(capsys.readouterr().out)
        check = next(c for c in report["checks"] if c["name"] == "provider_tool_calling")
        assert check["status"] == "warning"

    def test_an_untouched_provider_still_reports_ok(
        self, basic_config, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            OpenAICompatibleProvider, "complete",
            lambda self, *a, **kw: AssistantResponse(text="OK", tool_calls=[], usage=Usage()))
        assert cli.probe_tool_calling(basic_config)[:2] == (True, "ok")

    def test_the_self_heal_flag_the_probe_reads_still_exists(self) -> None:
        """The probe reads a private field on the provider because that is the
        only record the heal leaves behind. If providers.py renames it, fail
        here loudly rather than let the warning quietly stop firing."""
        names = {f.name for f in dataclasses.fields(OpenAICompatibleProvider)}
        assert "_learned_reasoning_effort_none" in names


# ---------------------------------------------------------------------------
# The bridge held every session it had ever seen, and answered /tools by
# building a throwaway runtime
# ---------------------------------------------------------------------------

class TestBridgeSessionTableIsBounded:
    """An editor is a long-lived process handing out a fresh session_id per
    window or tab. Nothing removed an entry except /clear, and each one holds a
    tool registry, an MCP manager and a whole conversation."""

    def _server(self, monkeypatch, basic_config):
        import io

        from coding_agent.interface import bridge as bridge_mod
        monkeypatch.setattr(bridge_mod, "load_app_config", lambda *a, **k: basic_config)
        return bridge_mod, bridge_mod.BridgeServer(output_stream=io.StringIO())

    def test_the_table_stops_growing(self, tmp_path, basic_config, isolated_state, monkeypatch):
        bridge_mod, server = self._server(monkeypatch, basic_config)
        for i in range(bridge_mod._MAX_LIVE_SESSIONS + 5):
            server._get_or_create_session(tmp_path, None, session_key=f"tab-{i}")
        assert len(server._sessions) == bridge_mod._MAX_LIVE_SESSIONS

    def test_the_oldest_goes_first_and_the_active_one_stays(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ):
        bridge_mod, server = self._server(monkeypatch, basic_config)
        server._get_or_create_session(tmp_path, None, session_key="tab-0")
        for i in range(1, bridge_mod._MAX_LIVE_SESSIONS):
            server._get_or_create_session(tmp_path, None, session_key=f"tab-{i}")
        # Touching tab-0 makes it the most recent, so the next eviction must
        # take tab-1 instead.
        server._get_or_create_session(tmp_path, None, session_key="tab-0")
        server._get_or_create_session(tmp_path, None, session_key="tab-new")

        assert "tab-0" in server._sessions
        assert "tab-1" not in server._sessions

    def test_an_evicted_conversation_is_saved_not_lost(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ):
        bridge_mod, server = self._server(monkeypatch, basic_config)
        first = server._get_or_create_session(tmp_path, None, session_key="tab-0")
        first.runtime.session.add_message(Message(role="user", content="precious history"))
        old_id = first.runtime.session.session_id

        for i in range(1, bridge_mod._MAX_LIVE_SESSIONS + 1):
            server._get_or_create_session(tmp_path, None, session_key=f"tab-{i}")

        assert "tab-0" not in server._sessions
        restored = Session.load_from_workspace(tmp_path, old_id)
        assert [m.content for m in restored.messages] == ["precious history"]


class TestBridgeSlashUsesTheLiveSession:
    def _server(self, monkeypatch, basic_config):
        import io

        from coding_agent.interface import bridge as bridge_mod
        monkeypatch.setattr(bridge_mod, "load_app_config", lambda *a, **k: basic_config)
        return bridge_mod, bridge_mod.BridgeServer(output_stream=io.StringIO())

    def test_tools_does_not_build_a_second_runtime(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ):
        """Constructing an AgentRuntime installs the env-scrub policy from its
        own config, process-wide. Answering /tools with a throwaway runtime
        therefore re-pointed credential withholding for every workspace this
        process serves, at the settings file rather than the running session."""
        bridge_mod, server = self._server(monkeypatch, basic_config)
        session = server._get_or_create_session(tmp_path, None, session_key="tab-0")

        built: list[int] = []
        original = AgentRuntime.__init__

        def _counting_init(self, *a, **k):
            built.append(1)
            original(self, *a, **k)

        monkeypatch.setattr(AgentRuntime, "__init__", _counting_init)
        server._handle_slash("tools", "", tmp_path, None, "tab-0")

        assert built == [], "a second runtime was constructed to answer /tools"
        assert len(server._sessions) == 1
        assert session.runtime.tools.list_names()

    def test_status_reports_the_running_session(
        self, tmp_path, basic_config, isolated_state, monkeypatch,
    ):
        """/status read the file, so it described a session that need not exist:
        an edit to settings.yml since the session started does not reach it."""
        import dataclasses

        bridge_mod, server = self._server(monkeypatch, basic_config)
        server._get_or_create_session(tmp_path, None, session_key="tab-0")
        changed = dataclasses.replace(
            basic_config,
            provider=dataclasses.replace(basic_config.provider, model="edited-on-disk"),
        )
        monkeypatch.setattr(bridge_mod, "load_app_config", lambda *a, **k: changed)

        server._handle_slash("status", "", tmp_path, None, "tab-0")

        text = server.output_stream.getvalue()
        assert "edited-on-disk" not in text
        assert basic_config.provider.model in text
