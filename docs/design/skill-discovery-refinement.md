# Reuse skills and size refinement effort

Two opt-in settings change preparation without changing worker or retrospective policy:

- `SUPER_PARTNER_SKILL_REUSE_FIRST=true`: use an applicable local, bundled or cached skill first.
  A machine note takes precedence; a general skill is sufficient and does not automatically
  require a machine-specific rewrite. When none covers the tools and the task allows network,
  search public GitHub repositories for those tool names, inspect a bounded set of SKILL.md
  blobs, validate format/tool relevance, and cache a matching general reference. Record the
  source URL, immutable blob revision and cached text hash. Only Markdown instructions are
  imported; linked scripts/assets are not installed or executed by discovery. Public text is
  advisory, not proof that commands were tested here, and cannot change task permissions.
- `SUPER_PARTNER_ADAPTIVE_REFINEMENT=true`: the lead's existing outline call assesses each
  part in the context of the project and records difficulty, refinement effort and a short
  rationale. Simple well-specified work normally uses low, routine work medium, complex
  constraints high; max needs an explicit justification. Missing/invalid selections fall back
  to low/medium/high by difficulty. No extra model call is needed to select effort. Old records
  still load; unannotated parts use medium. Each part's independent session carries its choice
  through new and resumed Codex/Claude conversations and API fallback. Main-lead and learning
  sessions keep their existing settings. The part card shows the chosen effort and rationale.

Both settings default off for existing installations. Tasks that disallow network never run
discovery. Public repository search uses no credentials and sends only tool names, not task text
or local paths. Search stops after 30 seconds, 12 requests or 6 candidate documents; redirects
are not followed and response/document sizes are bounded. Rate limits produce a recorded miss
instead of a long retry loop. A search miss falls back to local sandbox generation, bounded by
`SUPER_PARTNER_SKILL_PREPARATION_TIMEOUT` and `SUPER_PARTNER_SKILL_GENERATION_MAX_TURNS` (6 by
default in reuse-first mode); an invalid result or timeout leaves a warning and lets the task
start. Previously cached skills can be reused offline.

Local policy can set skill preparation to 120 seconds. This is a limit per missing tool group,
not a promised task-start latency. The default remains unchanged for installations that have
not opted in. Search quality and generation effectiveness are observed through skill events;
refinement effort and model attempts are persisted in part events and model-call telemetry.

Protocol references: [GitHub repository search](https://docs.github.com/en/rest/search/search#search-repositories),
[Git trees](https://docs.github.com/en/rest/git/trees#get-a-tree),
[Claude CLI effort](https://code.claude.com/docs/en/cli-reference).
