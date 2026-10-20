---
name: platform-rules
description: Limits on any change to Super Partner's own source, by its platform engineer or a platform repair. Super Partner runs on many machines; facts about one machine belong in settings, not in code. Use it together with skills/platform-repair/SKILL.md or skills/platform-engineer/SKILL.md whenever you edit Super Partner itself.
---

# Rules for changing Super Partner itself

Super Partner is one program that runs on many machines. A change to its source reaches every
one of them, so it must be right for all of them, not only for the machine where the problem
showed up.

## 1. No facts about this machine in the source

Never write into code, prompts or tests a fact that is true only here:
absolute paths of installed tools or files (`/workspace/...`, `/opt/...`, `/home/<user>/...`),
host names, ports, user names, tool versions, license servers, environment scripts.

Put such a fact where machine settings live, and make the code read it:

- a setting in `backend/super_partner/config.py` (an environment variable `SUPER_PARTNER_...`),
  whose default does nothing (empty list, empty string, off);
- or the recorded environment in `.super-partner/tools/sandbox.json` (`environment.py`:
  `env`, `host_ports`, ...), which environment repairs write for this machine.

Tests use temporary paths and set the setting themselves; they never depend on what is installed.
In the report, say which setting this machine needs and its value (see 6).

## 2. Is it the machine, or Super Partner?

If the problem is this machine's setup (a tool not installed, a service stopped, a path or a
variable missing), it is an environment repair: record it for this machine (sandbox.json, the
shared tools folder). Change the source only when Super Partner behaves wrongly on any machine,
for example it cannot use a setting that exists, or it loses output.

## 3. The sandbox stays closed by default

Do not widen what every step can reach (network, host paths, host ports, credentials, the user's
home) to fix one task. Open exactly what is needed, through a setting that is off by default, and
say in the report what steps can reach now that they could not before.

## 4. Small, compatible, reversible

- No new dependencies (pip, npm, system packages) unless the user agreed.
- Stored records (tasks, jobs, settings files) written by the old version must still load: give
  new fields defaults.
- Keep existing behaviour for anyone who does not set the new setting.

## 5. Do not use git

The copy you work in sits inside the live repository, so git commands there act on the live
one. Do not run git at all (no `stash`, `checkout`, `reset`, `commit`, `clean`, not even `diff`).
To check that a test fails without your change, copy the original file aside, or use a scratch
copy outside the repository.

## 6. Report

Besides what the repair or component guide asks for, add one line if this machine needs anything:

- 本机需要: the setting or recorded value and what to set it to (or "无").
