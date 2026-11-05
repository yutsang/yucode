"""Post-edit syntax feedback, tool-output spill, audit identity, metrics snapshot.

Covers IMPROVEMENT_PLAN_2026-08 items A16, A3, A5 and A11 -- four defects that
shared a shape: the runtime did the work and then threw away the evidence.

Plus the three follow-ups the review of that work raised: nothing under
`state_dir` was ever deleted, the per-tool-result cap bounded one field rather
than the whole result, and a subagent's audit rows named a session that does
not exist.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from coding_agent.config import load_app_config
from coding_agent.config.settings import state_dir
from coding_agent.core.runtime import AgentRuntime
from coding_agent.tools import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    return ToolRegistry(workspace, load_app_config(None), None)


def _runtime(workspace: Path) -> AgentRuntime:
    config = load_app_config(None)
    config = dataclasses.replace(
        config,
        runtime=dataclasses.replace(config.runtime, permission_mode="danger-full-access"),
    )
    return AgentRuntime(workspace, config)


class TestPostEditSyntaxFeedback:
    """A16 -- a syntax error must be reported by the write that caused it.

    Previously nothing checked a file after the agent wrote it, so a broken
    edit surfaced on some later pytest run, after the model had already built
    on top of it.
    """

    def test_write_reports_a_syntax_error(self, registry: ToolRegistry) -> None:
        out = registry.execute("write_file", {"path": "bad.py", "content": "def f(:\n    pass\n"})
        assert "Syntax error introduced in bad.py" in out
        assert "line 1" in out

    def test_write_stays_quiet_on_valid_python(self, registry: ToolRegistry) -> None:
        out = registry.execute("write_file", {"path": "ok.py", "content": "def f():\n    pass\n"})
        assert "Syntax error" not in out

    def test_edit_reports_a_syntax_error_it_introduced(self, registry: ToolRegistry) -> None:
        registry.execute("write_file", {"path": "m.py", "content": "def f():\n    pass\n"})
        out = registry.execute(
            "edit_file", {"path": "m.py", "old_string": "def f():", "new_string": "def f(:"},
        )
        assert "Syntax error introduced in m.py" in out

    def test_the_file_is_still_written(self, registry: ToolRegistry, workspace: Path) -> None:
        """Report, don't refuse -- the model may be mid-refactor on purpose."""
        registry.execute("write_file", {"path": "half.py", "content": "def f(:\n"})
        assert (workspace / "half.py").read_text() == "def f(:\n"

    def test_a_bom_prefixed_file_is_not_reported_as_broken(
        self, registry: ToolRegistry, workspace: Path,
    ) -> None:
        """A UTF-8 BOM is legal in a .py file; `ast.parse` on a str is not.

        Python's tokenizer strips the BOM when it reads a *file*, so such a
        module imports fine, but `ast.parse` receives the decoded string with
        a leading U+FEFF and rejects it as an invalid non-printable character.
        `_edit_file` reads with plain "utf-8" (not "utf-8-sig"), so every edit
        to a BOM-prefixed file -- the normal shape of a file written by a
        Windows editor -- reports a syntax error at line 1 that does not
        exist, and tells the model to go fix it.
        """
        (workspace / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
        out = registry.execute(
            "edit_file", {"path": "bom.py", "old_string": "x = 1", "new_string": "x = 2"},
        )
        assert "Syntax error" not in out, out

    def test_apply_patch_reports_a_syntax_error_it_introduced(
        self, registry: ToolRegistry,
    ) -> None:
        """apply_patch writes files too, and had no check at all.

        The whole point of the check is that the model hears about the break
        while it still has the context to fix it; a patch that lands a syntax
        error was surfacing several turns later on a pytest run instead.
        """
        registry.execute("write_file", {"path": "p.py", "content": "def f():\n    pass\n"})
        out = registry.execute("apply_patch", {"patch": (
            "*** Begin Patch\n"
            "*** Update File: p.py\n"
            "@@\n"
            "-def f():\n"
            "+def f(:\n"
            "*** End Patch\n"
        )})
        assert "Syntax error introduced in p.py" in out, out

    def test_apply_patch_reports_a_syntax_error_in_an_added_file(
        self, registry: ToolRegistry,
    ) -> None:
        out = registry.execute("apply_patch", {"patch": (
            "*** Begin Patch\n"
            "*** Add File: added.py\n"
            "+def g(:\n"
            "*** End Patch\n"
        )})
        assert "Syntax error introduced in added.py" in out, out

    def test_apply_patch_stays_quiet_on_valid_python(self, registry: ToolRegistry) -> None:
        out = registry.execute("apply_patch", {"patch": (
            "*** Begin Patch\n"
            "*** Add File: fine.py\n"
            "+def g():\n"
            "+    return 1\n"
            "*** End Patch\n"
        )})
        assert "Syntax error" not in out, out

    def test_apply_patch_still_writes_the_broken_file(
        self, registry: ToolRegistry, workspace: Path,
    ) -> None:
        registry.execute("apply_patch", {"patch": (
            "*** Begin Patch\n"
            "*** Add File: broken.py\n"
            "+def g(:\n"
            "*** End Patch\n"
        )})
        assert (workspace / "broken.py").read_text(encoding="utf-8") == "def g(:\n"

    def test_non_python_is_untouched(self, registry: ToolRegistry) -> None:
        out = registry.execute("write_file", {"path": "notes.txt", "content": "def f(:"})
        assert "Syntax error" not in out


class TestSyntaxCheckSizeCap:
    """The check is an implicit tax on every write/edit, so it needs a ceiling.

    `ast.parse` runs ~0.2-0.5 ms per KB of source (measured: 256 KB = 41 ms,
    1 MB = 214 ms, 10 MB = 2.4 s on the dev box; a reviewer's slower machine
    measured 585 KB = 304 ms and 2.3 MB = 1.5 s). `_write_file` caps content
    at MAX_WRITE_SIZE = 10 MB, but `_edit_file` reads from disk and had no
    cap at all -- so every single edit to a multi-MB generated module
    (`*_pb2.py` and friends) paid seconds for a check that cannot help,
    because nobody hand-wrote that syntax.
    """

    def _broken_source(self, size_bytes: int) -> str:
        """Valid-looking padding with an unmistakable syntax error at the end."""
        filler = "x = 1\n" * (size_bytes // 6)
        return filler + "def f(:\n    pass\n"

    def test_an_oversized_write_skips_the_check(self, registry: ToolRegistry) -> None:
        from coding_agent.tools.filesystem import _SYNTAX_CHECK_MAX_BYTES

        content = self._broken_source(_SYNTAX_CHECK_MAX_BYTES + 4096)
        out = registry.execute("write_file", {"path": "generated_pb2.py", "content": content})
        assert "Syntax error" not in out, out

    def test_an_oversized_edit_skips_the_check(
        self, registry: ToolRegistry, workspace: Path,
    ) -> None:
        """The unbounded path: `_edit_file` re-parses whatever is on disk."""
        from coding_agent.tools.filesystem import _SYNTAX_CHECK_MAX_BYTES

        source = self._broken_source(_SYNTAX_CHECK_MAX_BYTES + 4096)
        (workspace / "big_pb2.py").write_text(source, encoding="utf-8")
        out = registry.execute(
            "edit_file",
            {"path": "big_pb2.py", "old_string": "def f(:", "new_string": "def g(:"},
        )
        assert "Syntax error" not in out, out

    def test_a_file_under_the_cap_is_still_checked(self, registry: ToolRegistry) -> None:
        """The cap must not be so tight that ordinary source stops being checked.

        The largest hand-written module in this repo is ~100 KB, and 99.9% of
        the .py files in a real site-packages tree are under 363 KB.
        """
        from coding_agent.tools.filesystem import _SYNTAX_CHECK_MAX_BYTES

        content = self._broken_source(_SYNTAX_CHECK_MAX_BYTES - 4096)
        out = registry.execute("write_file", {"path": "large_but_sane.py", "content": content})
        assert "Syntax error introduced in large_but_sane.py" in out, out[-300:]

    def test_the_cap_covers_the_biggest_module_in_this_repo(self) -> None:
        from coding_agent.tools.filesystem import _SYNTAX_CHECK_MAX_BYTES

        biggest = max(
            (p.stat().st_size for p in Path("coding_agent").rglob("*.py")),
            default=0,
        )
        assert 0 < biggest < _SYNTAX_CHECK_MAX_BYTES

    def test_an_oversized_file_is_still_written(
        self, registry: ToolRegistry, workspace: Path,
    ) -> None:
        """Skipping the check must not skip the write."""
        from coding_agent.tools.filesystem import _SYNTAX_CHECK_MAX_BYTES

        content = self._broken_source(_SYNTAX_CHECK_MAX_BYTES + 4096)
        registry.execute("write_file", {"path": "huge.py", "content": content})
        assert (workspace / "huge.py").read_text(encoding="utf-8") == content


class TestToolOutputSpill:
    """A3 -- output over the cap goes to disk instead of being lost.

    bash has always spilled; every other tool truncated in place with no way
    to recover the remainder.
    """

    def test_over_cap_output_is_written_to_disk(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        big = "y" * (64 * 1024)
        (workspace / "big.txt").write_text(big, encoding="utf-8")

        out = runtime._execute_tool("read_file", json.dumps({"path": "big.txt"}))

        assert "Full output" in out, out[-400:]
        spilled = list((state_dir(workspace) / "tool_output").glob("*.txt"))
        assert len(spilled) == 1
        assert "y" * 1000 in spilled[0].read_text(encoding="utf-8")

    def test_the_reported_size_is_the_full_size_not_the_truncated_one(
        self, workspace: Path,
    ) -> None:
        """The footer's job is to tell the model how much it is NOT seeing.

        The size was computed after `output` had already been rebound to the
        truncated text, so a 65 KB result advertised itself as ~32 KB -- the
        exact size of the excerpt printed directly above it. A model reading
        that has no reason to open the spill file at all.
        """
        runtime = _runtime(workspace)
        (workspace / "big.txt").write_text("y" * (64 * 1024), encoding="utf-8")

        out = runtime._execute_tool("read_file", json.dumps({"path": "big.txt"}))

        match = re.search(r"Full output \(([\d,]+)", out)
        assert match is not None, out[-400:]
        reported = int(match.group(1).replace(",", ""))

        spilled = list((state_dir(workspace) / "tool_output").glob("*.txt"))[0]
        assert reported == len(spilled.read_text(encoding="utf-8"))
        assert reported > len(out)

    def test_the_footer_points_at_a_tool_that_can_actually_open_it(
        self, workspace: Path,
    ) -> None:
        """The spill has to be reachable by the tool the footer names.

        Spills land in state_dir, outside the workspace, and `_resolve_path`
        refused anything outside the tree -- so the original "read it with
        read_file" instruction always failed and the feature was inert.
        Redirecting the advice to bash did not help either: bash requires
        danger-full-access while the shipped default is workspace-write, so
        under the default config that path is denied too.

        The fix is to make the agent's own state directory readable, so this
        runs under the *default* permission mode, not an elevated one.
        """
        config = load_app_config(None)
        assert config.runtime.permission_mode == "workspace-write", (
            "this test is about the shipped default; if it changed, revisit "
            "whether bash would now be a usable route to the spill"
        )
        runtime = AgentRuntime(workspace, config)
        (workspace / "big.txt").write_text("y" * (64 * 1024), encoding="utf-8")
        out = runtime._execute_tool("read_file", json.dumps({"path": "big.txt"}))

        match = re.search(r"saved to (\S+?)\. ", out)
        assert match is not None, out[-400:]
        spill_path = match.group(1)

        recovered = runtime._execute_tool("read_file", json.dumps({"path": spill_path}))
        assert "escapes the workspace" not in recovered
        assert "y" * 500 in recovered

    def test_the_state_dir_exemption_does_not_open_the_home_directory(
        self, workspace: Path,
    ) -> None:
        """Only the agent's own state tree is exempt, not everything above it."""
        runtime = _runtime(workspace)
        outsider = state_dir(workspace).parent.parent / "settings.yml"
        outsider.parent.mkdir(parents=True, exist_ok=True)
        outsider.write_text("api_key: secret\n", encoding="utf-8")

        denied = runtime._execute_tool("read_file", json.dumps({"path": str(outsider)}))
        assert "escapes the workspace" in denied

    def test_small_output_does_not_spill(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        (workspace / "small.txt").write_text("hello", encoding="utf-8")
        runtime._execute_tool("read_file", json.dumps({"path": "small.txt"}))
        assert not (state_dir(workspace) / "tool_output").exists()


class TestSpillPointerKeepsJsonParseable:
    """The spill pointer was appended outside the JSON's closing brace.

    `_truncate_tool_output` goes out of its way to keep an over-cap JSON
    result parseable — it shrinks the string leaves and re-serializes rather
    than byte-slicing — precisely so downstream `json.loads()` calls keep
    working. `_record_tool_observation`'s bash returncode parser, in this same
    module, is one of them. Appending "[Full output ... saved to ...]" after
    the closing brace undid exactly that, and pushed the result back over the
    cap it had just enforced.
    """

    def _over_cap_bash_output(self, runtime: AgentRuntime, workspace: Path) -> str:
        """A bash result whose JSON is over the runtime cap but under bash's own.

        shell.py truncates a stream itself only past 128 KB, so 70 KB of
        stdout reaches the runtime intact and trips the 32 KB outer cap.
        """
        script = workspace / "gen.py"
        script.write_text("print('y' * 70000)", encoding="utf-8")
        out = runtime._execute_tool(
            "bash", json.dumps({"command": f"{sys.executable} {script}"}),
        )
        assert "Full output" in out, out[-400:]  # the spill actually happened
        return out

    def test_an_over_cap_json_result_is_still_valid_json(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        out = self._over_cap_bash_output(runtime, workspace)

        payload = json.loads(out)
        assert payload["returncode"] == 0
        assert payload["stdout"].startswith("y")

    def test_the_disk_pointer_survives_inside_the_json(self, workspace: Path) -> None:
        """Staying parseable must not cost the model the pointer to the spill."""
        runtime = _runtime(workspace)
        out = self._over_cap_bash_output(runtime, workspace)

        match = re.search(r"saved to (\S+?)\. ", out)
        assert match is not None, out[-400:]
        assert Path(match.group(1)).is_file()

    def test_the_returncode_still_reaches_the_grounding_observations(
        self, workspace: Path,
    ) -> None:
        """The concrete consumer named in _truncate_tool_output's docstring."""
        from coding_agent.core.runtime import _ToolObservations

        runtime = _runtime(workspace)
        out = self._over_cap_bash_output(runtime, workspace)

        observations = _ToolObservations()
        runtime._record_tool_observation(
            observations, "bash", json.dumps({"command": "irrelevant"}), out,
        )
        assert observations.last_bash_returncode == 0

    def test_the_capped_result_stays_within_the_cap(self, workspace: Path) -> None:
        """The pointer was added after truncation, so it re-breached the cap."""
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES

        runtime = _runtime(workspace)
        out = self._over_cap_bash_output(runtime, workspace)

        assert len(out.encode("utf-8")) <= _MAX_TOOL_RESULT_BYTES

    def test_plain_text_output_still_gets_a_pointer_and_a_cap_notice(
        self, workspace: Path,
    ) -> None:
        """Non-JSON results have no field to inject into — they keep a footer."""
        runtime = _runtime(workspace)
        (workspace / "big.txt").write_text("y" * (64 * 1024), encoding="utf-8")

        out = runtime._execute_tool("read_file", json.dumps({"path": "big.txt"}))

        assert "Output capped at" in out
        assert "Full output" in out
        assert re.search(r"saved to (\S+?)\. ", out) is not None


class TestAuditIdentity:
    """A5 -- an audit row has to say which session and directory it came from.

    Without that, a day's log is an undifferentiated pile of commands from
    every run that touched the workspace, real and test alike.
    """

    def _rows(self, workspace: Path) -> list[dict]:
        rows: list[dict] = []
        for path in (state_dir(workspace) / "audit").glob("*.jsonl"):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return rows

    def test_rows_carry_session_id_and_cwd(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        runtime._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        rows = self._rows(workspace)
        assert len(rows) == 1
        assert rows[0]["session_id"] == runtime.session.session_id
        assert rows[0]["cwd"] == str(workspace)

    def test_read_only_tools_are_not_audited(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
        runtime._execute_tool("read_file", json.dumps({"path": "a.py"}))
        assert self._rows(workspace) == []

    def test_writers_beyond_the_original_trio_are_audited(self, workspace: Path) -> None:
        """apply_patch and friends changed files and left no trace before."""
        from coding_agent.observability.metrics import AuditLogger, MetricsCollector

        metrics = MetricsCollector(
            audit_logger=AuditLogger(workspace, enabled=True, session_id="sid123"),
        )
        metrics.record_tool_audit(
            "apply_patch", json.dumps({"path": "x.py"}), "ok",
            is_error=False, permission="workspace-write",
        )
        rows = self._rows(workspace)
        assert [r["tool_name"] for r in rows] == ["apply_patch"]
        assert rows[0]["session_id"] == "sid123"

    def test_rows_follow_the_session_after_a_clear(self, workspace: Path) -> None:
        """/clear and /resume rebind runtime.session — the stamp must follow.

        The id was snapshotted into AuditLogger at construction, so from the
        first /clear onward every row carried the *first* session's id while
        the metrics snapshot and compaction archives written beside it used
        the new one. Correlating an audit row back to its session — the only
        reason the field exists — silently stopped working at session two.
        """
        from coding_agent.core.session import Session

        runtime = _runtime(workspace)
        first_id = runtime.session.session_id
        runtime._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        runtime.session = Session(model=runtime.session.model)  # what /clear does
        second_id = runtime.session.session_id
        assert second_id != first_id

        runtime._execute_tool("write_file", json.dumps({"path": "b.py", "content": "y = 2\n"}))

        assert [r["session_id"] for r in self._rows(workspace)] == [first_id, second_id]

    def test_a_writer_without_a_path_records_its_argument_keys(self, workspace: Path) -> None:
        from coding_agent.observability.metrics import AuditLogger, MetricsCollector

        metrics = MetricsCollector(
            audit_logger=AuditLogger(workspace, enabled=True, session_id="sid"),
        )
        metrics.record_tool_audit(
            "edit_notebook_cell", json.dumps({"notebook": "n.ipynb", "cell": 2}), "ok",
            is_error=False, permission="workspace-write",
        )
        assert self._rows(workspace)[0]["arg_keys"] == ["cell", "notebook"]


class TestAuditExcludesAgentBookkeeping:
    """Widening the audit gate to "any workspace-write tool" swept in todo_write.

    todo_write is the highest-frequency tool in a session and its row says
    only `arg_keys: ["todos"]` -- zero information, and enough volume to bury
    the file writes and shell commands the log exists to preserve.

    The rule: a tool is audited when it can change something the user owns,
    execute a command, or alter the agent's own authority. todo_write does
    none of those -- it rewrites `state_dir/todos.json`, the agent's private
    scratch copy of a plan the transcript already contains.
    """

    def _rows(self, workspace: Path) -> list[dict]:
        rows: list[dict] = []
        for path in (state_dir(workspace) / "audit").glob("*.jsonl"):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return rows

    def test_todo_write_produces_no_audit_row(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        for n in range(3):
            runtime._execute_tool(
                "todo_write",
                json.dumps({"todos": [{"id": str(n), "content": f"step {n}", "status": "pending"}]}),
            )
        assert self._rows(workspace) == []

    def test_it_does_not_silence_the_writes_around_it(self, workspace: Path) -> None:
        """The exclusion must be surgical: real work either side still lands."""
        runtime = _runtime(workspace)
        runtime._execute_tool("todo_write", json.dumps({"todos": [{"id": "1", "content": "a"}]}))
        runtime._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))
        runtime._execute_tool("todo_write", json.dumps({"todos": [{"id": "1", "content": "b"}]}))

        assert [r["tool_name"] for r in self._rows(workspace)] == ["write_file"]

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            # Rewrites the agent's own config -- including the permission mode
            # that decides what it may do next. Agent-owned state, but exactly
            # the kind of change an audit log exists to preserve.
            ("config_write", {"key": "runtime.permission_mode", "value": "danger-full-access"}),
            # Durable instructions that steer every later session, and at
            # workspace scope the file lands inside the user's own project.
            ("memory_save", {"name": "n", "description": "d", "type": "user", "content": "c"}),
        ],
    )
    def test_other_agent_state_writers_are_still_audited(
        self, workspace: Path, tool_name: str, arguments: dict,
    ) -> None:
        from coding_agent.observability.metrics import AuditLogger, MetricsCollector

        metrics = MetricsCollector(
            audit_logger=AuditLogger(workspace, enabled=True, session_id="sid"),
        )
        metrics.record_tool_audit(
            tool_name, json.dumps(arguments), "ok",
            is_error=False, permission="workspace-write",
        )
        assert [r["tool_name"] for r in self._rows(workspace)] == [tool_name]

    def test_the_exclusion_is_a_rule_not_a_grab_bag(self) -> None:
        """Every excluded name must still be a registered workspace-write tool.

        Guards the set against rotting into a list of dead names, and forces a
        re-read of the rule if one of them is ever promoted to touching user
        files.
        """
        from coding_agent.config import load_app_config
        from coding_agent.observability.metrics import _AGENT_STATE_ONLY_TOOLS

        registry = ToolRegistry(Path.cwd(), load_app_config(None), None)
        permissions = dict(registry.permission_specs())
        for name in _AGENT_STATE_ONLY_TOOLS:
            assert permissions.get(name) == "workspace-write", name


class TestMetricsSnapshot:
    """A11 -- MetricsCollector.save() had no caller anywhere."""

    def test_a_turn_writes_a_session_keyed_snapshot(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        runtime._persist_metrics()

        path = state_dir(workspace) / "metrics" / f"metrics_{runtime.session.session_id}.json"
        assert path.is_file()
        assert "tools" in json.loads(path.read_text(encoding="utf-8"))

    def test_two_sessions_do_not_overwrite_each_other(self, workspace: Path) -> None:
        _runtime(workspace)._persist_metrics()
        _runtime(workspace)._persist_metrics()
        assert len(list((state_dir(workspace) / "metrics").glob("metrics_*.json"))) == 2


class _StubProvider:
    """Answers every call with the same tool-free response, so a turn ends."""

    def complete(self, messages, tools, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        from coding_agent.core.session import AssistantResponse

        return AssistantResponse(text="worker output")


class TestSubagentMetricsSnapshots:
    """One metrics file per *worker* is one file per throwaway uuid.

    `AgentRuntime.__init__` mints a fresh Session whenever the caller doesn't
    supply one, and `AdminCoordinator._run_worker` builds one AgentRuntime per
    worker -- so a single orchestrated turn dropped a metrics_<uuid>.json for
    every research/work worker and every retry, each keyed by an id that
    appears in no session file, no audit row and no compaction archive, with
    nothing to clean them up. tests/conftest.py already records the shape of
    this failure ("1,249 directories after a single day's work once metrics
    started being written per turn") but only isolated the *test* home
    directory; the real workspace kept accumulating.

    The turn those workers ran for belongs to the parent runtime, so that is
    the snapshot worth keeping -- and until now the coordinator path wrote no
    parent snapshot at all.
    """

    def _snapshots(self, workspace: Path) -> list[str]:
        return sorted(p.name for p in (state_dir(workspace) / "metrics").glob("*.json"))

    def _config(self, orchestration_mode: str):  # noqa: ANN202
        from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions

        return AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(
                permission_mode="danger-full-access",
                orchestration_mode=orchestration_mode,
                max_worker_steps=2,
            ),
        )

    def test_a_coordinator_turn_writes_one_snapshot_not_one_per_worker(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coding_agent.core.coordinator import AdminCoordinator, TaskPlan

        monkeypatch.setattr(
            AdminCoordinator, "_plan_task",
            lambda self, prompt, cb: TaskPlan(
                is_simple=False,
                research_tasks=["look A up", "look B up"],
                work_tasks=["do A", "do B"],
                validation_criteria=[],
            ),
        )
        runtime = AgentRuntime(workspace, self._config("multi"), provider=_StubProvider())  # type: ignore[arg-type]

        runtime.orchestrate("do a multi-step task")

        assert self._snapshots(workspace) == [f"metrics_{runtime.session.session_id}.json"]

    def test_a_top_level_turn_still_writes_its_own_snapshot(self, workspace: Path) -> None:
        """The suppression must be about being a subagent, not about turns."""
        runtime = AgentRuntime(workspace, self._config("single"), provider=_StubProvider())  # type: ignore[arg-type]

        runtime.run_turn("do one thing")

        assert self._snapshots(workspace) == [f"metrics_{runtime.session.session_id}.json"]


# ---------------------------------------------------------------------------
# Retention: nothing under state_dir was ever deleted
# ---------------------------------------------------------------------------


class _StubPopen:
    """Enough of a Popen for register_background_task; nothing here polls it."""

    def poll(self) -> None:
        return None


def _aged_file(directory: Path, name: str, *, size: int, age_days: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("x" * size, encoding="utf-8")
    when = time.time() - age_days * 86_400
    os.utime(path, (when, when))
    return path


def _dir_bytes(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


class TestStateRetention:
    """Nothing under `state_dir` was ever removed.

    Every over-cap tool call left a full copy of its output in `tool_output/`
    (128 KB at a time from bash alone), every background command left its log
    in `bash_output/`, and `archives/`, `checkpoints/` and `metrics/` each grew
    a file per compaction, checkpoint and turn. The deployment target is a
    locked-down company Windows box with no admin rights, so there is no
    cleanup job anyone can install: if the agent does not bound its own state
    tree, nothing does.
    """

    def test_an_over_budget_spill_directory_is_trimmed_oldest_first(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "1")
        spill_dir = state_dir(workspace) / "tool_output"
        files = [
            _aged_file(spill_dir, f"old_{i}.txt", size=200 * 1024, age_days=10 - i)
            for i in range(10)
        ]

        _runtime(workspace)._spill_tool_output("read_file", "y" * 1000)

        assert _dir_bytes(spill_dir) <= 1024 * 1024
        assert not files[0].exists(), "the oldest spill should go first"
        assert files[-1].exists(), "the newest spills must survive"

    def test_a_stale_spill_goes_even_when_the_directory_is_small(
        self, workspace: Path,
    ) -> None:
        """Age is the second limit: a month-old spill helps nobody."""
        spill_dir = state_dir(workspace) / "tool_output"
        stale = _aged_file(spill_dir, "stale.txt", size=64, age_days=30)
        recent = _aged_file(spill_dir, "recent.txt", size=64, age_days=1)

        _runtime(workspace)._spill_tool_output("read_file", "y" * 1000)

        assert not stale.exists()
        assert recent.exists()

    def test_a_file_written_moments_ago_is_never_deleted(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A just-written file may be one a live process is still appending to,
        or the spill whose path was handed to the model on the line above."""
        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "1")
        spill_dir = state_dir(workspace) / "tool_output"
        fresh = [
            _aged_file(spill_dir, f"fresh_{i}.txt", size=800 * 1024, age_days=0)
            for i in range(2)
        ]

        _runtime(workspace)._spill_tool_output("read_file", "y" * 1000)

        assert all(p.exists() for p in fresh), "the budget must not evict live files"

    def test_a_registered_background_task_log_is_never_deleted(
        self, workspace: Path, registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`registry.background_tasks` is the only reliable in-use signal.

        A background command can run for hours with its log untouched in
        between writes, so mtime alone would happily delete the file its own
        process still holds open -- and the runtime reads that file back when
        the task finishes.
        """
        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "1")
        bash_dir = state_dir(workspace) / "bash_output"
        live = _aged_file(bash_dir, "live.log", size=900 * 1024, age_days=2)
        finished = _aged_file(bash_dir, "finished.log", size=900 * 1024, age_days=3)
        registry.register_background_task("42", "sleep 999", _StubPopen(), live)  # type: ignore[arg-type]

        from coding_agent.tools.shell import _bash_output_path

        _bash_output_path(registry)

        assert live.exists(), "deleting a running task's log breaks the read that follows"
        assert not finished.exists()

    def test_web_fetch_artifacts_share_the_spill_budget(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """They live one level down, in `tool_output/web_fetch/`."""
        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "1")
        artifacts = state_dir(workspace) / "tool_output" / "web_fetch"
        pages = [
            _aged_file(artifacts, f"page_{i}.md", size=400 * 1024, age_days=9 - i)
            for i in range(5)
        ]

        _runtime(workspace)._spill_tool_output("read_file", "y" * 1000)

        assert _dir_bytes(state_dir(workspace) / "tool_output") <= 1024 * 1024
        assert not pages[0].exists()

    def test_the_metrics_snapshot_prunes_its_own_directory(self, workspace: Path) -> None:
        metrics_dir = state_dir(workspace) / "metrics"
        stale = _aged_file(metrics_dir, "metrics_old.json", size=64, age_days=90)

        _runtime(workspace)._persist_metrics()

        assert not stale.exists()

    def test_checkpoints_and_archives_prune_their_own_directories(
        self, workspace: Path,
    ) -> None:
        runtime = _runtime(workspace)
        stale_cp = _aged_file(
            state_dir(workspace) / "checkpoints", "checkpoint_old.json", size=64, age_days=90,
        )
        stale_ar = _aged_file(
            state_dir(workspace) / "archives", "old_archive.json", size=64, age_days=90,
        )

        runtime.checkpoint("label")
        runtime._archive_before_compact()

        assert not stale_cp.exists()
        assert not stale_ar.exists()

    def test_the_limits_are_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coding_agent.observability.metrics import retention_limits

        assert retention_limits("tool_output") == (32 * 1024 * 1024, 7)

        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "5")
        monkeypatch.setenv("YUCODE_STATE_RETENTION_DAYS", "1")
        assert retention_limits("tool_output") == (5 * 1024 * 1024, 1)

        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "0")
        assert retention_limits("tool_output")[0] == 0, "0 disables the byte cap"

    def test_the_sweep_is_not_repeated_on_every_write(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The state tree can be a roaming-profile network path on the target
        box, where a stat-walk per bash call is not free."""
        from coding_agent.observability import metrics as metrics_module

        calls: list[Path] = []
        monkeypatch.setattr(
            metrics_module, "sweep_state_dir",
            lambda directory, **kwargs: calls.append(directory) or [],
        )
        root = state_dir(workspace)
        for _ in range(3):
            metrics_module.prune_state_subdir(root, "tool_output")

        assert len(calls) == 1


class TestAuditIsNotSwept:
    """The audit trail is deliberately exempt from retention.

    It answers "what did the agent do to this machine?" on a box with no other
    audit mechanism, so size-based deletion destroys the evidence exactly when
    there was most of it. It also does not need the help: a row is written only
    for a state-changing tool call and is a few hundred bytes, already rotated
    one file per day -- a heavy day costs ~1 MB against the tens of MB a single
    session can spill. Its growth is bounded by actions actually taken, not by
    how much output a tool happened to produce.
    """

    def test_audit_and_sessions_have_no_retention_policy(self) -> None:
        from coding_agent.observability.metrics import _RETENTION

        assert "audit" not in _RETENTION, "deleting the audit trail destroys the evidence"
        assert "sessions" not in _RETENTION, "the user's own conversation history"

    def test_an_ancient_audit_file_survives_a_sweep(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even asked directly, and even far over any other directory's budget."""
        from coding_agent.observability.metrics import prune_state_subdir

        monkeypatch.setenv("YUCODE_STATE_RETENTION_MB", "1")
        monkeypatch.setenv("YUCODE_STATE_RETENTION_DAYS", "1")
        root = state_dir(workspace)
        ancient = _aged_file(root / "audit", "2019-01-01.jsonl", size=2 * 1024 * 1024, age_days=2000)

        prune_state_subdir(root, "audit")

        assert ancient.exists()


# ---------------------------------------------------------------------------
# The per-tool-result cap bounded one leaf, not the whole result
# ---------------------------------------------------------------------------


class TestToolResultTotalCap:
    """`_shrink_json_strings` shortens string leaves over `max_bytes // 4`, and
    `_truncate_tool_output` returned the re-serialised payload without ever
    measuring it. Two shapes slipped through: a `read_files` over ten files is
    many mid-sized leaves and no large one, so nothing shrank and the result
    fell through to byte-slicing the JSON (unparseable, which is the one thing
    the function exists to prevent); and one huge leaf among many mid-sized
    ones shrank only the huge one and returned the rest at full size -- valid
    JSON, several times the cap it was asked to enforce.
    """

    def _files_payload(self, count: int, leaf_chars: int, big_leaf: int = 0) -> str:
        entries: list[dict[str, str]] = [
            {"path": f"pkg/mod{i}.py", "content": "z" * leaf_chars} for i in range(count)
        ]
        if big_leaf:
            entries.append({"path": "pkg/huge.py", "content": "Z" * big_leaf})
        return json.dumps(entries, indent=2)

    def test_one_shrunk_leaf_does_not_licence_the_rest(self) -> None:
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES, _truncate_tool_output

        payload = self._files_payload(20, 6_000, big_leaf=60_000)
        out = _truncate_tool_output(payload, _MAX_TOOL_RESULT_BYTES)

        assert len(out.encode("utf-8")) <= _MAX_TOOL_RESULT_BYTES
        json.loads(out)

    def test_many_mid_sized_leaves_stay_parseable(self) -> None:
        """No single leaf is over the per-leaf cap, so nothing was shrunk."""
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES, _truncate_tool_output

        payload = self._files_payload(20, 6_000)
        out = _truncate_tool_output(payload, _MAX_TOOL_RESULT_BYTES)

        parsed = json.loads(out)
        assert len(out.encode("utf-8")) <= _MAX_TOOL_RESULT_BYTES
        assert parsed[0]["path"] == "pkg/mod0.py"
        assert "truncated" in parsed[0]["content"]

    def test_the_shrunk_leaf_reports_the_original_size(self) -> None:
        """Re-shrinking an already-shrunk value would report the excerpt's
        length as the total, and stack one marker per pass."""
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES, _truncate_tool_output

        out = _truncate_tool_output(self._files_payload(20, 6_000), _MAX_TOOL_RESULT_BYTES)

        content = json.loads(out)[0]["content"]
        assert content.count("[truncated;") == 1
        assert "6,000 chars total" in content

    def test_the_spill_pointer_survives_the_tighter_shrink(self) -> None:
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES, _truncate_tool_output

        note = "Full output (240,000 chars) saved to /state/tool_output/read_files_ab.txt."
        out = _truncate_tool_output(self._files_payload(20, 6_000), _MAX_TOOL_RESULT_BYTES, note)

        assert json.loads(out)[-1] == note
        assert len(out.encode("utf-8")) <= _MAX_TOOL_RESULT_BYTES

    def test_a_small_json_result_is_left_alone(self) -> None:
        """The tighter budget must not start mangling results that already fit."""
        from coding_agent.core.runtime import _MAX_TOOL_RESULT_BYTES, _truncate_tool_output

        payload = json.dumps({"returncode": 0, "stdout": "hello", "stderr": ""})
        out = _truncate_tool_output(payload, _MAX_TOOL_RESULT_BYTES)

        assert "hello" in out
        assert "truncated" not in out


# ---------------------------------------------------------------------------
# Subagent audit rows named a session that does not exist
# ---------------------------------------------------------------------------


class _WritingProvider:
    """Emits one write_file tool call on its first call, then plain text.

    Enough for a coordinator worker to leave a real audit row without any of
    the later planner/validator calls tripping over a tool call they have no
    tools for.
    """

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        from coding_agent.core.session import AssistantResponse, ToolCall

        self.calls += 1
        if self.calls == 1:
            return AssistantResponse(text="", tool_calls=[ToolCall(
                id="call_1", name="write_file",
                arguments=json.dumps({"path": "worker.py", "content": "x = 1\n"}),
            )])
        return AssistantResponse(text="worker output")


class TestSubagentAuditIdentity:
    """A subagent's rows were filed under an id that names nothing.

    Every subagent runtime mints its own Session (`AgentRuntime.__init__` does
    `session or Session(...)`), and the audit row carried that uuid -- an id
    matching no session file, no compaction archive and, since subagents
    stopped writing metrics snapshots, no metrics file either. A day's log was
    part real rows and part rows nobody could trace to anything.

    The fix is emphatically not to stop auditing subagents: a worker's bash
    commands and file writes are exactly what the trail exists to record. It is
    to file the row under the session that owns the turn, and keep the worker's
    own id as `subagent_id` so its rows stay groupable and visibly a worker's.
    """

    def _rows(self, workspace: Path) -> list[dict]:
        rows: list[dict] = []
        for path in (state_dir(workspace) / "audit").glob("*.jsonl"):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return rows

    def _subagent_of(self, workspace: Path, parent: AgentRuntime) -> AgentRuntime:
        """Build a runtime the way a worker is built: inside the parent's turn."""
        from coding_agent.core.runtime import _turn_in_flight

        with _turn_in_flight(parent.session.session_id):
            return _runtime(workspace)

    def test_a_subagent_row_is_filed_under_the_parent_session(self, workspace: Path) -> None:
        parent = _runtime(workspace)
        child = self._subagent_of(workspace, parent)

        child._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        rows = self._rows(workspace)
        assert [r["session_id"] for r in rows] == [parent.session.session_id]

    def test_the_row_still_says_which_subagent_wrote_it(self, workspace: Path) -> None:
        parent = _runtime(workspace)
        child = self._subagent_of(workspace, parent)

        child._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        row = self._rows(workspace)[0]
        assert row["subagent_id"] == child.session.session_id
        assert row["subagent_id"] != row["session_id"]

    def test_a_subagent_still_writes_its_rows(self, workspace: Path) -> None:
        """Metrics went the other way -- a statistic, not evidence. A worker's
        bash command is the single most audit-worthy thing the agent does."""
        parent = _runtime(workspace)
        child = self._subagent_of(workspace, parent)

        child._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        assert [r["tool_name"] for r in self._rows(workspace)] == ["write_file"]

    def test_a_top_level_row_carries_no_subagent_id(self, workspace: Path) -> None:
        runtime = _runtime(workspace)
        runtime._execute_tool("write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))

        assert "subagent_id" not in self._rows(workspace)[0]

    def test_a_coordinator_worker_files_under_the_parent_session(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end through the path that mints the throwaway sessions."""
        from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions
        from coding_agent.core.coordinator import AdminCoordinator, TaskPlan

        monkeypatch.setattr(
            AdminCoordinator, "_plan_task",
            lambda self, prompt, cb: TaskPlan(
                is_simple=False, research_tasks=[], work_tasks=["write the file"],
                validation_criteria=[],
            ),
        )
        config = AppConfig(
            provider=ProviderConfig(name="test", api_key="k", model="m"),
            runtime=RuntimeOptions(
                permission_mode="danger-full-access",
                orchestration_mode="multi",
                max_worker_steps=2,
            ),
        )
        runtime = AgentRuntime(workspace, config, provider=_WritingProvider())  # type: ignore[arg-type]

        runtime.orchestrate("do a multi-step task")

        rows = [r for r in self._rows(workspace) if r["tool_name"] == "write_file"]
        assert rows, "the worker's file write must still be audited"
        assert [r["session_id"] for r in rows] == [runtime.session.session_id]
        assert all(r["subagent_id"] and r["subagent_id"] != r["session_id"] for r in rows)
