# Knowledge Agent Service Instructions

## Scope

- This repository owns the independent FastAPI service, lifecycle-aware SQLite memory store, separate vector index, deterministic entity linking, and additive Neo4j graph adapter.
- The service must remain independent of VS Code, the extension runtime, and any OpenBMC checkout.
- Treat HTTP request and response shapes as a cross-repository contract with the Context Recall extension. Preserve compatibility unless both repositories are updated and validated together.

## Data And Safety Boundaries

- Preserve immutable source evidence and stable evidence IDs. Derived vectors and graph claims must remain traceable to their source records or document chunks.
- Preserve lifecycle distinctions between `raw`, `candidate`, and active memory. Default search must not return inactive knowledge as active fact.
- Keep imports, migrations, synchronization, and graph writes additive and idempotent. Do not add implicit reset, clear, or delete behavior.
- Keep relink operations dry-run by default. Any committed graph mutation must require an explicit caller action.
- Never commit, return, or log credentials. Health and status responses may expose safe counts and readiness only, not secrets or credential-bearing configuration.
- Keep model inference outside this service; accept caller-supplied semantic proposals and validate them before persistence.

## Implementation

- Support Python 3.10 and newer and preserve the existing package layout under `src/knowledge_agent_service`.
- Use structured parsing and typed request models for API data. Avoid ad hoc manipulation of serialized payloads.
- Keep SQLite memory and vector stores separate. Preserve WAL behavior and deterministic, reproducible entity linking.
- Neo4j is optional. The API must degrade clearly and retain useful SQLite behavior when Neo4j is unavailable.
- Changes to API schemas, vector dimensions, lifecycle semantics, or graph identifiers require focused compatibility tests.

## Validation

- Run `.venv/bin/python -m pytest` after Python changes when the project virtual environment exists; otherwise run `python3 -m pytest` in an environment with the development dependencies installed.
- Add or update focused tests under `tests/` for every behavior change.
- Run the full test suite for API contract, migration, lifecycle, vector, or graph changes.