# Super Partner architecture map

A short map for finding code fast. Read this first, then open only the lines you need.
Keep it current: a change that moves a responsibility updates this file in the same patch.

## Life of a task

```
api.create_task ── planner.autonomous_plan ── team.form (when employee_teams; before task planning)
                                         ├─ task_planning.plan_tasks (when direct_task_planning:
                                         │      executable tasks, employees, dependencies, no part refinement)
                                         └─ routing.route_node        (plan, pick a model per step;
                                                                         with a lead: major parts only, outline.py)
api.confirm_task ── Executor.execute ── _preflight                     (check sandbox, tools, internet, models first:
                                         │                                preflight.py; repair once, else pause)
                                         └─ _dispatch                    (run ready steps in parallel;
                                          └─ _split_parts               split every major part at once, refining.py)
  _execute_node ── _work ── _complete ── providers.complete             (one attempt of one step)
                                          ├─ cli_channels  (membership CLIs: claude, codex, ...)
                                          └─ agent.Workspace (API models' tools, bwrap sandbox)
  _check   (verification steps: VERDICT line; others: a reviewer model, quality.review_prompt)
  _finish  (record the NodeAttempt, then: passed / send back / diagnose / retry / unable)
  _decide ── ask_lead ── _apply_decision                                (the task's lead decides, leading.py)
     ├─ retry / switch_model / accept / unable / ask_user / rework / replan (replan.py)
     ├─ repair_environment / repair_platform ── _repair (diagnosis with the lead's brief)
     └─ request_component ── _engineer_for_lead (task waits; the platform engineer's job, engineer.py)
  _repair ── _diagnose_with ── _repair_done                             (fallback: no lead decides)
     └─ STATUS: PLATFORM_REPAIR ── _fix_platform ── platform_repair     (fix Super Partner itself)
  all steps done ── _final_review (the lead: done / rework / replan) ── succeeded
  run ends: succeeded / failed / incomplete (_end_incomplete) / paused
  naturally finished run ── retrospect.record ── Learning.finish_learning (when enabled; excludes manual cancellation)
    └─ _learn: conclusion + improvement items ── independent validation ── active skill/rule
       └─ platform item ── engineer (copy, tests, activation; automatic when configured, even after source-task cancellation)
    next task: selected / attach ── planning and step instructions ── observe (before/after)
```

Retrospective analysis and lesson validation use `learning.strongest_model`: highest overall
capability, then planning and reliability; a different validator only among equally strong models.
Calls use separate empty conversations and diagnosis reasoning effort, with the existing budget,
availability, context and task CLI-permission checks.

Initial lead planning (`planner._plan_as_lead`) retains a validated employee roster when an
outline fails and another lead takes over. Optional `team_reasoning_effort` and
`planning_reasoning_effort` apply only during their respective calls; the persistent lead's
policy is restored afterwards. `planning_call_timeout` bounds each such call, including API
queueing/retries and CLI execution, and finishes cancellation before trying another lead.
Empty effort settings and a zero deadline preserve the legacy call policy. These controls do
not alter refinement or retrospective model/effort selection.

With `direct_task_planning`, `task_planning.py` asks for complete work items once and reuses
the ordinary DAG, assignment and artifact-contract validators. Plans contain nodes and no parts,
so the existing dispatcher runs ready tasks without invoking refinement. One task is valid; there
is no compulsory file-count/turn-count subdivision. Unknown file scope uses the existing full
workspace lock. Employee model selection is retained at creation and confirmation. Legacy stored
part-based plans remain unchanged, and explicit worker delegation remains available.

`LearningReview.automatic` distinguishes automatic work from an explicit manual request and
defaults to true for legacy records. Cancellation stops pending automatic work through
`skip_cancelled_learning`; source checks reject late model replies. `restore_learning` skips
cancelled automatic reviews instead of treating recovery as a new manual request. Completed
history, active lessons and already submitted independent platform jobs remain intact.

## Who owns what

