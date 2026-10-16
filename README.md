# Super Partner

Local-first multi-model orchestration for software and research tasks. It plans work as a dependency graph, routes each node to the cheapest capable configured model, estimates cost, asks for one confirmation, then executes autonomously with an audit trail and budget circuit breaker.

Task planning uses the configured model with the strongest planning and reliability scores to generate a validated JSON dependency graph. Invalid, unavailable, or cyclic model plans fall back to the deterministic planner. Each resulting node is independently assigned to the lowest-cost enabled model that meets its capability and context requirements, while authenticated membership CLIs retain priority.

Planning may use a membership CLI, but subtasks and their reviews go to API models first: a CLI takes a subtask only when no enabled API model can do it (none reaches the capability threshold, or none can act on the project). Each node records why its model was chosen. `SUPER_PARTNER_SUBTASKS_PREFER_API=false` restores CLI-first routing. At startup the app safely detects Codex CLI, Claude Code, Gemini CLI, and Qwen Code without exposing account details. A CLI failure falls back to the matching API only when its API key is configured, that API model is enabled, and its estimated cost fits the remaining budget.

## Run locally

### One-command startup

```bash
./start.sh
```

The first run installs missing backend and frontend dependencies. Open <http://127.0.0.1:5173>, then stop everything with `./stop.sh`. Runtime logs are stored under `.super-partner/logs/`; use `./scripts/service.sh status` or `restart` for service management.

When a systemd user manager is available, the launcher runs the API and web console as
separate user services, independent of the terminal or automation session that started them.
They restart on process failure, and `stop.sh` stops the managed units. Without a user manager,
the launcher uses detached background processes. Logs are appended across starts.

### Manual startup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn super_partner.api:app --reload
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. The built-in mock model works without credentials, so the full workflow is immediately testable. You can also use `docker compose up --build`.

### Frontend interaction checks

Run `npm --prefix frontend test` for DOM interaction tests. They mount the actual app with
isolated task fixtures and cover deletion from the list/detail, cancelling active missions,
dismissing confirmations, retries, network errors, and stale polling responses. Native confirmation dialogs are disabled in
the tests to exercise the embedded-browser case. No live task records are accessed.

## CLI

```bash
super-partner models
super-partner plan "Research and implement a health endpoint" --budget 2
super-partner plan "Use APIs only" --no-use-cli
super-partner plan "Fix the flaky test" --project ~/prj/my-app
super-partner run TASK_ID
```

## Working on an existing project

Give a task a project directory to have membership CLI agents edit that checkout in place:

```bash
super-partner plan "Fix the failing login test" --project ~/prj/my-app
```

In the web console, click the project directory field under the prompt: it suggests recently used
projects and git repositories found under the allowed roots (most recently active first), and checks
any typed or pasted path as you go, showing its branch and uncommitted changes. **Browse…** opens the
desktop's own folder dialog on the machine running the API, starting at the current folder (needs
`DISPLAY`/`WAYLAND_DISPLAY` and `zenity`; the button is hidden otherwise).

- The directory must be an existing absolute path inside `SUPER_PARTNER_PROJECT_ROOTS` (a JSON list, default: your home directory), and cannot be the Super Partner data directory.
- The planner sees the project's top-level layout and git state, and marks nodes that must read or change project files. Only those nodes run in the project; everything else runs in a scratch workspace.
- Project nodes route to membership CLIs or to API models that support tool calls (see below); a CLI project node never falls back to an API. Nodes editing the same project run one at a time, across runs as well.
- Each run records the starting git HEAD, branch, and uncommitted file count as a `workspace.snapshot` event. Agents are told not to commit, so review changes with `git diff` and revert with `git checkout`/`git stash` if needed. Starting from a clean working tree makes that easiest.

## Local-only access

The API can drive CLI agents that edit local files, so it only answers requests whose `Host` is
in `SUPER_PARTNER_ALLOWED_HOSTS` (default `["127.0.0.1", "localhost", "::1"]`, which blocks DNS
rebinding) and refuses state-changing requests from browser origins outside
`SUPER_PARTNER_ALLOWED_ORIGINS` (default: the web console on port 5173). Docker Compose publishes
both ports on `127.0.0.1` only.

