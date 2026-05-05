# Changelog

## 0.3.0 - 2026-05-05

- Added origin-aware lifecycle metadata: `sourceChannel`, `sourceSessionKey`, `sourceMessageId`, and `mirrorChannels`.
- Added dependency-gate metadata: `notBefore` and `blockedBy`.
- Preserved the new optional metadata across add, claim, complete, fail, retry, list, and grouped read views.
- Added SQLite schema migration for existing `0.2.x` databases.
- Bumped package version to `0.3.0`.

## 0.2.0 - 2026-04-09

- Made SQLite the canonical TaskMesh backend.
- Added grouped read adapter compatibility.
- Aligned concurrency behavior around SQLite transactions and WAL mode.