| Area | Module | Key names |
|---|---|---|
| HTTP API, task lifecycle endpoints | `api.py` | `create_task`, `follow_up_task`, `resume_task`, `reopen_node`, `reply_to_diagnosis`, `set_network_access` |
| Records and statuses | `models.py` | `RunStatus`, `NodeStatus`, `Outcome` (attempt outcomes), `DiagnosisStatus`, `FINISHED`, `TaskNode`, `NodeAttempt`, `Diagnosis`, `ExecutionRun` |
| Declared artifact handoffs and stop limits | `contracts.py`, `models.py` | Optional `TaskNode.artifact_contract` (`ArtifactContract`: stage, external/dependency inputs, file/directory outputs, acceptance commands, blocking conditions, total turns/cost); `plan_problems`, `prepare`, `delivery_snapshot`, `accepted`; `ArtifactEvidence`, persistent `contract_turns/contract_cost_usd` |
| Storage (SQLite, JSON bodies) | `db.py` | `Database.save_run`, `get_run`, events, `save_retrospective`; `conversations`, `conversation_archives`, `model_calls` (task deletion removes all three) |
| Skills for the steps: SKILL.md files with the `tools` they cover, general in `skills/steps/`, this machine's in `<data_dir>/skills/` (first), public references in `<data_dir>/skills-discovered/`; a step is given those whose tools it names. Public references retain origin, immutable blob revision and content hash, and never claim local verification | `skills.py` | `library`, `for_step` (`quality.node_prompt`), `section`, `save_machine`, `save_discovered` |
| Optional reuse-first skill preparation: local/general/cache → bounded public GitHub repository/tree/blob search (only when the task permits network) → bounded sandbox generation. No remote install scripts run. Discovery is at most 30 seconds and 12 requests; generation uses `skill_preparation_timeout` and `skill_generation_max_turns`. Failures remain advisory | `skill_discovery.py`, `preflight.py` | `discover`, `Discovery`, `_prepare_skills`; `skill_reuse_first` (off by default); events `run.skill_searching`, `run.skill_selected`, `run.skill_generating` |
| The check before a task's first step, in a real step sandbox with the task's settings: the sandbox's output and exit code, the tools the lead listed (`TaskPlan.requires`, from `OUTLINE_PROMPT`) and, as warnings, the programs the steps' checks call, the internet, each API model; one environment repair, then a pause with what is missing (going on runs anyway); then this machine's skill for each special tool the task needs, written by a model trying it in a step's sandbox and kept for later tasks (`_prepare_skills`, `needed_tools`, `tool_groups`, `skill_prompt`; setting `preflight_skills`) | `preflight.py` (mixed into `Executor`) | `Preflighting._preflight`, `sandbox_checks`, `tool_commands`, `_model_checks`, `_preflight_repair`, `repair_prompt`; `ExecutionRun.preflight`, settings `preflight`, `preflight_command_timeout`, `preflight_network_url` |
| Looking back on naturally finished tasks: each step's estimate against its cost and turns, its problems named by kind and key, counted across tasks, marked when they come back after a repair; a diagnosis is told the same problem's earlier cases. Automatic records and backfill exclude manual cancellation; saved history remains readable | `retrospect.py` | `record` (on `END_EVENTS` in `Executor.emit`), `AUTOMATIC`, `review`, `repaired_areas`, `program`, `recurring`, `similar` (`diagnosis.repair_prompt` `history`); API `/api/retrospectives/recurring`, `/api/tasks/{id}/retrospective` |
| Turning retrospectives into verified improvements: separate bounded budget, recorded evidence, sandbox checks for skills, independent review for skills/advisory rules, platform engineer jobs for source/platform rules; immutable lesson snapshots in later plans and steps, comparisons only with the same model/difficulty mix | `learning.py` (mixed into `Executor`) | `Learning.finish_learning`, `start_learning`, `_learn`, `_validate_lesson`, `_learning_engineer`, `restore_learning`, `sync_improvements`; `selected`, `attach`, `instruction`, `observe`; `LearningSettings`, `LearningReview`, `Improvement`, `ExecutionRun.improvement_uses`; tables `learning_reviews`, `improvements`, `improvement_effects`, `learning_settings`; API `/api/tasks/{id}/learning`, `/api/improvements`, `/api/improvements/settings`, `/api/improvements/{id}/disable`; `docs/design/continuous-improvement.md` |
| Project employees and delegation | `team.py`, `models.py`, `executor.py` | Opt-in `employee_teams`: `form` chooses staff/models/capabilities/Skills before the outline; `Employee`, `PlanPart.employee_id`, `TaskNode.employee_id`; `route` preserves the chosen model, `context` supplies shared requirements/interfaces and delivery history, `refresh_skills` snapshots assigned instructions. `DELEGATE_TASKS` requests become validated child nodes in the same run; `_delegate_result` atomically saves the child graph and yielded parent, which resumes only after accepted children. One active task per employee, provider-aware admission, inherited contract costs/turns via `governors`. `frontend/team.tsx` shows staff, tasks, delegation and shared agreements. See `docs/design/project-team.md`. |
| Planning | `planner.py` | `autonomous_plan` (by the lead when one is available: `_plan_as_lead`), `_validate_plan` (flat plan, when no lead plans), `task_node`, `WORK_RULES`; project work as small packages (`TaskNode.files/reads/provides/check`, `package`, `scope_problems` sends broad steps back to the lead) |
| Direct executable task planning | `task_planning.py`, `planner.py`, `api.py` | Opt-in `direct_task_planning`: `plan_tasks` returns complete task matters, owners, dependencies and acceptance, with no later refinement. Reuses task parsing, contract gates and employee routing. See `docs/design/direct-task-planning.md`. |
| The lead plans top-down (docs/design/plan-parts.md): before the run, major parts (大项) by function with their contracts (`provides`); each part later split level by level until every piece is an independent task | `outline.py` | `plan_outline`, `OUTLINE_PROMPT`, `refine`, `part_opening`, `refine_message`, `siblings`, `task_problems`, `Tree`, `link` (a wait on a part becomes a wait on its last tasks), `waits_on_unsplit`, `MAX_DEPTH`, `MAX_TASKS`, `task_problems` (sends back a split with a line of `CHAIN_LIMIT` waits, `longest_chain`, or far more tasks than the part's `size`); `TaskPlan.parts/request/in_project/unsplit_cost`, `PlanPart.status/size/provides`, `TaskNode.part_id`, `TaskPlan.part_path`; estimate `routing.part_estimate`, `fit_and_route` |
| Splitting a running task's major parts into tasks, all at once, each in its own lead conversation; a part that cannot be split pauses the task. Combined-plan contract errors, including concurrent merge conflicts, return the rejected split and latest handoffs to the same conversation for at most `outline.ROUNDS` corrections before model fallback. Publication validates a copied plan under the run lock; cancelled/deleted tasks cannot receive late splits. With `adaptive_refinement`, the lead's per-part `difficulty/refinement_effort/refinement_reason` selects the conversation effort: routine low/medium, complex high, exceptional max with a reason. Old unannotated plans default to normal/medium; the main lead and retrospective policy stay independent | `refining.py` (mixed into `Executor`), `outline.py` | `_split_parts`, `_split_part`, `combine_split`, `SplitConflict`, `correction_message`, `REFINEMENT_EFFORT`; `SupervisorSession.reasoning_effort` → providers/CLI; events `part.refining`, `part.refine_attempt`, `part.refine_correcting`, `part.refined`, `part.refine_failed`; `frontend/plan-parts.tsx` shows the selection and reason |
| The task's lead: one conversation from planning to the end (docs/design/supervisor.md) | `supervisor.py` | `candidates`, `ask`, `handover`, `trim`; `Executor.ask_lead`, `ProviderRegistry.converse`, `CliChannelRegistry.converse`, `ExecutionRun.supervisor` |
| Model choice | `routing.py` | `route_node` (first pick; a hard step takes the cheapest within `hard_score_margin` of the best, `close_to_best`), `strongest_for` (retry), `stronger_than` (escalation), `spread_to` (a step past its provider's share of the running steps, `Executor._spread`, settings `max_steps_per_provider` (off by default), `spread_cost_factor`), `pick_reviewer` (the cheapest model no weaker overall than the one checked, `CapabilityScore.overall`), `diagnosers`; `node_cost` (an API model's tool loop turn by turn, cached input at the cache price: `LOOP_*`), `reestimate` (after splitting or replanning) |
| Scheduling, attempts, step outcomes | `executor.py` | `Executor`; see "Executor" below |
| Reviewing a result | `review.py` (mixed into `Executor`) | `_check`, `_reviewer`, `MAX_BACKUPS` |
| The lead's decisions after a failed attempt, and its questions to the user; while it decides, `ExecutionRun.lead_thinking` (`_thinking`); a repair it asks for (`Diagnosis.trigger == "lead_repair"`, `decision_id`) comes back to it (`_after_repair`) before the step runs again | `leading.py` (mixed into `Executor`) | `_decide`, `_ask_for_decision`, `_apply_decision`, `_after_repair`, `_decisions_left`, `start_lead_answer`, `start_lead_chat`; prompts in `supervisor.failure_message`, `repair_message`, `parse_decision` |
| Diagnosing failures and acting on them (when no lead decides; repairs the lead asks for) | `diagnosing.py` (mixed into `Executor`) | `_repair`, `_diagnose_with`, `_repair_done`, `begin_diagnosis`, `_run_diagnosis` |
| Repairing Super Partner itself (flow) | `self_repair.py` (mixed into `Executor`) | `_fix_platform`, `_activate_platform`, `_hold_for_platform`, `restore_platform`, `_finish_platform` (a repair the lead asked for goes back to it: `_platform_to_lead`) |
| The platform engineer: repairs and components the user (or a lead) asks for, as jobs; plan → approve → build in a copy → check → approve → apply → git commit (docs/design/platform-engineer.md) | `engineer.py` (mixed into `Executor`) | `EngineerJob`, `engineer_request`, `engineer_join` (into a task's conversation with its lead: by the user, the lead's chat `supervisor.split_engineer`, or any decision's `engineer` field; the lead hears progress, `_engineer_news`/`engineer_updates`), `engineer_reply`, `engineer_approve`, `engineer_reject`, `_engineer_run`, `_engineer_apply`, `_engineer_finished`, `_engineer_to_lead`, `commit`; API `/api/engineer`; prompts `plan_prompt`, `edit_prompt`; guide `skills/platform-engineer/SKILL.md` |
| Changing a running task's plan at the lead's decision | `replan.py` | `change_plan`, `PlanError` |
| What happens after a failure without a lead (fallback: retry model, meaning of a diagnosis) | `recovery.py` | `retry_model`, `failed_models`, `after_diagnosis`, `turns_for`, `MODEL_FAILURES` |
| Prompts for steps and reviews, verdict parsing | `quality.py` | `node_prompt`, `continue_prompt`, `attempt_notes`, `review_prompt`, `parse_review`, `UNABLE_RULE`, `unable_claim`, `retry_feedback` |
| Diagnosis prompts and parsing | `diagnosis.py` | `prompt`, `repair_prompt`, `platform_instructions`, `parse`, `STATUS` |
| API model calls, output caps, tool loop | `providers.py` | `ProviderRegistry.complete`, `_send`, `output_ceiling`, `_loop`, `_answer`, `_cache_point`, `reasoning_fields`; rejected effort is visibly recorded, or rejected outright with `reasoning_fallback=strict`; complete tool traces remain in `NodeAttempt.trace` |
| Durable conversations and structured memory | `conversation.py`, `providers.py`, `executor.py` | `Conversation`, `flush`, `reopen`, `handoff`, `_compact_context`, `Executor._conversation`; OpenAI-compatible/Anthropic/Gemini checkpoints at tool intent and each completion; generation/dependency/request fences; original archives accessible only through task-step-scoped `RecallHistory` |
| Call telemetry and admission | `calls.py`, `call_control.py` | `observed`, `ProviderGate`; request/retry counts, requested/sent effort, observed tokens/cache writes/reads, unknown usage, duration and queue time; configured provider-wide concurrency/RPM/TPM, shared Retry-After cooldown; `/api/tasks/{id}/calls` |
| Historical delivery cost | `routing_feedback.py`, `routing.py` | `collect`, `expected_cost`; minimum eight distinct steps per model/capability/difficulty, smoothed success probability and observed rework cost, bounded latency weight; applied before an automatically routed step's first attempt |
| Optional tool extensions and hooks | `extensions.py`, `agent.py` | `api_tool_extensions`, `api_tool_hooks`: JSON-schema-checked command tools and before/after hooks through the existing Workspace sandbox; empty by default; excluded from read-only review and host repair modes |
| Streamed replies (server-sent events) put back together into the provider's non-streamed reply; a request stops only when nothing arrives for `api_reply_deadline` (`ProviderRegistry._post`, `_stream`, `StreamSilent`) | `streaming.py` | `openai`, `anthropic`, `gemini`, `events`, `StreamError` |
| CLI model calls and their sandboxes | `cli_channels.py` | `CliChannelRegistry.complete`, `_with_network`, `repair_command` (the real /dev), `codex_settings` (Codex runs with the platform's reasoning effort, the lead's service tier, and without the user's plugins, apps and MCP servers: `CODEX_EXTRAS`, `codex_servers`); what a CLI step is told about its sandbox: `quality.cli_environment` |
| Readable folder names (`20261021-1441-async-fifo-uvm`) for tasks and leads; leads' folders outside any project | `folders.py` | `task_folder` (`ExecutionRun.folder_name`, from the outline's `folder`), `lead_folder`, `lead_root` (setting `lead_root`) |
| Project brief for planning (file summaries by the cheapest API model, refreshed for changed files only), and the files a step is given: a package's own files in full, otherwise the file list and the files it names | `briefing.py` | `planning_brief`, `refresh`, `render`, `attached_files`, `likely_files`; `Executor._with_files`, `quality.scope_section`, `routing.brief_writer` |
| A work package far past its expected turns goes to the lead (continue, or stop and then split) | `progress.py`, `review.py` | `expected_turns`, `OVERRUN_FACTOR`; `_overrun_check`, `supervisor.overrun_message` |
| Steps in one project at once: disjoint package files run together, unscoped steps alone | `scopes.py` | `FolderLock.hold`, `Scope.clashes`, `Scope.own`; `Executor._work` |
| Tools and sandbox for API models: Bash, Read, Write, Edit, MultiEdit, Glob, Grep, LS; Bash keeps its directory and the attempt's /tmp; platform data and other task folders stay hidden | `agent.py` | `TOOLS`, `Workspace.run`, `run_all` (reads at once unless hooks are configured), `bash`, `sandbox_args`, `network_prefix`; structured compaction `providers._compact_context`; effort by difficulty `settings.reasoning_by_difficulty`, for leads `lead_reasoning_effort`, for diagnoses `diagnosis_reasoning_effort` |
| Checking long attempts by default; explicit artifact contracts have hard cumulative limits; a step others wait for far past its expected minutes goes to the lead (`Review._critical_check`, `supervisor.critical_overrun_message`) | `progress.py` | `check_prompt`, `parse`, `api_evidence`, `cli_evidence`, `Stopped`, `limit_reason`; the check itself is `Executor._progress_watch` (review.py) |
| Per-request budget reservations, cost of a call | `budget.py` | `RequestBudget.reserve`, `token_cost` (cached input at the cache price; `providers.cached_tokens`) |
| Quota / outage detection and cooldown | `quota.py` | `QuotaTracker.classify`, `mark`, `exhausted` |
| Shared machine fixes (tools dir, env) | `environment.py` | `tools_dir`, `command_env`, `profiles`, `shell_setup` (scripts each step command sources first: setting `sandbox_profiles` or `"profiles"` in `sandbox.json`), `host_ports` (with `license_daemon_ports`: a license server's vendor daemons, from lmgrd's process tree in /proc, or for an lmgrd of another user by tracing `lmutil lmstat`: `_probe`, cached `PROBE_TTL`) |
| Self-repair: isolated copy, checks, activation | `platform_repair.py` | `PlatformRepairs.prepare`, `check`, `load`, `stale`, `diff`, `activate`, `SOURCE_DIRS` |
| Model catalog and benchmark scores | `catalog_sync.py`, `benchmarks.py`, `scoring.py` | `public_models`, `match_results`, `rescore` |
| Results listing, ZIP, GitHub publish | `publish.py` | `SKIP`, `result_markdown` |
| Settings (env `SUPER_PARTNER_*`) | `config.py` | `settings` |

## Executor

`Executor` (`executor.py`) mixes in `Reviewing` (`review.py`), `Leading` (`leading.py`), `Diagnosing`
(`diagnosing.py`), `SelfRepair` (`self_repair.py`), `Engineering` (`engineer.py`) and `Refining` (`refining.py`); they share its state (`db`, `locks`, `_complete`, `_save`, `emit`).

- `execute` / `_dispatch`: the run loop; starts every ready node, or up to `max_parallel_nodes` when that is set (0, the default: no limit).
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

Artifact work packages add gates without replacing the original acceptance criteria, task budget,
or sandbox permissions. Planning separates VDB collection, URG text reports and delivery audit
when these jobs are requested; it does not require all three for a report from an existing VDB,
a collection-only request, or unrelated coverage work. Flat planning, part refinement and replanning
share `contracts` validation; an invalid contract cannot fall back to an unbounded plan. Dependency
inputs must name an ancestor's declared output; external inputs need no producer. Outputs are
allowed to be absent before the step runs. VDBs are directories; overlapping directory/file scopes
are serialized by `scopes.py`.

`Executor._work` records the input fingerprints and the generation's output baseline before a
contracted step runs. `Reviewing._artifact_check` checks nonempty, fresh files/directories and runs
the declared commands through the ordinary `agent.Workspace` sandbox. Command results are saved in
the attempt trace and `ArtifactEvidence`, then the original review still applies. A consumer checks
the producer's accepted evidence and current fingerprints (including transitive inputs), so a changed
VDB invalidates its old report. Evidence paths reject traversal, symlinks and private files. A missing
audit output does not send already accepted upstream work back for rework.

`RequestBudget` preserves run-wide reservations and additionally caps a contracted step's calls,
including its reviews/progress checks. Calls for that step share reservations. Worker turns and all
associated API charges are persisted at each request and survive retry, model switch, rework and
restart. A hard turn cap makes no extra paid summary request. CLI or tool-less channels that cannot
enforce it return `blocked`. Missing prerequisites or an explicit `BLOCKED` result also block; a
limit without accepted delivery records `Outcome.STOPPED`. `_stop_contract` preserves the attempt,
logs, charges and a deterministic task diagnosis, blocks consumers and ends incomplete without
automatic diagnosis/retry. Lead acceptance and replanning cannot erase these gates or limits.
Legacy records have no contract and retain their existing execution policy.

Ordinary packages with a declared `check` now execute it in the task sandbox before model review.
A failed command cannot be overridden by the worker claiming success. Review selection treats
overall capability as a hard floor before API preference or independence; if only the producer
qualifies it is called in a fresh review conversation. With no qualified reviewer work remains
unverified. A progress check must explicitly return one valid CONTINUE; malformed conclusions stop
the attempt, and two successive failed progress checks cannot leave it running indefinitely.

API work conversations survive process loss, while startup still pauses interrupted tasks for
explicit resume. Checkpoints never replay an unrecorded tool completion automatically: its outcome
is unknown and must be inspected. Same-model continuations retain native tool history; model changes
receive a bounded handoff plus an archive reference. Near 65% of the context window, the same model
writes a six-field JSON memory. Only validated summaries replace history, original user instructions
remain verbatim, and originals remain task-scoped and retrievable. Summaries count against existing
request budgets and contracted turns. A failed summary preserves the checkpoint and stops the
attempt. See `docs/design/call-runtime.md` for configuration and limits.

## Tests

| Area | Tests |
|---|---|
| Team formation, assignments, delegation, recovery and shared budgets | `tests/test_team.py` |
| Direct tasks without refinement; planning stage effort, deadlines and staffing reuse | `tests/test_direct_planning.py`, `tests/test_planning_latency.py` |
| Executor flow, retries | `tests/test_executor.py`, `test_recovery.py`, `test_recovery_regressions.py`, `test_turns.py`, `test_resume.py` |
| Diagnosis | `test_diagnosis.py`, `test_diagnosis_interaction.py` |
| Unable steps | `test_unable.py` |
| Network, sandbox, tools | `test_network.py`, `test_agent.py`, `test_safety.py` |
| Providers, output caps | `test_providers.py`, `test_streaming.py`, `test_quota.py` |
| Durable calls, memory, model handoff, telemetry, gates, hooks and history-based routing | `test_call_runtime.py`, `test_continue.py` |
| Looking back on tasks | `test_retrospect.py` |
| Learning from finished tasks, validation, budget, reuse, effect comparisons | `test_learning.py`, `test_engineer.py` (automatic platform improvements) |
| Folder names | `test_folders.py` |
| Steps others wait for | `test_critical_path.py` |
| The check before the first step | `test_preflight.py` |
| Skills | `test_skills.py` |
| Spreading steps over providers | `test_spread.py` |
| Progress checks | `test_progress.py` |
| The lead's conversation | `test_supervisor.py` |
| The lead's decisions | `test_leading.py`, `test_replan.py` (plan changes, final review) |
| Planning in parts | `test_outline.py`, `test_packages.py` |
| Artifact contracts, freshness, audit gates and cumulative limits (temporary files and fake models, no URG dependency) | `test_packages.py`, `test_outline.py`, `test_replan.py`, `test_executor.py`, `test_turns.py`, `test_progress.py`, `test_review_evidence.py`, `test_leading.py`, `test_safety.py`; helpers in `artifact_helpers.py` |
| Review prompts and evidence | `test_quality.py`, `test_review_evidence.py` |
| Platform self-repair | `test_platform_repair.py` |
| The platform engineer | `test_engineer.py` |
| Catalog and benchmarks | `test_catalog_sync.py`, `test_benchmarks.py`, `test_scoring.py` |
| API-level flows | `test_core.py`, `test_followup.py`, `test_task_deletion.py`, `test_publish.py` |
| Web UI | `frontend/tests/mission-actions.test.mjs` |

Commands: `python -m pytest -q tests/test_x.py` (one area), `python -m pytest -q` (all backend,
about 35 s), `cd frontend && npm test` and `npm run build` (only when the frontend changed).

Live network sandbox tests use `tests/conftest.py`'s `network_enabled` fixture to honor the
validation controller's `SUPER_PARTNER_VALIDATION_ALLOW_NETWORK` setting. Tests with both offline
and online cases request it only for the online case, so offline command checks still run.
`test_platform_repair.py` exercises these gates with simulated workspaces and both permissions;
it needs neither a real network namespace nor a TUN device.

## Frontend (`frontend/`)

`src.tsx` (app shell, task creation, actions), `lead.tsx` (the task's conversation, in its own "对话" tab that opens when it waits for you: the lead's decisions, questions and messages), `lead-engineer.tsx` (the platform engineer in that conversation: its jobs' lines and cards, @平台工程师, bringing it in), `engineer-bubble.tsx` (the platform engineer's own chat, from a bubble in the page's corner; no navigation entry), `result-page.tsx` (results, requirement change),
`plan-parts.tsx` (the progress view: major parts as stages that open in place, `PlanView`), `plan-graph.tsx` (steps and attempts), `engineer.tsx` (the platform engineer's page), `diagnosis.tsx` (diagnosis chat), `mission-history.tsx`
(task list), `model-profiles.tsx` (models), `style.css` (tokens and styles).

`retrospective.tsx` is the result page's “复盘与改进” tab: learning settings and budget, conclusions,
items and validation evidence, platform-job progress, lessons used, effect comparisons and disabling future use.
`team.tsx` is the project’s “员工” tab: employee roles/models/Skills, assignments and task details, delegation parents, waiting children, and shared project agreements. Legacy runs show their unassigned tasks without inventing employees.

`call-insights.tsx` is its “模型调用” tab: sent effort and downgrade warnings, reported input/output/cache
tokens, missing usage, requests/retries and elapsed/queued time. Historical calls before this change
cannot be reconstructed and are not shown as zero usage.
