# Database migration — backlog

Ideas for `postgresledger` without a worked-out plan yet.

## Tool (`postgresledger`)

- **`CONCURRENTLY` migrations run outside a transaction** — if one fails
  halfway it stays partly applied and is not recorded in the meta table; a
  second `apply` fails (`ADD COLUMN` has no `IF NOT EXISTS`). Idea: split
  the file by group (pgschema has them in `--output-human`, not in
  `--output-sql`) or apply through `pgschema apply --plan`.
- **Rollback migrations** — today a bad migration is undone by rolling
  forward (undo the change in `schema.sql`, `pgl new`). Idea: every
  migration gets its own rollback file, written when the migration is made
  (e.g. `0004_add_email.rollback.sql`, generated from "state before" vs
  "state after" in the scratch Postgres), and `pgl rollback` runs it and
  removes the record. Same data-loss caution as any DROP.
- **Data migrations** — hand-written SQL (UPDATE, backfill, `REFRESH
  MATERIALIZED VIEW`) is currently added to the generated file before the
  first `apply`; maybe a separate file/section that is not generated.
- **Migration number on `capture`** — two branches can get the same number;
  a CI check, or a timestamp instead of a sequence number.
- **Adding an existing schema to `schemas`** — `capture` takes it in on one
  database, but the other databases that already have the schema cannot run
  that migration. Needs a way to record one migration as `baseline` on them
  (e.g. `pgl baseline --version NNNN`, checked against `drift` first).
- **References in both directions between two schemas** — the plan database
  and the migrations load schemas in the order of `schemas`, so a schema can
  only refer to the ones before it.
- **Docker image for `postgresledger`** (pgschema + psql + Python) for CI and deploy.
- **Publish to PyPI** — the name `postgresledger` was free on 2026-10-21; until
  then it installs from git (`uv tool install git+<url>`).

## Risks

- Materialized view with dependent views — `DROP ... RESTRICT` fails; check
  what pgschema does when a view depends on a materialized view.
- pgschema is a young project (v1.13.1) — before the real database, check
  the object types we use (partitioning, extensions, RLS, grants).
