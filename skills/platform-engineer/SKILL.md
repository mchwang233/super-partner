---
name: platform-engineer
description: How to add a component to Super Partner itself (a model or channel, a tool or skill for the steps, a web UI feature, a scheduling or check feature) as its platform engineer. Use it when planning or building such a change in a platform-engineer copy. For fixing a defect, follow skills/platform-repair/SKILL.md instead.
---

# Adding a component to Super Partner

Any model can follow this: it needs only file reading, search, editing and a shell.
Super Partner gives it to the platform engineer for every component it plans or builds.

## Rules

- Change only Super Partner's source in the copy you were given. Do not touch task data,
  credentials, Git history, the live source, or running services.
- Keep unrelated existing changes. Never delete or weaken an existing test.
- Every component comes with tests that fail without it and pass with it.
- Build on what exists: find the owning module in `docs/ARCHITECTURE.md` and extend it.

## Planning (nothing is edited yet)

1. Read `docs/ARCHITECTURE.md`. Search (`grep -n`) for the names involved and read only those
   line ranges, not whole large files.
2. Find the closest existing example and follow it:
   - a model or channel: `catalog.py` (models), `providers.py` (API), `cli_channels.py` (`CLI_SPECS`);
   - a tool for API models: `agent.py` (`Workspace`, its tool list and sandbox);
   - a skill: a folder under `skills/` with a `SKILL.md`;
   - a web UI feature: the component in `frontend/` named in the map, styles in `style.css`;
   - scheduling, steps, checks: `executor.py`, `review.py`, `quality.py`, `planner.py`.
3. Write the plan the user approves: 目标, 改动 (each file), 测试, 风险. Keep it small enough to
   build and check in one go; if it is bigger, plan the first useful part and name the rest.
4. Ask the user (QUESTION / OPTIONS / STATUS: NEEDS_INPUT) only for a choice that is theirs:
   cost, credentials, behaviour they must decide.

## Building

1. Follow the approved plan. If something in it turns out wrong, do the closest thing that works
   and say what changed and why in the report.
2. Write the tests in the test file for that area (table in `docs/ARCHITECTURE.md`), reusing the
   fixtures and helpers already there. Web UI: `frontend/tests/mission-actions.test.mjs`.
3. Prefer one place for a decision over the same check in several modules, and an existing helper
   over a new parameter threaded through every layer.
4. Check, from the narrowest to the widest:
   - `python -m pytest -q tests/<area>.py`
   - `python -m pytest -q` (all backend)
   - only if the frontend changed: `cd frontend && npm test && npm run build`
   Fix every failure your change causes.
5. Update `docs/ARCHITECTURE.md` for a new module or a moved responsibility.

## Report

In Chinese, short: 改了什么 (each file), 测试 (names and what they prove), 验证 (commands and
results). End with `STATUS: FIXED`.
