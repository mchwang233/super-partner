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
  _decide ── ask_lead ── _apply_decision                                (the task's lead decides, leading.py)
     ├─ retry / switch_model / accept / unable / ask_user / rework / replan (replan.py)
     └─ repair_environment / repair_platform ── _repair (diagnosis with the lead's brief)
  _repair ── _diagnose_with ── _repair_done                             (fallback: no lead decides)
     └─ STATUS: PLATFORM_REPAIR ── _fix_platform ── platform_repair     (fix Super Partner itself)
  all steps done ── _final_review (the lead: done / rework / replan) ── succeeded
  run ends: succeeded / failed / incomplete (_end_incomplete) / paused
```

## Who owns what

| Area | Module | Key names |
|---|---|---|
| HTTP API, task lifecycle endpoints | `api.py` | `create_task`, `follow_up_task`, `resume_task`, `reopen_node`, `reply_to_diagnosis`, `set_network_access` |
| Records and statuses | `models.py` | `RunStatus`, `NodeStatus`, `Outcome` (attempt outcomes), `DiagnosisStatus`, `FINISHED`, `TaskNode`, `NodeAttempt`, `Diagnosis`, `ExecutionRun` |
| Storage (SQLite, JSON bodies) | `db.py` | `Database.save_run`, `get_run`, events |
| Planning | `planner.py` | `autonomous_plan` (by the lead when one is available: `_plan_as_lead`), `_validate_plan`; project work as small packages (`TaskNode.files/reads/provides/check`, `package`, `scope_problems` sends broad steps back to the lead) |
| The task's lead: one conversation from planning to the end (docs/design/supervisor.md) | `supervisor.py` | `candidates`, `ask`, `handover`, `trim`; `Executor.ask_lead`, `ProviderRegistry.converse`, `CliChannelRegistry.converse`, `ExecutionRun.supervisor` |
| Model choice | `routing.py` | `route_node` (first pick), `strongest_for` (retry), `stronger_than` (escalation), `pick_reviewer`, `diagnosers` |
| Scheduling, attempts, step outcomes | `executor.py` | `Executor`; see "Executor" below |
| Reviewing a result | `review.py` (mixed into `Executor`) | `_check`, `_reviewer`, `MAX_BACKUPS` |
| The lead's decisions after a failed attempt, and its questions to the user | `leading.py` (mixed into `Executor`) | `_decide`, `_apply_decision`, `_decisions_left`, `start_lead_answer`, `start_lead_chat`; prompts in `supervisor.failure_message`, `parse_decision` |
| Diagnosing failures and acting on them (when no lead decides; repairs the lead asks for) | `diagnosing.py` (mixed into `Executor`) | `_repair`, `_diagnose_with`, `_repair_done`, `begin_diagnosis`, `_run_diagnosis` |
| Repairing Super Partner itself (flow) | `self_repair.py` (mixed into `Executor`) | `_fix_platform`, `_activate_platform`, `_hold_for_platform`, `restore_platform` |
| Changing a running task's plan at the lead's decision | `replan.py` | `change_plan`, `PlanError` |
| What happens after a failure without a lead (fallback: retry model, meaning of a diagnosis) | `recovery.py` | `retry_model`, `failed_models`, `after_diagnosis`, `turns_for`, `MODEL_FAILURES` |
| Prompts for steps and reviews, verdict parsing | `quality.py` | `node_prompt`, `continue_prompt`, `attempt_notes`, `review_prompt`, `parse_review`, `UNABLE_RULE`, `unable_claim`, `retry_feedback` |
| Diagnosis prompts and parsing | `diagnosis.py` | `prompt`, `repair_prompt`, `platform_instructions`, `parse`, `STATUS` |
| API model calls, output caps, tool loop | `providers.py` | `ProviderRegistry.complete`, `_send`, `output_ceiling`, `_loop`, `Conversation` (a step's next attempt continues it; chosen by `Executor._conversation`) |
| CLI model calls and their sandboxes | `cli_channels.py` | `CliChannelRegistry.complete`, `_with_network`, `repair_command` |
| Project brief for planning (file summaries by the cheapest API model, refreshed for changed files only), and the files a step is given: a package's own files in full, otherwise the file list and the files it names | `briefing.py` | `planning_brief`, `refresh`, `render`, `attached_files`, `likely_files`; `Executor._with_files`, `quality.scope_section`, `routing.brief_writer` |
| A work package far past its expected turns goes to the lead (continue, or stop and then split) | `progress.py`, `review.py` | `expected_turns`, `OVERRUN_FACTOR`; `_overrun_check`, `supervisor.overrun_message` |
| Steps in one project at once: disjoint package files run together, unscoped steps alone | `scopes.py` | `FolderLock.hold`, `Scope.clashes`, `Scope.own`; `Executor._work` |
| Tools and sandbox for API models | `agent.py` | `Workspace`, `project_map` (file list given at the start of an attempt), `sandbox_args`, `network_prefix` |
| Checking long attempts instead of turn limits and timeouts | `progress.py` | `check_prompt`, `parse`, `api_evidence`, `cli_evidence`, `Stopped`; the check itself is `Executor._progress_watch` (review.py) |
| Per-request budget reservations | `budget.py` | `RequestBudget.reserve` |
| Quota / outage detection and cooldown | `quota.py` | `QuotaTracker.classify`, `mark`, `exhausted` |
| Shared machine fixes (tools dir, env) | `environment.py` | `tools_dir`, `command_env` |
| Self-repair: isolated copy, checks, activation | `platform_repair.py` | `PlatformRepairs.prepare`, `check`, `activate`, `SOURCE_DIRS` |
| Model catalog and benchmark scores | `catalog_sync.py`, `benchmarks.py`, `scoring.py` | `public_models`, `match_results`, `rescore` |
| Results listing, ZIP, GitHub publish | `publish.py` | `SKIP`, `result_markdown` |
| Settings (env `SUPER_PARTNER_*`) | `config.py` | `settings` |

## Executor

`Executor` (`executor.py`) mixes in `Reviewing` (`review.py`), `Diagnosing` (`diagnosing.py`) and
`SelfRepair` (`self_repair.py`); they share its state (`db`, `locks`, `_complete`, `_save`, `emit`).

- `execute` / `_dispatch`: the run loop; starts ready nodes up to `max_parallel_nodes`.
- `_execute_node`: one attempt; exceptions map to outcomes (`budget`, `error`, quota → `_switch_model`).
- `_check` (review.py): review or verification verdict; `quality.Verdict(passed, reason, fix, unable, ...)`.
- `_finish`: appends the `NodeAttempt` with an `Outcome`.
- After a failure (when `auto_diagnose`): the lead decides (`_decide`, leading.py): retry, switch
  model, accept, unable, ask the user, or a repair, which runs `_repair` with its brief. Without a
  lead able to decide: `_repair` then `_repair_done` (diagnosing.py), which carries out
  `recovery.after_diagnosis(outcome, status)`: hold for a platform repair, ask the user, switch
  model, retry after an environment fix, mark unable (`_mark_unable`), pause, or retry.
- `_retry_or_fail`: counts the failure and fails the node or retries it on
  `recovery.retry_model` (a model that failed the node twice in a row gives way).
- A failed verification goes to the lead too (`supervisor.verification_message`): rework chosen
  steps with what to change (`_send_back(..., limited=False)`), accept, or rerun the verification.
  Without a lead, `_send_back` reopens all verified steps, capped by max_rework_rounds.
- `_fix_platform` → `platform_repair.check` → `_activate_platform` (self_repair.py): self-repair,
  applied by restart.

## Tests

| Area | Tests |
|---|---|
| Executor flow, retries | `tests/test_executor.py`, `test_recovery.py`, `test_recovery_regressions.py`, `test_turns.py`, `test_resume.py` |
| Diagnosis | `test_diagnosis.py`, `test_diagnosis_interaction.py` |
| Unable steps | `test_unable.py` |
| Network, sandbox, tools | `test_network.py`, `test_agent.py`, `test_safety.py` |
| Providers, output caps | `test_providers.py`, `test_quota.py` |
| Progress checks | `test_progress.py` |
| The lead's conversation | `test_supervisor.py` |
| The lead's decisions | `test_leading.py`, `test_replan.py` (plan changes, final review) |
| Review prompts and evidence | `test_quality.py`, `test_review_evidence.py` |
| Platform self-repair | `test_platform_repair.py` |
| Catalog and benchmarks | `test_catalog_sync.py`, `test_benchmarks.py`, `test_scoring.py` |
| API-level flows | `test_core.py`, `test_followup.py`, `test_task_deletion.py`, `test_publish.py` |
| Web UI | `frontend/tests/mission-actions.test.mjs` |

Commands: `python -m pytest -q tests/test_x.py` (one area), `python -m pytest -q` (all backend,
about 35 s), `cd frontend && npm test` and `npm run build` (only when the frontend changed).

## Frontend (`frontend/`)

`src.tsx` (app shell, task creation, actions), `lead.tsx` (the lead's decisions, questions and messages), `result-page.tsx` (results, requirement change),
`plan-graph.tsx` (steps and attempts), `diagnosis.tsx` (diagnosis chat), `mission-history.tsx`
(task list), `model-profiles.tsx` (models), `style.css` (tokens and styles).
