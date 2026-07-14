# Repository operating rules

## Deployment and Git

- Every change that is deployed or used by the running service must be committed to Git before the work is reported complete.
- Do not deploy from a dirty worktree. If an emergency deployment temporarily makes this unavoidable, run the quality gate and commit the exact deployed state immediately afterward.
- Do not push commits or create remote releases unless the user explicitly requests it.

## Required pre-commit quality gate

Before committing any application or deployment change, run:

```bash
./scripts/quality_gate.sh
```

The gate must pass completely. It includes tests, syntax checks, linting, a dependency-vulnerability audit, and a static application-security scan. Do not bypass, weaken, or ignore a failing check. Fix the finding or clearly stop and report the blocker.

`gcommit.sh` runs this gate automatically. Keep the local gate and GitHub Actions CI aligned whenever checks change.
