# Super Partner

Local-first multi-model orchestration for software and research tasks. It plans work as a dependency graph, routes each node to the cheapest capable configured model, estimates cost, asks for one confirmation, then executes autonomously with an audit trail and budget circuit breaker.

Task planning uses the configured model with the strongest planning and reliability scores to generate a validated JSON dependency graph. Invalid, unavailable, or cyclic model plans fall back to the deterministic planner. Each resulting node is independently assigned to the lowest-cost enabled model that meets its capability and context requirements, while authenticated membership CLIs retain priority.

Authenticated membership CLIs are preferred over metered APIs. At startup the app safely detects Codex CLI, Claude Code, Gemini CLI, and Qwen Code without exposing account details. A CLI failure falls back to the matching API only when its API key is configured, that API model is enabled, and its estimated cost fits the remaining budget.

## Run locally

### One-command startup

```bash
./start.sh
```

The first run installs missing backend and frontend dependencies. Open <http://127.0.0.1:5173>, then stop everything with `./stop.sh`. Runtime logs are stored under `.super-partner/logs/`; use `./scripts/service.sh status` or `restart` for service management.

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

In the web console, use the project directory field under the prompt. **Browse…** opens a folder
picker served by the API, and **System dialog…** opens the desktop's own folder dialog on the
machine running the API (needs `DISPLAY`/`WAYLAND_DISPLAY` and `zenity`; the button is hidden
otherwise, and the dialog appears on that machine's desktop, not on a remote browser's).

- The directory must be an existing absolute path inside `SUPER_PARTNER_PROJECT_ROOTS` (a JSON list, default: your home directory), and cannot be the Super Partner data directory.
- The planner sees the project's top-level layout and git state, and marks nodes that must read or change project files. Only those nodes run in the project; everything else runs in a scratch workspace.
- Project nodes route only to membership CLIs, because API models cannot see local files, and they never fall back to an API. Nodes editing the same project run one at a time, across runs as well.
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

Startup waits up to three seconds for saved keyring credentials. If the keyring is locked or
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
New entries start disabled and cannot join routing until the required metadata and routing
preset scores exist. Retired models and models absent from a successful official list are
excluded from new routing. Existing preset scores are not vendor benchmarks.

Optional `.env` settings:

```dotenv
SUPER_PARTNER_CATALOG_AUTO_SYNC=true
SUPER_PARTNER_CATALOG_SYNC_INTERVAL_HOURS=24
```

The interval accepts 1–720 hours. Turning off automatic sync keeps manual refresh available.

## Public benchmark scores

**模型画像** defaults exclusively to **Artificial Analysis** for scores, radar charts,
coverage counts, the **仅有 AA 成绩** filter, and the comparison table. A model without a
matching AA result stays unscored, even when another benchmark or a routing preset exists.
The **评分依据** selector explicitly switches the whole view to BFCL, SuperCLUE, or
**项目调度预设（高级）**; reopening the page starts with AA again. Preset score sorting is
available only in the routing-preset view.

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

Public scores are displayed separately from the seven existing routing presets. Importing
scores does not enable models, change task thresholds, or equate tool-call accuracy, Chinese
benchmark performance, or hallucination control with this app's task success rate. The app
does not yet calibrate public benchmarks into routing scores or run its own evaluation suite.

## Main API

- `GET /api/projects/browse` lists selectable folders; `GET`/`POST /api/projects/native-picker` reports and opens the desktop folder dialog.
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
