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
super-partner run TASK_ID
```

## Provider credentials

Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `ARK_API_KEY`, `ZHIPU_API_KEY`, `MINIMAX_API_KEY`, or `MOONSHOT_API_KEY`. The API also accepts a provider key at `PUT /api/providers/{provider}/key` and stores it through the operating-system keyring. Secrets are never returned by an API response.

Seed prices for models marked `pricing_verified: false` are placeholders and must be reviewed before cost-sensitive production use. The local demo model remains the safe default when no provider keys exist.

## Main API

- `POST /api/tasks` creates a plan and estimate.
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
