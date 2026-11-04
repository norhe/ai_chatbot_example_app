# Voyager Travel — AI Agent Security Training App

A training application that shows, step by step, how to secure an AI booking assistant
(Ollama + Flask + PostgreSQL). Each phase is a self-contained directory with its own
README, dependencies and run instructions. The phases were extracted from this repository's
git history, so you can also diff them to see exactly what each step changes:

```bash
diff -ru phase1-monolith phase2-microservices
```

| Phase | Directory | Concept | Security posture |
|---|---|---|---|
| 1 | [`phase1-monolith`](phase1-monolith/README.md) | All agent/tool code runs inside `app.py` | Simple, but agent access cannot be secured: one process, one DB credential |
| 2 | [`phase2-microservices`](phase2-microservices/README.md) | Each agent is its own HTTP microservice | Creates a per-agent boundary, but nothing is enforced yet (stub token check, shared `DATABASE_URL`) |
| 3 | [`phase3-vault-approle`](phase3-vault-approle/README.md) | HashiCorp Vault + AppRole | Each agent gets its own ephemeral, least-privilege DB credentials |
| 4 | [`phase4-vault-oidc`](phase4-vault-oidc/README.md) | Keycloak OIDC + Vault agentic-identity setup | User JWT validation, role-based access, agent registration in Vault |

## Provenance

| Phase | Source |
|---|---|
| 1 | git commit `8aefde0` (tools + DB in-process) |
| 2 | Derived from commit `604d40f` with all Vault code removed (no commit in history has HTTP services without Vault) |
| 3 | git commit `604d40f` (Vault AppRole + dynamic DB credentials) |
| 4 | `main` at `ca220d2` |

## Running a phase

Every phase needs Python 3.12+ with [uv](https://docs.astral.sh/uv/), a local
[Ollama](https://ollama.com/) and a PostgreSQL instance. `cd` into the phase directory and
follow its README. Only one phase should be run at a time (they share ports 5000 and 5101–5104
and the same database).