If the API stops while a task is queued or running, the task comes back **paused** on the next
start rather than resuming by itself; check the project's state, then resume it from the console.

## Provider credentials

Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `ARK_API_KEY`, `ZHIPU_API_KEY`, `MINIMAX_API_KEY`, or `MOONSHOT_API_KEY`. The API also accepts a provider key at `PUT /api/providers/{provider}/key` and stores it through the operating-system keyring. Secrets are never returned by an API response.

Startup waits up to ten seconds for saved keyring credentials. If the keyring is locked or
unresponsive, the app starts using environment credentials and available CLIs. Unlock the
desktop keyring and restart to load saved API keys.

Seed prices for models marked `pricing_verified: false` are placeholders and must be reviewed before cost-sensitive production use. The local demo model remains the safe default when no provider keys exist.

## Automatic model catalog updates

The **模型画像** page shows the model catalog, per-provider sync results, release dates,
metadata sources, and an **立即更新** button. Models are sorted by release date and can be
filtered by provider. The API refreshes overdue catalogs in the background on startup and
then every 24 hours while the app is running; failed sources retry after one hour.

The public [Models.dev catalog](https://models.dev/) supplies text-model metadata for OpenAI,
Anthropic, Google, DeepSeek, Qwen (China), Doubao, Zhipu, MiniMax (international), and Kimi
(China), matching the app's inference regions. This community-maintained catalog may lag
vendor releases. It supplies reference base prices, not a complete billing calculation
(context tiers, caches, modalities, and regional/account terms may differ).

When keys are configured, the app additionally checks the official model-list APIs for
[OpenAI](https://developers.openai.com/api/reference/resources/models/methods/list),
[Anthropic](https://platform.claude.com/docs/en/api/models/list),
[Google](https://ai.google.dev/api/models), and
[DeepSeek](https://api-docs.deepseek.com/api/list-models/), including pagination. The other
five providers currently use the public catalog. These metadata requests make no inference
calls. Keys are sent only to the corresponding fixed official API host, never to Models.dev.
Being listed by an API does not guarantee inference access for every account or endpoint.

Sync preserves model switches, existing capability scores, explicitly verified prices,
and historical entries. Models absent from the latest public catalog are marked unlisted,
not deleted. Failed or malformed responses leave the last usable catalog intact, including
across restarts. CLI entries continue to represent locally detected channels.

New models have no fabricated scores: unknown capability scores, prices, and context limits
are shown as unknown. Public benchmark results are imported separately, as described below.
New entries start disabled and cannot join routing until the required metadata and an
Artificial Analysis result exist. Retired models and models absent from a successful official
list are excluded from new routing.

Optional `.env` settings:

```dotenv
SUPER_PARTNER_CATALOG_AUTO_SYNC=true
SUPER_PARTNER_CATALOG_SYNC_INTERVAL_HOURS=24
```

The interval accepts 1–720 hours. Turning off automatic sync keeps manual refresh available.

## Public benchmark scores

**模型画像** defaults exclusively to **Artificial Analysis** for scores, radar charts,
coverage counts, the **仅有 AA 成绩** filter, and the comparison table. A model without a
matching AA result stays unscored, even when another benchmark exists.
The **评分依据** selector explicitly switches the whole view to BFCL, SuperCLUE, or
**调度评分（高级）**; reopening the page starts with AA again. Routing-score sorting is
available only in the routing-score view.

Public results retain source links, evaluation versions, dates, and inference settings.
The comparison table uses a single source/version with an optional configuration filter.
Missing measurements stay missing; index values are not converted to percentages.

The catalog updater also synchronizes these sources, with persistent independent caches:

- [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html): the official CSV and the leaderboard's
  version/update date; function calling, multi-turn, search, and memory scores. FC and Prompt
  results remain separate.
- [SuperCLUE](https://www.superclueai.com/): the original workbook used by the public website.
  The importer discovers the current evaluation month from the website's chart module,
  instead of pinning a workbook month. It reads cached spreadsheet values only. Website
  structure changes or invalid files produce a visible sync error and preserve the last
  successful results.
- [Artificial Analysis](https://artificialanalysis.ai/leaderboards/models): without a key,
  imports Intelligence Index scores from the public leaderboard's rendered table and
  seven capability measurements from the chart data included in the same public page:
  SciCode, Terminal-Bench 4.0, HLE, CritPt, AA-LCR, and AA-Omniscience accuracy/non-hallucination.
  Public JSON is decoded without executing JavaScript. Per-benchmark ratios become percentages
  by multiplying by 100; the composite Intelligence Index stays separate. Subscores must
  match the model page identifier, provider, inference setting, and rounded headline score.
  Radar axes stay fixed; missing axes show **暂无**, with no zero points or filled polygon.
  Models with only an index still show the chart frame with an explanation.
  The importer retains displayed headline precision, inference settings, model links, and source
  asterisk annotations. Missing scores (`--`) stay missing. The page does not identify an
  evaluation version/date, so these remain explicitly unspecified, not the retrieval date.
  No page scripts are executed. With an optional `SUPER_PARTNER_ARTIFICIAL_ANALYSIS_API_KEY`
  in `.env` (or exported `ARTIFICIAL_ANALYSIS_API_KEY`), restart the app to use the paginated
  [free-tier API](https://artificialanalysis.ai/data-api/docs), adding Coding and Agentic
  indices alongside the public per-benchmark measurements. A failed public page fetch does
  not discard successful API indices. Keys go only to the API, never to public-page requests,
  other sources or the browser.
  No model inference requests are made by score sync.

Matching requires the same provider and an exact identifier after formatting normalization,
or a unique matching display name. Model snapshot dates and tier/version digits are retained.
An evaluation published before a model's catalog release date is not attached to that model.
Each result retains its own reasoning/tool configuration; the app does not select the best
variant silently or average incompatible benchmarks. Models not covered by these sources
continue to show that no public result is available. A rolling API alias can change even
when its name stays the same, so the evaluation date/configuration still matters.

## What CLI agents may do

Both membership CLIs run shell commands only inside a sandbox, so tasks can run syntax
checks, tests and scripts without anyone approving each command:

- Codex runs with `--sandbox workspace-write`.
- Claude Code runs with its bubblewrap sandbox (`sandbox.enabled`,
  `autoAllowBashIfSandboxed`, and `allowUnsandboxedCommands: false`, so a command cannot ask
  to leave it) plus `--permission-mode acceptEdits` for file edits. On Linux this needs
  `bwrap` and `socat`.

Commands can write only to the working directory (the task's scratch folder or the project)
and `/tmp`, and have no network access, so installing packages or fetching URLs fails. The
planner is told these limits so acceptance criteria stay achievable.

### Allowing the network for one task

Tasks are offline by default. **允许联网** (on the new-task form, the plan confirmation and
resume bars, and the task's 详情 tab; `allow_network` on `POST /api/tasks`, or
`PUT /api/tasks/{id}/network` with `{"allow": true}`) lets that task's commands reach the
internet from its next model call on. A requirement change inherits the setting.

- API models' commands get their own network namespace from `pasta` (passt): the internet is
  reachable, but no host port is forwarded and the host's loopback is not mapped, so this
  application's API and other local services stay out of reach. A proxy in the server's
  `HTTP(S)_PROXY`/`ALL_PROXY` on a loopback address is the one port forwarded, and those
  variables are kept. Without `pasta` the sandbox shares the host's network instead, local
  services included.
- Claude Code's sandbox allows every domain except `localhost`/`127.0.0.1`/`::1`; Codex gets
  `sandbox_workspace_write.network_access=true`. Gemini and Qwen run unsandboxed anyway.
- Platform repair validation follows the same task's **允许联网** setting, read again before
  each validation round. Enabled checks use the same network isolation and proxy settings as
  task commands; disabled checks remain offline and explicitly skip tests that require a live
  network. Source files, real task data, and credentials retain their existing isolation.
- When a step fails because it must reach the internet while it runs, the diagnosis asks
  instead of guessing, offering **允许本任务联网**; picking it turns the setting on and the
  step is retried. Missing packages are still installed by the diagnosis into the shared tools
  folder, so a task does not need the network just to get its dependencies.

## API models act through tools

When a node runs on an API model, the model gets five tools and works in a loop until it
answers without calling one: `list_dir`, `read_file`, `write_file`, `edit_file` and
`run_command`. The loop uses each provider's own tool calling (OpenAI-compatible
`tools`/`tool_calls`, Anthropic `tool_use`/`tool_result`, Gemini `functionCall`/
`functionResponse`), so API models can change projects and run checks like the CLIs.

- File tools resolve every path inside the working directory (symlinks included) and
  refuse anything outside it.
- `run_command` runs under bubblewrap: the system is read-only, the home folder is replaced
  by an empty in-memory one with only the working directory mounted writable, `/tmp` is
  private, there is no network, and the environment is cleared, so API keys never reach a
  command. Without `bwrap` the tool is not offered.
- Local services such as an EDA license server can be let through port by port, while
  everything else stays offline. `SUPER_PARTNER_SANDBOX_HOST_PORTS` lists the host ports.
  Each one is served on a Unix socket mounted into the sandbox, and `socat` listens on the
  same port inside it. `SUPER_PARTNER_SANDBOX_ENV` names host variables copied in; listing
  `PATH` uses the host's PATH. For Synopsys tools, pin the vendor daemon's port in the license
  file (`VENDOR snpslmd port=27021`) and list it next to lmgrd's:

  ```
  SUPER_PARTNER_SANDBOX_HOST_PORTS=[26585,27021]
  SUPER_PARTNER_SANDBOX_ENV=["PATH","VCS_HOME","VERDI_HOME","SNPSLMD_LICENSE_FILE","VCS_ARCH_OVERRIDE"]
  ```

  Only API models' commands get these ports; the membership CLIs' sandboxes stay offline.
- Every received usage report is charged immediately, including rounds before a failure,
  model switch, cancellation, or the turn limit. Before each API request,
  execution, review, and diagnosis calls share reservations for a conservative input estimate
  and a bounded output token count. Calls wait for competing reservations to be released;
  insufficient remaining funds pause the task without consuming a quality retry. Final
  replies are checked too. Usage that a provider never reports cannot be reconstructed.
- **Turns.** A turn is one model request, which may hold several tool calls; the prompt says
  so and asks the model to batch independent calls and write each file whole. A node gets
  `api_agent_max_turns` (40) turns when easy, 1.5× when normal and 2× when hard. Five turns
  before the limit the model is told to wrap up; at the limit it gets one more request, without
  further tool calls, to answer with a progress summary (done, not done, what next). That
  summary goes through the usual review and reaches the next attempt as its previous result;
  with review off it counts as rejected. `read_file` returns up to 1200 lines per call.
- **Tool-call log.** Every tool call of an attempt is recorded on it (turn, tool, file or
  command, whether it went through) and shown, folded, under the attempt in the plan view;
  diagnoses get a per-tool count and the latest attempt's calls.
- **Failed twice.** When one model has failed a node twice in a row (rejected, failed
  verification, errors and running out of turns all count, the last even when the model claimed
  the step cannot be done; provider limits, cancellations and unverified reviews do not), the next retry goes to the strongest other eligible model, a
  membership CLI included, even under the API-first policy. Later retries do not go back to a
  model that failed the node twice. The rules are in `backend/super_partner/recovery.py`.
- Models marked as not supporting tools, and the offline demo model, only answer with text
  and are not routed to project nodes. `SUPER_PARTNER_API_AGENT=false` turns tools off.

Planning and review calls stay single requests without tools.

## How nodes hand off and check work

Ready nodes (all dependencies succeeded) start as soon as there is room, at most
`max_parallel_nodes` at a time, within the budget. Each node's prompt carries its
acceptance criteria, any findings it must fix, and, for every direct dependency, its result
— or, when longer than `handoff_max_chars` (4000), the reviewer's summary of it — plus the
files that dependency changed. The plan view records which form each node received.

**Acceptance review.** After a node finishes, another model checks the result against the
node's acceptance criteria and replies pass or fail with the reason and what to fix. The
reviewer is preferably not the model that did the work and follows the configured API-first
policy. Real tasks exclude the demo model from recovery and review. For project tasks the reviewer also sees the diff of the changed tracked
files. A reviewer that errors or answers unreadably is replaced by another available reviewer.
If none returns a valid verdict, the result stays unverified and follows bounded recovery;
it is never accepted as a pass. Set `SUPER_PARTNER_REVIEW_NODES=false` to skip reviews.

**Retries.** A node whose call fails or whose result is rejected runs again with the findings
in its prompt, on the strongest available model for its capability, up to
`max_node_attempts` (3) runs in total. Retries pass through the same budget check as any
node, so an exhausted budget pauses the task instead of overspending. Review calls are
charged like any other call.

**Verification and rework.** A planner node with `verifies` checks those upstream nodes and
ends with `VERDICT: PASS` or `VERDICT: FAIL: <problems>`. A failed verdict reopens the
verified nodes with the problems, plus every node that used their results (the verifier
included), up to `max_rework_rounds` (2) per task; after that the verifier fails. In-flight
consumers are cancelled and rerun after their new dependencies complete; generation checks
also reject late results. A missing verdict retries verification without marking it passed
or treating the missing verdict as evidence that the upstream work itself failed.

**Resuming.** A failed or cancelled task can continue from where it stopped
(`POST /api/tasks/{id}/resume`, optionally with a new budget). Finished nodes keep their
results; failed, cancelled and unfinished nodes run again with a fresh attempt count, the
last failure reason as feedback, and the rework rounds reset. A node whose model has since
been disabled or lost its key is routed again first.

**Pausing.** **暂停** on a running task (`POST /api/tasks/{id}/pause`) stops new steps from
starting; steps already running finish and keep their results. **按此预算继续** picks up
from there.

**Diagnosis.** **诊断** (`POST /api/tasks/{id}/diagnose`) asks a strong model why the task is
stuck: a membership CLI when one is available to the task (it can read the working files and
costs nothing extra), otherwise the best planning model. It gets the request, the plan, every
attempt with its reviewer's reasons, feedback, changed files, results and recent events, and
writes a report (问题定位 / 根本原因 / 处理结果与下一步) plus a `NEXT_STEP:` instruction for the failing
step. For a paused or failed step, this button uses the environment repair and model-switching
flow below, then automatically continues within the already approved budget. A running task
without a stopped step receives analysis only. Cancelling a task stops its manual diagnosis
as well; deletion waits for both execution and diagnosis to finish.

**Interactive diagnosis.** Every completed report, including older reports without choices,
has a reply form. Select a proposed option, write your own explanation, or submit both.
Replies are stored with the original report and become context for the next diagnosis;
the next diagnosis starts automatically, using the existing budget. Failed submissions keep
the draft and selection. Replies are accepted after the current diagnosis or activation finishes.
When a requirement or user preference really blocks progress, the model emits a Chinese
`QUESTION: ...`, an optional `OPTIONS: ["...", "..."]` JSON array, and `STATUS: NEEDS_INPUT`.
The task waits for an actual answer; no option is selected automatically. Routine authorized
repairs continue without an extra question. The reply endpoint is
`POST /api/tasks/{run_id}/diagnoses/{diagnosis_id}/reply` with `text` and/or `choice`.

**Repairing Super Partner itself.** Diagnosis distinguishes task mistakes, shared environment
problems, and defects in this application's scheduler, provider handling, persistence, API or
web UI. `STATUS: PLATFORM_REPAIR` automatically starts a separate source-editing phase.
It copies application source and tests into `<data_dir>/platform-repairs/<id>/source`,
preserving existing local edits as its baseline. The repair model works in that copy and must
add a regression check. The controller independently checks Python syntax, compatibility with
a temporary copy of existing database records, backend tests, frontend tests and a frontend
build. A failed check feeds back into a bounded repair attempt; it is not treated as success.

Every model that diagnoses or repairs the platform, CLI or API, follows one procedure:
[`skills/platform-repair/SKILL.md`](skills/platform-repair/SKILL.md), with the module map in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The repair prompt contains both; a diagnosis
gets their paths in the source checkout, marked as being about Super Partner's own code and not
the task's. They are copied into the repair source, where `AGENTS.md` (read by Codex) and
`CLAUDE.md` (read by Claude Code) point to them. A diagnosis names the places to change in
`LOCATION: path:line` lines, and the repair starts from them instead of rereading the code.

For an editable source checkout started with `start.sh` under a systemd user manager, passing
repairs activate automatically. New work waits while in-flight steps and requests finish;
an independent worker checks for concurrent source edits, saves a database backup, applies
the patch, restarts this checkout's API service, and checks the new process's health. Failed
activation rolls source files back. Durable task markers allow only tasks held by this repair
to continue after successful activation, within their original budgets; cancelled tasks,
explicit user pauses and unanswered diagnosis questions stay stopped. Source changes, check
results and activation outcomes are recorded in shared recovery history for all projects.
The configuration switches are `SUPER_PARTNER_PLATFORM_SELF_REPAIR` (default `true`) and
the optional `SUPER_PARTNER_PLATFORM_ROOT` for the source checkout. Other installation or
service-manager layouts must provide a supported editable checkout before automatic activation
is available. No model assertion alone marks a platform repair as active.

Stored model selections are checked again at confirmation and before execution. Disabled
models and models that lost their credentials are replaced under current routing rules.

**Environment repair.** After every failed attempt of a step (and when a verification runs
out of rework rounds), the step waits while the same strong model looks for what blocks it
outside the task itself: missing software, sandbox or machine limits, configuration, licenses,
unreachable services. It gets the plan, every attempt and recent events, and is told how the
step's commands are restricted. Its own commands run on the host as the user with network
access, but under bubblewrap, whose `no_new_privs` blocks sudo and other setuid programs
(Claude Code runs with its sandbox off and `bypassPermissions`, Codex with
`--dangerously-bypass-approvals-and-sandbox`; API models get a host-mode `run_command` without
API keys in its environment). It does not do the step's work.

Repairs go in a shared tools folder, `<data_dir>/tools`, so every step sees them: `bin/` is
first on PATH, `python/` (for `pip install --target`) is on PYTHONPATH, and `sandbox.json`
records environment variables and local ports (`{"env": {...}, "host_ports": [...]}`) that
sandboxed commands get. API models' sandbox mounts the folder read-only; CLIs get the same
PATH, PYTHONPATH and variables. Anything that needs root becomes exact commands in the report.

Its answer ends with one status line:

- `STATUS: FIXED` with what it changed (`## 修复内容`): the step runs again on its own model with
  that summary as feedback, and the failure does not count against its attempts (twice per
  step; after that repairs count like any retry).
- `STATUS: RETRY` with a `NEXT_STEP:` instruction: nothing outside the task is wrong, so the
  usual retry follows (on the strongest model) with the advice added to its feedback. Once a
  step has failed twice in a row, the diagnosis is shown those failures and told that another
  RETRY must change the approach materially.
- `STATUS: UNABLE` with `USER_ACTION:`: the step's goal cannot be reached in this environment
  and the diagnosis cannot change that (no root to install a simulator, no licence, no
  credentials). The step ends as 无法完成 with the report instead of being retried, and the
  rest of the task goes on.
- `STATUS: QUOTA_EXHAUSTED`: the failed step's execution model has run out of usage or credits.
  The scheduler excludes that model and hands the original step to an eligible backup without
  counting a failed attempt. This also handles provider messages not yet recognized by the
  error detector. The diagnosis stays in the history, linked to the model-switch event.
- `STATUS: MODEL_UNAVAILABLE`: the failed step's provider is overloaded, unreachable or
  timing out. The scheduler temporarily excludes it and switches the execution model.
- `STATUS: BLOCKED` with `USER_ACTION: ROOT|CREDENTIALS|BUDGET|DECISION: <specific need>`:
  a concrete human-only dependency requires input, so the task pauses. A bare BLOCKED
  suggestion is treated as RETRY; the diagnosis must perform available repairs instead of
  handing back instructions it could execute itself.

Shared environment repairs run serially across projects. Changes under `<data_dir>/tools`
reported as FIXED are recorded with their diagnosis, affected files and verification report;
later diagnoses receive that history and check existing fixes before repeating installations.
Project-specific settings must stay local. `GET /api/recovery` exposes these records, active
model cooldowns and learned error signatures for inspection.

When a step runs out of attempts, or the diagnosis could not run, the task also pauses with
the report instead of failing. **按此预算继续** then runs the failed step again with fresh
attempts and the advice. `SUPER_PARTNER_AUTO_DIAGNOSE=false` turns diagnoses off: failures
retry as before, and a step that runs out of attempts fails the task. Resuming a failed task
also hands the latest advice to the failed step. The task page shows the report and earlier
ones.

**Running out of quota.** A model that stops because of its quota (a membership CLI at its
usage/session/weekly/daily/monthly limit, an API answering 429, 402 or an insufficient balance)
is marked exhausted until the reset time its message gives (including a reported timezone),
or for an hour (five minutes for a plain rate limit). It
leaves `configured_models()`, so every choice skips it: a step switches to a backup chosen by
the usual routing rules (the strongest one if it was already retrying) without counting a
failed attempt, and so does a later step routed to it at planning time; reviewers, planners
and diagnosers move to the next one. With no backup left the task pauses with
`quota_exhausted` and continues from **按此预算继续**. The console lists exhausted models with
their reset time and a **恢复使用** button (`GET`/`DELETE /api/models/quota`); saving a
provider's key clears its marks. Upstream 5xx/network failures use the same switching path
after request retries, with a five-minute cooldown and a separate service-failure label.
Cooldowns are stored in the application database and survive restarts. A diagnosis that
recognizes an unfamiliar quota/service error also saves a model-scoped error signature;
other projects immediately reuse that classification on recurrence without another
diagnosis. Clearing a cooldown preserves the learned rule. Routing eligibility and budget
limits still apply to every replacement.

Each node in the plan view shows its check result, retries, the upstream results it
received, the files it changed, and every attempt with its model, reviewer, cost and reason.

## Task results

**未能完成 (incomplete)** is the honest end of a task whose goal cannot be reached here, instead
of a success that only prepared it (deployment scripts written, but nothing deployed without a
network). Every step is told to do what it can and end with `UNABLE: <what, why, what would
make it possible>` rather than simulate the outcome, and reviewers are told to judge the effect
the task asks for, not the preparation, and to name such a limit in an `unable` field. Either
way the attempt is `unable`: the per-failure diagnosis tries to lift the limit (installing
software, or asking to allow the network), retries a wrong claim, and otherwise the step ends
as **无法完成** with its reason, the user action the diagnosis named, and its report. Steps that
need its result are skipped (`blocked`), the rest of the task carries on, and the task ends as
`incomplete`, its result opening with what could not be done and why, followed by what was
done. "重做未完成的步骤" (resume) runs those steps again once the limit is lifted; a requirement
change works on it like on any finished task.

**需求变更** on the result page of a finished, failed or cancelled task adds to it instead of
starting over (`POST /api/tasks/{id}/followup` with the new requirement and a budget). The new
task links back to it (`parent_run_id`), works in the same project or the same scratch folder
(`workspace_id`), and is planned knowing the original request, plan, an excerpt of the result
and the files that exist; each of its steps is told to build on them and keep what the change
does not touch, and only goes to models that can edit files. Like any task it waits for
confirmation before it runs. A change of a change keeps the first folder and the earlier
background.

**查看完整结果** on a finished task opens a result page with the final result, the output
files (viewable in place), and every step's output. For a task without a project the output
is its whole scratch folder minus agent folders (`.review`, `.claude`, …); for a project
task it is only the files its steps changed.

- **另存为 ZIP** downloads the output files plus `RESULT.md` (request, final result and each
  step's output) from `GET /api/tasks/{id}/archive`.
- **上传到 GitHub** commits the same files as one commit on `main` (`POST /api/tasks/{id}/github`):
  either into a new repository (private by default) created with a GitHub token saved in the
  OS keyring (`PUT /api/github/token`), or into an existing empty repository pushed with the
  machine's own git/SSH setup. The token reaches git through the environment, never a command
  line or URL. Project tasks are not published from here, since their changes live in the
  user's own repository.

## Attachments

Images and files can be attached to a task with **📎 添加附件**, by dropping them on the task
form, or by pasting a screenshot into the description. Each file is uploaded to the local API
(`POST /api/attachments?name=…`, raw body, at most 20 MB and 10 files per task) and stored
under `<data_dir>/attachments/`; upload content is separate from internal metadata, so names
such as `meta.json` are preserved. Older upload paths remain readable. Deleting the task
deletes its files. The type is detected
from the content: PNG, JPEG, GIF and WebP are images, UTF-8 files are text, everything else
is a binary file.

Every node of the task receives the attachments. Membership CLIs get the file paths; Codex
also receives images with `--image`, and Claude Code gets the folders with `--add-dir`. API
models receive images as image inputs and text files inline; binary files are only named.
Tasks with images route only to CLIs and to API models not marked as lacking vision.

## Routing scores

The seven routing scores come only from Artificial Analysis; there are no hand-written
presets. Each AA metric is converted by rank: a model's percentile among every AA-scored
model is placed linearly in a fixed span per capability (for example 0.74–0.97 for coding),
so planner thresholds keep their meaning. Coding uses the AA coding index, SciCode and
Terminal-Bench; planning the intelligence index and HLE; research knowledge accuracy, AA-LCR
and HLE; tool use the agentic index and Terminal-Bench; reliability the non-hallucination
rate. AA has no Chinese-language or structured-output evaluation, so those follow the
intelligence index. When several AA rows match a model, the strongest is used.

Membership CLIs run the account's default model, so each inherits the scores of the
strongest AA-scored API model from the same vendor. Models without AA results (and CLIs whose
vendor has none) have no scores: they are not routed, are left out of the model list and the
model page, and cannot serve as a CLI's API fallback. The offline demo model keeps fixed
scores for tests and is not listed either. If no
AA results are cached yet, the last derived scores are kept. BFCL and SuperCLUE results are
shown for reference only and do not affect routing.

The planner rates each task **easy**, **normal** or **hard**, which sets the minimum score
for the task's capability (70, 80 or 90). Among the models that clear it (API models only, unless none qualifies), easy and normal
tasks take the cheapest and hard tasks the one with the highest score for the capability,
with cost only breaking ties. When no available model reaches a threshold, it is
lowered to the best available score and the plan notes the change. The plan view shows each
task's difficulty and threshold, and the model chip explains why that model was chosen.

## Main API

- `GET /api/projects/discover` lists git projects under the allowed roots; `GET /api/projects/inspect?path=` validates a folder and reports its git state; `GET`/`POST /api/projects/native-picker?start=` reports and opens the desktop folder dialog.
- `POST /api/tasks` creates a plan and estimate; pass `project_dir` to work on an existing project.
- `POST /api/tasks/{id}/confirm` confirms the budget and starts execution.
- `GET /api/tasks/{id}` returns the full graph and results.
- `DELETE /api/tasks/{id}` deletes a succeeded, failed, or cancelled mission and its event
  history. The history list and mission detail offer **删除记录** with an in-page confirmation
  dialog showing the task name, progress, and any failure. Project
  files and workspace files are retained. Unfinished tasks return 409; missing records return 404.
- `GET /api/tasks/{id}/events` streams audit events as SSE.
- `POST /api/tasks/{id}/cancel` cancels an active task. **Cancel mission** opens an in-page
  confirmation showing the task name. After **确认取消**, the page shows progress or any
  error and updates the task status immediately on success. Results, logs, and project
  files are retained.
- `GET /api/models` lists capabilities, prices, credential readiness, and matched `benchmarks`.
- `GET /api/models/sync` shows the persisted per-provider update status and next check.
- `POST /api/models/sync` starts a background refresh (202); repeated requests share the active job.
- `PATCH /api/models` globally enables or disables a model using `model_id` and `enabled` JSON fields.
- `GET /api/channels/cli?refresh=true` re-detects installed and authenticated membership CLIs.
- `GET /api/providers` lists API connection states without returning secrets.
- `PUT` or `DELETE /api/providers/{provider}/key` manages an OS-keyring credential.
- `POST /api/providers/{provider}/test` verifies credentials against the provider model-list API.
