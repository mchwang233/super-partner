# Super Partner

Local-first multi-model orchestration for software and research tasks. It plans work as a dependency graph, routes each node to the cheapest capable configured model, estimates cost, asks for one confirmation, then executes autonomously with an audit trail and budget circuit breaker.

Task planning uses the configured model with the strongest planning and reliability scores to generate a validated JSON dependency graph. Invalid, unavailable, or cyclic model plans fall back to the deterministic planner. Each resulting node is independently assigned to the lowest-cost enabled model that meets its capability and context requirements, while authenticated membership CLIs retain priority.

Authenticated membership CLIs are preferred over metered APIs. At startup the app safely detects Codex CLI, Claude Code, Gemini CLI, and Qwen Code without exposing account details. A CLI failure falls back to the matching API only when its API key is configured.

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

## Provider credentials

Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `ARK_API_KEY`, `ZHIPU_API_KEY`, `MINIMAX_API_KEY`, or `MOONSHOT_API_KEY`. The API also accepts a provider key at `PUT /api/providers/{provider}/key` and stores it through the operating-system keyring. Secrets are never returned by an API response.

Seed prices for models marked `pricing_verified: false` are placeholders and must be reviewed before cost-sensitive production use. The local demo model remains the safe default when no provider keys exist.

## Main API

- `GET /api/projects/browse` lists selectable folders; `GET`/`POST /api/projects/native-picker` reports and opens the desktop folder dialog.
- `POST /api/tasks` creates a plan and estimate; pass `project_dir` to work on an existing project.
- `POST /api/tasks/{id}/confirm` confirms the budget and starts execution.
- `GET /api/tasks/{id}` returns the full graph and results.
- `GET /api/tasks/{id}/events` streams audit events as SSE.
- `POST /api/tasks/{id}/cancel` cancels an active task.
- `GET /api/models` lists capabilities, prices, and credential readiness.
- `PATCH /api/models` globally enables or disables a model using `model_id` and `enabled` JSON fields.
- `GET /api/channels/cli?refresh=true` re-detects installed and authenticated membership CLIs.
- `GET /api/providers` lists API connection states without returning secrets.
- `PUT` or `DELETE /api/providers/{provider}/key` manages an OS-keyring credential.
- `POST /api/providers/{provider}/test` verifies credentials against the provider model-list API.
