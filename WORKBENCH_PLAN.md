# Plan: run yucode on the company Windows PC via the KPMG Workbench gateway (GPT-5)

**Audience:** the implementing agent (Claude Sonnet 5) working in this repo on the Mac.
**End user:** fills in real keys/charge-code in `settings.yml` later, on the Windows company PC.
**Prime directive:** follow `CLAUDE.md` — simplicity first, surgical changes, verifiable goals. Everything below that can be config-only must stay config-only.

---

## 1. Goal

Make **Workbench/GPT-5.5 the primary provider of yucode** (full switch-over, not an add-on) and make `yucode chat` work on a locked-down company Windows PC:

- Python only, **no admin access**, possibly no usable `pip` network access, no `rg`.
- LLM access ONLY through the **KPMG Workbench gateway** (Azure-OpenAI-compatible APIM), serving GPT-5-class reasoning models with `reasoning_effort: high|medium|low`.
- GPT-5.5 replaces Qwen3-32B as the reference model. The weak-tier machinery stays in the tree but is **dormant** (gpt-5 resolves to strong tier) — do not delete it; see `AGENT_UPGRADE_NOTES.md` §2.
- The user will paste the API key / charge code into the config on the company PC — ship a template with clearly marked `FILL-ME` placeholders.

Companion documents:
- `AGENT_UPGRADE_NOTES.md` — code-structure review (tool surface, security, intelligence-tier renovation) with prioritised follow-ups.
- `.yucode/skills/fdd-commentary/SKILL.md` — FDD databook commentary skill to trial on the company PC (ships in the bundle, WI-6).

## 2. Verified facts about the Workbench gateway

Source of truth: a working production client in the sibling repo
`/Users/ytsang/Desktop/Github/python-pptx/` (do not modify that repo).

| Fact | Where verified |
|---|---|
| Endpoint style: `AzureOpenAI(base_url="https://api.workbench.kpmg/genai/azure/openai", api_version="2024-12-01-preview")` → i.e. `POST {base}/deployments/{model}/chat/completions?api-version={v}` | `fdd_utils/ai.py:2660-2680`, `fdd_utils/config.yml` workbench section |
| Auth: subscription key sent as **both** `api-key` (Azure SDK default header) and `Ocp-Apim-Subscription-Key`; plus `x-kpmg-charge-code` and `x-kpmg-region-override` headers. Reference sends them as default headers AND per-call `extra_headers` | `ai.py:2666-2679`, `ai.py:3008-3014` |
| Models: `gpt-5-5-2026-04-24-gs-sdc` (GPT-5.5), `gpt-5-4-2026-03-05-gs-sdc` (GPT-5.4) | `fdd_utils/config.yml` `available_models` |
| Model rejects `temperature` (only its default 1), rejects `top_p` / `frequency_penalty` / `presence_penalty`, requires `max_completion_tokens` (rejects `max_tokens`), accepts `reasoning_effort` (`high|medium|low`) | `fdd_utils/config.yml:37-39`, `ai.py:3016-3031` |
| On an unrecognised 400, the error body names the offending param — the reference client parses it, drops/renames the param, retries up to 4 rounds ("self-heal") | `ai.py:3053-3100` |
| Reasoning models burn hidden reasoning tokens from the completion budget → too-small `max_completion_tokens` yields HTTP 200 with **empty content** | `ai.py:3033-3051`, `test_workbench_connectivity.py:54-68` |
| TLS: corporate MITM — reference uses `verify=False`; timeout 180 s | `ai.py:2678` |
| Streaming and function-calling (`tools` param) support are **unverified** through this gateway — the pptx pipeline uses neither | gap; addressed by WI-4 |

## 3. What yucode already handles with config only (no code change)

`coding_agent/core/providers.py` is a zero-dependency urllib OpenAI-compatible client. Today's config (`ProviderConfig`, `coding_agent/config/settings.py:83`) already supports:

