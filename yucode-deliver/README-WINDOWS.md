# yucode on the company Windows PC — setup guide

This folder is a self-contained deployment bundle: source, offline wheels, the
Workbench connectivity probe, and the FDD commentary skill. No admin rights
and no PyPI network access are required. Background and rationale for every
decision here: `WORKBENCH_PLAN.md` and `AGENT_UPGRADE_NOTES.md` in the main
repo (not included in this bundle — ask if you need them).

## 0. Pre-flight checks

1. **Python version** — open a command prompt and run:
   ```
   py --version
   ```
   Must be **3.10 or newer**. If it reports 3.9 or older, stop here and report
   back — this build does not support it.
2. Note whether `pip` can reach the internet at all (try `py -m pip --version`,
   then later `install.bat`). Either way works — see step 2 below.

## 1. Install

**Option A — offline install (preferred):**
```
install.bat
```
This installs `yucode-agent`, `prompt_toolkit`, and `openpyxl` from the
`wheels\` folder shipped in this bundle — no network access used. If your
user Scripts directory isn't on PATH, the `yucode` command may not be found;
the invocation below always works regardless of PATH:
```
py -m coding_agent.interface.cli chat --workspace .
```

**Option B — zero-install fallback (pip itself is blocked by policy):**
Just run yucode directly from this folder — the core runtime is pure
standard library plus `prompt_toolkit` (optional; falls back to plain input
if missing) and `openpyxl` (optional; only needed for `.xlsx` reads —
the FDD skill falls back to a databook `.txt` export if it's absent):
```
cd yucode-deliver
py -m coding_agent.interface.cli chat --workspace .
```

## 2. Verify the Workbench gateway before touching the agent

Run the connectivity probe with your real key/model/charge-code. This checks
the exact request shape the agent will use — URL, headers, and (critically)
whether the gateway supports function calling, which the agent cannot work
without.

```
py test_connection.py --azure --base-url https://api.workbench.kpmg/genai/azure/openai ^
    --api-key <your-subscription-key> --model gpt-5-5-2026-04-24-gs-sdc ^
    --charge-code <your-charge-code> --test-tools
```

Read the `Summary` section at the end:
- `connectivity` **must** show PASS.
- `function calling` **must** show PASS (if you passed `--test-tools`) — if it
  shows FAIL, the gateway is stripping the `tools` parameter and the agent
  will not be usable until that's resolved; don't proceed to step 4 yet.
- `streaming` is informational only — FAIL here just means keep
  `streaming_mode: no_stream` in your config (already the default below).

## 3. Configure

Copy the template and fill in the placeholders:
```
mkdir %USERPROFILE%\.yucode
copy docs\settings.workbench.yml %USERPROFILE%\.yucode\settings.yml
notepad %USERPROFILE%\.yucode\settings.yml
```

Fields marked `FILL-ME` that you must fill in (never commit this file
anywhere — it holds your real credentials):

| Field | Value |
|---|---|
| `provider.api_key` | Your Workbench subscription key |
| `provider.extra_headers.Ocp-Apim-Subscription-Key` | Same value as `api_key` above |
| `provider.extra_headers.x-kpmg-charge-code` | Your charge code |

Everything else in the template (URL, api-version, model, `omit_params`,
`reasoning_effort`) is already correct for GPT-5.5 through this gateway.

## 4. Install the FDD commentary skill (optional)

```
mkdir %USERPROFILE%\.yucode\skills
xcopy /E /I .yucode\skills\fdd-commentary %USERPROFILE%\.yucode\skills\fdd-commentary
```
Once installed it's available in every workspace. Try it against a workspace
containing a financial databook:
```
use the fdd-commentary skill: write commentary for Cash and Accounts receivable
```

## 5. Smoke test

```
py -m coding_agent.interface.cli chat "list the files in this folder and summarise the project" --workspace <some-project-folder>
```
Expect: the agent calls a listing/read tool, then gives a grounded answer
referencing what it actually found — not a generic response.

Then start interactive mode:
```
py -m coding_agent.interface.cli chat --workspace .
```

## 6. Permission mode — pick one deliberately

yucode's `runtime.permission_mode` controls how much the agent can do without
asking. Set it in `%USERPROFILE%\.yucode\settings.yml` under `runtime:`.

- **`workspace-write`** (ships as the default) lets the agent run *any* shell
  command and write anywhere inside the workspace; the only thing standing
  between the agent and the OS is the pattern-based safety filter (blocks
  things like `del /s /q C:\`, `format C:`, `rm -rf /`, force-pushes, and
  similar catastrophic commands). There is **no OS-level sandbox on
  Windows** — that only exists on Linux. The safety filter catches
  fat-fingered/naive destructive commands; it is not a defense against a
  deliberately adversarial prompt.
- **`prompt`** makes the agent ask before every shell command or file write in
  interactive sessions. Slower, but you see and approve every action before
  it happens. (Note: `prompt` mode has no effect in one-shot mode — there's
  no one there to answer the prompt — so it's an interactive-only safeguard.)

**Recommendation:** use `prompt` for your first few sessions on this machine,
or whenever you're working on something sensitive. Switch to `workspace-write`
once you're comfortable with what the agent tends to do. Either way, know
that read-only mode also exists (`read-only`) if you just want the agent to
investigate without touching anything.

## 7. Where things are

| What | Where |
|---|---|
| Your config (real keys) | `%USERPROFILE%\.yucode\settings.yml` |
| Session history | `%USERPROFILE%\.yucode\projects\<hash>\` |
| Audit log (every bash command + file write, JSONL) | `%USERPROFILE%\.yucode\projects\<hash>\audit\` |
| Command history (interactive REPL) | `%USERPROFILE%\.yucode\history` |

## 8. If something doesn't work

- **`ModuleNotFoundError` for `prompt_toolkit`**: expected if you used the
  zero-install fallback without it — the REPL prints a one-line notice and
  falls back to plain input automatically. Not an error.
- **Office file tools (`read_excel_sheet` etc.) fail with "openpyxl is
  required"**: expected if `openpyxl` wasn't installed — the error message
  tells you the exact install command. Point the agent at a `.txt` databook
  export instead if you can't install it.
- **Gateway rejects a request with HTTP 400 mentioning a parameter name**:
  yucode automatically retries without that parameter and remembers the fix
  for the rest of the session — this should self-heal after the first call.
  If it doesn't, re-run `test_connection.py --azure` and check the response
  body shown in the FAIL output.
- **Anything else**: re-run step 2's connectivity probe — it isolates gateway
  problems from agent problems.
