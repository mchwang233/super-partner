# Super Partner architecture map

A short map for finding code fast. Read this first, then open only the lines you need.
Keep it current: a change that moves a responsibility updates this file in the same patch.

## Life of a task

```
api.create_task ── planner.autonomous_plan ── routing.route_node        (plan, pick a model per step)
api.confirm_task ── Executor.execute ── _dispatch                       (run ready steps in parallel)
  _execute_node ── _work ── _complete ── providers.complete             (one attempt of one step)
                                          ├─ cli_channels  (membership CLIs: claude, codex, ...)
                                          └─ agent.Workspace (API models' tools, bwrap sandbox)
  _check   (verification steps: VERDICT line; others: a reviewer model, quality.review_prompt)
  _finish  (record the NodeAttempt, then: passed / send back / diagnose / retry / unable)
  _repair ── _diagnose_with ── _repair_done                             (diagnosis after a failure)
     └─ STATUS: PLATFORM_REPAIR ── _fix_platform ── platform_repair     (fix Super Partner itself)
  run ends: succeeded / failed / incomplete (_end_incomplete) / paused
```

## Who owns what

| Area | Module | Key names |
|---|---|---|
| HTTP API, task lifecycle endpoints | `api.py` | `create_task`, `follow_up_task`, `resume_task`, `reopen_node`, `reply_to_diagnosis`, `set_network_access` |
| Records and statuses | `models.py` | `RunStatus`, `NodeStatus`, `FINISHED`, `TaskNode`, `NodeAttempt`, `Diagnosis`, `ExecutionRun` |
| Storage (SQLite, JSON bodies) | `db.py` | `Database.save_run`, `get_run`, events |
| Planning | `planner.py` | `autonomous_plan`, `_validate_plan` |
| Model choice | `routing.py` | `route_node` (first pick), `strongest_for` (retry), `stronger_than` (escalation), `pick_reviewer`, `diagnosers` |
| Scheduling, attempts, diagnosis flow, platform repair flow | `executor.py` | see "Executor" below |
| What happens after a failure (retry model, meaning of a diagnosis) | `recovery.py` | `retry_model`, `failed_models`, `after_diagnosis`, `turns_for`, `MODEL_FAILURES` |
| Prompts for steps and reviews, verdict parsing | `quality.py` | `node_prompt`, `review_prompt`, `parse_review`, `UNABLE_RULE`, `unable_claim`, `retry_feedback` |
| Diagnosis prompts and parsing | `diagnosis.py` | `prompt`, `repair_prompt`, `platform_instructions`, `parse`, `STATUS` |
| API model calls, output caps, tool loop | `providers.py` | `ProviderRegistry.complete`, `_send`, `output_ceiling`, `_loop` |
| CLI model calls and their sandboxes | `cli_channels.py` | `CliChannelRegistry.complete`, `_with_network`, `repair_command` |
| Tools and sandbox for API models | `agent.py` | `Workspace`, `sandbox_args`, `network_prefix` |
| Per-request budget reservations | `budget.py` | `RequestBudget.reserve` |
| Quota / outage detection and cooldown | `quota.py` | `QuotaTracker.classify`, `mark`, `exhausted` |
| Shared machine fixes (tools dir, env) | `environment.py` | `tools_dir`, `command_env` |
| Self-repair: isolated copy, checks, activation | `platform_repair.py` | `PlatformRepairs.prepare`, `check`, `activate`, `SOURCE_DIRS` |
| Model catalog and benchmark scores | `catalog_sync.py`, `benchmarks.py`, `scoring.py` | `public_models`, `match_results`, `rescore` |
| Results listing, ZIP, GitHub publish | `publish.py` | `SKIP`, `result_markdown` |
| Settings (env `SUPER_PARTNER_*`) | `config.py` | `settings` |

## Executor (`executor.py`)

- `execute` / `_dispatch`: the run loop; starts ready nodes up to `max_parallel_nodes`.
- `_execute_node`: one attempt; exceptions map to outcomes (`budget`, `error`, quota → `_switch_model`).
- `_check`: review or verification verdict; `quality.Verdict(passed, reason, fix, unable, ...)`.
- `_finish`: appends the `NodeAttempt`; outcome is one of `passed`, `rejected`, `verify_failed`,
  `unverified`, `unable`, `error`, `budget`, `superseded`, `cancelled`, `quota`, `unavailable`.
- After a failure: `_repair` (when `auto_diagnose`) then `_repair_done`, which carries out
  `recovery.after_diagnosis(outcome, status)`: hold for a platform repair, ask the user, switch
  model, retry after an environment fix, mark unable (`_mark_unable`), pause, or retry.
- `_retry_or_fail`: counts the failure and fails the node or retries it on
  `recovery.retry_model` (a model that failed the node twice in a row gives way).
- `_send_back`: a failed verification reopens the verified steps (rework rounds).
- `_fix_platform` → `platform_repair.check` → `_activate_platform`: self-repair, applied by restart.

## Tests

| Area | Tests |
|---|---|
| Executor flow, retries | `tests/test_executor.py`, `test_recovery.py`, `test_recovery_regressions.py`, `test_turns.py`, `test_resume.py` |
| Diagnosis | `test_diagnosis.py`, `test_diagnosis_interaction.py` |
| Unable steps | `test_unable.py` |
| Network, sandbox, tools | `test_network.py`, `test_agent.py`, `test_safety.py` |
| Providers, output caps | `test_providers.py`, `test_quota.py` |
| Review prompts and evidence | `test_quality.py`, `test_review_evidence.py` |
| Platform self-repair | `test_platform_repair.py` |
| Catalog and benchmarks | `test_catalog_sync.py`, `test_benchmarks.py`, `test_scoring.py` |
| API-level flows | `test_core.py`, `test_followup.py`, `test_task_deletion.py`, `test_publish.py` |
| Web UI | `frontend/tests/mission-actions.test.mjs` |

Commands: `python -m pytest -q tests/test_x.py` (one area), `python -m pytest -q` (all backend,
about 35 s), `cd frontend && npm test` and `npm run build` (only when the frontend changed).

## Frontend (`frontend/`)

`src.tsx` (app shell, task creation, actions), `result-page.tsx` (results, requirement change),
`plan-graph.tsx` (steps and attempts), `diagnosis.tsx` (diagnosis chat), `mission-history.tsx`
(task list), `model-profiles.tsx` (models), `style.css` (tokens and styles).
