---
name: platform-repair
description: How to diagnose and fix a defect in Super Partner itself (scheduler, retries, model routing, providers, sandbox, API, web UI) quickly and safely. Use it when a diagnosis ends with STATUS: PLATFORM_REPAIR, when you are editing Super Partner's own source in a platform-repair copy, or when asked to fix Super Partner's code.
---

# Repairing Super Partner

Any model can follow this: it needs only file reading, search, editing and a shell.
Super Partner gives it to every model that diagnoses or repairs the platform.

## Rules

- Change only Super Partner's source in the copy you were given. Do not touch task data,
  credentials, Git history, the live source, or running services.
- Keep unrelated existing changes. Never delete or weaken an existing test.
- Every fix needs a regression test that fails without the fix and passes with it.
- Fix the cause in the module that owns the behaviour; see `docs/ARCHITECTURE.md`.

## Diagnosing (before any edit)

1. Read `docs/ARCHITECTURE.md` to find the owning module. Do not read whole large files:
   search (`grep -n`) for the names involved, then read only those line ranges.
2. Tie the failure to evidence: the attempt's outcome and reason, events, logs, the code path.
3. Report where the defect is, one line per place, then the status line:

   ```
   LOCATION: backend/super_partner/executor.py:847 _retry_or_fail — retries keep the same model
   LOCATION: tests/test_turns.py — add: two rejections in a row switch the model
   STATUS: PLATFORM_REPAIR
   ```

   The repair phase starts from these lines, so make them exact.

## Repairing

1. Start at the LOCATION lines from the diagnosis. Read the surrounding function and its callers,
   not the whole module.
2. Write the regression test first, in the test file for that area (table in
   `docs/ARCHITECTURE.md`). Reuse the fixtures and helpers already in that file.
3. Make the smallest change that fixes the cause. Prefer:
   - a decision in one place over the same check copied into several modules;
   - extending an existing helper over threading a new parameter through every layer. If a new
     parameter must cross layers, keep it to the call path that needs it.
   If the proper fix needs more than about three modules, fix the defect now with the smallest
   safe change and describe the larger change in the report.
4. Check, from the narrowest to the widest:
   - `python -m pytest -q tests/<area>.py`
   - `python -m pytest -q` (all backend, about 35 s)
   - only if the frontend changed: `cd frontend && npm test && npm run build`
   Fix every failure your change causes. A failure that also happens without your change
   (for example a sandbox limit of your own session) is noted in the report, not hidden.
5. If you moved a responsibility between modules, update `docs/ARCHITECTURE.md`.

## Report

Write it in Chinese, short, in this order, then the status line:

- 根本原因: what was wrong and why, with `file:line`.
- 改了什么: each file and the change.
- 回归测试: the test name and what it proves.
- 验证: the commands you ran and their results.

End with `STATUS: FIXED`. If a real choice belongs to the user (cost, credentials, behaviour
they must decide), ask instead:

```
QUESTION: <one question>
OPTIONS: ["<option 1>", "<option 2>"]
STATUS: NEEDS_INPUT
```
