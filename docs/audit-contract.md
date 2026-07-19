# Audit contract — 2026-07-19

- Root: the repository checkout (or installed `PI_WORKFLOWS_HOME`)
- Mode: repair and re-audit
- Definition of done: the extracted product is independently installable and
  operable; Agent X, Codex, Claude Code, and Loops use documented adapters; the
  create, validate, run, inspect, schedule, and graph paths have direct proof.
- Included: runner, factory, schemas, CLI, Pi extension, shared skill, installer,
  tests, agent adapters, Loops API/static integration, and live runtime.
- Excluded: generated runs/cache/output, provider billing accuracy outside Pi's
  returned usage, remote publication, and unrelated Agent workspace features.
- Protected behavior: DAG ordering, conditional route validation, gates,
  bounded retries/judges, content cache, cost ledger, QA, immutable evidence,
  existing Loops workflows, and Agent X tool compatibility.
- Required checks: unit tests, Python compile, CLI doctor, clean install, a
  no-model command workflow, Agent X tool registration, Loops API smoke, and
  browser verification at 1280x800.
- Non-goals: remote repository creation, npm publication, broad Loops redesign,
  or changing unrelated Agent X orchestration behavior.
- Repair authorized by Ali's active goal. No commit, push, or deploy is implied.
