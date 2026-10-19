# Super Partner

A local web app that plans a task into steps, runs each step on a chosen model (membership CLIs
or API models with sandboxed tools), reviews the results, and diagnoses and repairs failures.

- Backend: `backend/super_partner` (FastAPI, Python 3.9+). Frontend: `frontend` (React, Vite).
- Where things are: `docs/ARCHITECTURE.md`. Read it before searching the code.
- Fixing a defect in Super Partner itself: follow `skills/platform-repair/SKILL.md`.
- Adding a component to Super Partner itself: follow `skills/platform-engineer/SKILL.md`.
- Tests: `python -m pytest -q` (backend), `cd frontend && npm test && npm run build` (frontend).
- Lint: `ruff check backend tests`.