- Absolute `chat_path` URL incl. query string (`providers.py:250-255`) → can hit the Azure `deployments/...?api-version=...` URL.
- `extra_headers` → can carry `api-key`, `Ocp-Apim-Subscription-Key`, `x-kpmg-charge-code`, `x-kpmg-region-override`. Leave `api_key` empty so no `Authorization: Bearer` is sent.
- `extra_body` → can carry `reasoning_effort`. (yucode never sends `max_tokens`, so the max_completion_tokens rename issue doesn't arise; do NOT add a small token cap — see empty-content fact above.)
- `verify_tls: false`, `streaming_mode: no_stream`, `request_timeout_seconds`.
- Tier: `gpt-5*` doesn't match `_WEAK_MODEL_HINTS` → resolves **strong**; none of the weak-tier machinery activates. Correct — don't touch.
- Windows: system prompt already states the platform (`memory/prompting.py:109`); `bash` tool uses `shell=True` → `cmd.exe` on Windows; `grep_search` has a pure-Python fallback when `rg` is missing (`tools/filesystem.py:539-599`).

**The one hard blocker:** `_do_complete` **always** sends `temperature` (`providers.py:185`). GPT-5-class models 400 on it. `extra_body: {temperature: 1}` *might* pass (unverified — the reference client strips the param instead), so fix it properly in code (WI-1).

## 4. Work items (do in order; each has a verify step)

### WI-1 — provider: param omission + 400 self-heal  *(the essential change)*
In `coding_agent/core/providers.py` + `ProviderConfig`:
1. New config field `omit_params: list[str]` (default `[]`). After the body is fully built (including `extra_body` merge), delete these keys. Template sets `omit_params: [temperature]`.
2. Port the self-heal from `ai.py:3053-3100`: on HTTP 400, parse the rejected param name from the error body (Azure format: `{"error": {..., "param": "temperature"}}`; also regex the message text as fallback). If found: remember it in a per-provider-instance omit set, rebuild body, retry immediately. Bound at 4 rounds. Learned omissions persist for the rest of the session so only the first call pays the extra round-trip. Handle the `max_tokens` ↔ `max_completion_tokens` rename pair specially (swap, not drop).
- **Verify:** unit tests with a fake urlopen: (a) omit_params removes key; (b) 400-with-param triggers retry without it and second call succeeds; (c) learned omission applies to subsequent calls; (d) rename pair swaps; (e) non-param 400 still raises ProviderError. Full suite `python -m pytest tests/ -x -q` stays green.

### WI-2 — provider: `api_version` for Azure-style URLs
New `ProviderConfig.api_version: str = ""`. When non-empty, `_build_url()` returns `{base_url}/deployments/{model}/chat/completions?api-version={api_version}` and auth uses header `api-key: {api_key}` instead of `Authorization: Bearer` (Azure convention). `chat_path`/`append_chat_path` ignored in this mode. Rationale: without this, the model id must be duplicated inside `chat_path`, and switching GPT-5.5 ↔ 5.4 means editing two fields — error-prone for the user filling config on-site.
- **Verify:** unit tests on `_build_url()` + `_headers()` for api_version set/unset. Config round-trips through `simple_yaml`.

### WI-3 — workbench becomes the default provider
Full switch-over, per the owner's decision (GPT-5.5 ≫ Qwen3-32B):
1. Replace the `provider:` section of the bundled `coding_agent/config/config.yml` with the workbench settings from §6 (`api_key`/`Ocp-Apim-Subscription-Key`/charge-code as empty strings — the real values live only in the user's `~/.yucode/settings.yml`). Keep the old deepseek block as a commented-out example.
2. Set the bundled runtime defaults to the §5 posture (`orchestration_mode: single`, `streaming_mode: no_stream`, `request_timeout_seconds: 300`).
3. Also check in the full user-facing template from §6 (e.g. `docs/settings.workbench.yml`) for copy-paste onto the company PC. Never commit real keys.
- **Verify:** `load_yaml` parses both; a config loaded from the template produces the expected URL/headers/body in a unit test; existing tests that assume deepseek defaults are updated deliberately (not papered over).

### WI-4 — extend the connectivity probe (critical for day-1 on-site)
Extend the existing standalone, dependency-free `test_connection.py` (repo root — it's designed to be edited/run on locked-down machines):
1. `--azure` mode / azure candidates: build the `deployments/{model}/chat/completions?api-version=` URL; send `api-key` + `Ocp-Apim-Subscription-Key` + kpmg headers; body WITHOUT `temperature`.
2. `--test-tools` probe: send one trivial tool (e.g. `get_weather`) with `tool_choice: "auto"` and a prompt that forces a call; report whether `tool_calls` comes back. **This decides whether the agent works at all through the gateway.**
3. Streaming probe against the azure URL: report whether SSE chunks arrive (decides `no_stream` vs `hybrid`).
- **Verify:** run it against a public OpenAI-compatible endpoint (e.g. DeepSeek, key available locally) to confirm the probe logic itself; the workbench run happens on-site.

### WI-5 — REPL fallback when `prompt_toolkit` is absent
One-shot mode already works without it (lazy import at `cli.py:1049` inside `_make_pt_session`). Interactive chat hard-crashes. Add a minimal fallback: if the import fails, use plain `input()` (no completion/history) and print a one-line notice. Justified, not speculative: the target box may not allow pip at all, and the copy-folder deployment (WI-6) then has no site-packages.
- **Verify:** unit test that the fallback path returns a working prompt function; manually run `yucode chat` in a venv without prompt_toolkit.

### WI-6 — Windows no-admin delivery bundle
Refresh and package. Note `yucode-deliver/` at repo root is a **stale old snapshot** — regenerate it from the current tree (after WI-1…5) rather than patching it.
1. Build artifacts: project wheel + `pip download prompt_toolkit -d wheels/` (pure-Python: prompt_toolkit + wcwidth). Also include `openpyxl` (+ `et_xmlfile`) wheels — the FDD skill's Excel path needs it and it's pure-Python; python-pptx/docx wheels optional (likely already on the box).
2. `install.bat`: `py -m pip install --user --no-index --find-links=wheels yucode_agent-*.whl prompt_toolkit openpyxl` + note that the user Scripts dir may not be on PATH → always document `py -m coding_agent.interface.cli chat` as the canonical invocation.
3. Zero-install fallback (pip blocked entirely): copy the folder, run `py -m coding_agent.interface.cli chat --workspace .` from repo root — works because core is stdlib-only (REPL degraded per WI-5; FDD skill falls back to the databook .txt export instead of .xlsx).
4. Include the skill: copy `.yucode/skills/fdd-commentary/` into the bundle with an install note — on the company PC it goes to `%USERPROFILE%\.yucode\skills\fdd-commentary\SKILL.md` (user-global discovery) or `<workspace>\.yucode\skills\` per project.
5. A short `README-WINDOWS.md` in the bundle: pre-flight checks (§7), where `settings.yml` lives (`%USERPROFILE%\.yucode\settings.yml`), the FILL-ME list, the skill install path.
- **Verify:** on the Mac, simulate offline install into a fresh venv with `--no-index --find-links`; run the test suite from the bundle copy; `list_skills()` finds the skill from a fake home dir.
- **Found + fixed during this verification:** `pyproject.toml` didn't declare `tomli` as a dependency, even though `coding_agent/__init__.py` does `import tomli as tomllib` on Python <3.11. This was invisible in every dev/CI run because `black`/`pytest`/`mypy`/`build` all pull `tomli` in transitively — but a genuinely clean `pip install` on a Python 3.10 box (exactly the company-PC scenario) crashed on the very first `import coding_agent`. Fixed with `"tomli>=2.0; python_version < '3.11'"` in `dependencies`; regression-tested in `tests/test_core.py::test_pyproject_declares_tomli_for_python_310`. Lesson: always verify an offline install into a *fresh* venv, not just `PYTHONPATH=.` against the dev environment — the dev environment's stray transitive packages mask exactly this class of bug.

### WI-7 (optional, after on-site smoke passes) — per-phase `reasoning_effort`
Workbench accepts per-call `reasoning_effort`. Today yucode can only set it globally via `extra_body`. Small enhancement: let the coordinator/runtime pass a per-call override (planner/validator → `high`, workers → `medium`, compaction/synthesis → `low`), mirroring the per-agent overrides in the pptx reference (`fdd_utils/config.example.yml` `agents.2_Auditor.reasoning_effort`). Design sketch in `AGENT_UPGRADE_NOTES.md` §2. Skip if `medium` everywhere proves good enough — measure first.

## 5. Recommended runtime posture for GPT-5.5 (config, not code)

- `orchestration_mode: single` — GPT-5.5 is strong-tier; the multi-phase coordinator exists to babysit weak models and multiplies billable gateway calls (charge-code!). Single-agent loop with full toolset is the right default here. (`auto` also behaves sanely, but `single` is predictable for cost.)
- `streaming_mode: no_stream` until the WI-4 probe proves SSE works through APIM; `hybrid` won't save you because a 400/buffered stream isn't the "empty response" case hybrid falls back on.
- `request_timeout_seconds: 300` — reasoning models routinely exceed the 90 s default in no_stream mode (reference uses 180 s for single short completions; agent turns are longer).
- `extra_body: {reasoning_effort: "medium"}` — good default for agentic tool loops; the user can raise to `high` for hard tasks. Per-phase effort is WI-7 (later). Do NOT set `max_completion_tokens` (empty-content trap, §2).
- Do not switch to the `openai` SDK even though it exists on the company box — the zero-dependency urllib client is the deployment advantage; keep it.

## 6. `settings.yml` template (user fills FILL-ME on the company PC)

```yaml
# %USERPROFILE%\.yucode\settings.yml
provider:
  name: workbench
  type: openai_compatible
  base_url: "https://api.workbench.kpmg/genai/azure/openai"
  api_version: "2024-12-01-preview"        # WI-2: builds .../deployments/{model}/chat/completions?api-version=...
  model: "gpt-5-5-2026-04-24-gs-sdc"       # GPT-5.5; alt: gpt-5-4-2026-03-05-gs-sdc
  api_key: "FILL-ME"                        # sent as api-key header (WI-2)
  verify_tls: false                         # corporate TLS interception
  streaming_mode: no_stream                 # revisit after WI-4 probe
  request_timeout_seconds: 300
  omit_params: [temperature]                # WI-1: GPT-5 rejects non-default temperature
  extra_headers:
    Ocp-Apim-Subscription-Key: "FILL-ME"    # same value as api_key
    x-kpmg-charge-code: "FILL-ME"           # e.g. "0000"
    x-kpmg-region-override: "westeurope"
  extra_body:
    reasoning_effort: "medium"
runtime:
  orchestration_mode: single
```

## 7. On-site checklist (user runs on the company PC, in order)

1. `py --version` → must be **≥ 3.10** (yucode uses 3.10+ syntax). If 3.9: STOP, report back — backporting is out of scope.
2. Deploy bundle: run `install.bat`; if pip is blocked, use the copy-folder fallback (WI-6.3).
3. `py test_connection.py --azure ...` with real key → confirm at least one candidate returns content.
4. `py test_connection.py --azure --test-tools ...` → **must** show `tool_calls ≥ 1`. If not, see contingency C-1.
5. Streaming probe → if SSE works, optionally set `streaming_mode: hybrid`.
6. Create `%USERPROFILE%\.yucode\settings.yml` from the template; fill FILL-ME values.
7. Smoke: `py -m coding_agent.interface.cli chat "list the files in this folder and summarise the project" --workspace <some-repo>` → expect tool calls (list/read) then a grounded answer.
8. Interactive: `py -m coding_agent.interface.cli chat --workspace .`
9. Install the skill: copy `fdd-commentary` to `%USERPROFILE%\.yucode\skills\fdd-commentary\SKILL.md`, then in a workspace containing a databook run: `use the fdd-commentary skill: write commentary for Cash and Accounts receivable` → expect it to load the skill, read the databook, and produce styled commentary with a self-audit note.

## 8. Risks & contingencies

- **C-1: gateway strips/rejects the `tools` param** → the agent is dead without function calling. Contingency (build ONLY if the probe fails): prompt-embedded tool calling — inject tool schemas into the system prompt and instruct the model to emit `<tool_call>{...}</tool_call>`; the text-tag parser already exists (`providers.py:646`, `_extract_text_tool_calls`). Non-trivial; do not build speculatively.
- **C-2: SSE unsupported/buffered by APIM** → already covered: ship with `no_stream`.
- **C-3: pip fully blocked** → covered by copy-folder mode (WI-5 + WI-6.3).
- **C-4: company Python 3.9** → hard blocker; detect at step 1 before wasting time.
- **C-5: proxy required** → urllib honours `HTTP(S)_PROXY` env vars automatically; the pptx app reaches the gateway directly from the same box, so likely a non-issue.
- **C-6: `rg` absent** → pure-Python grep fallback already ships; slower on huge trees but functional. Optionally drop a portable `rg.exe` into a user-PATH folder (no admin needed) if IT policy allows.

## 9. Out of scope

- No provider-registry / multi-provider refactor; no switch to the openai SDK.
- No deleting the weak-tier machinery — it is dormant under gpt-5 (strong tier) and harmless; a local-model fallback may return one day. Renovation ideas live in `AGENT_UPGRADE_NOTES.md`, gated behind on-site validation.
- No changes to coordinator logic or tools beyond what's listed (WI-7 only after smoke passes).
- No backport below Python 3.10.
- Real keys/charge codes never enter this repo.
