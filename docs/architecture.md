# Database migration — architecture

Versioned migrations for an existing PostgreSQL database, built on top of
[pgschema](https://www.pgschema.com) (v1.13.1). pgschema is declarative: it
compares a desired-state SQL file with a live database and produces the DDL
that turns one into the other. `postgresledger` (command `pgl`) turns that into
numbered migrations with per-database tracking and drift detection. It is a
Python package whose only dependency is python-dotenv; it calls `pgschema`
and `psql`.

## One database per run

`pgl` knows exactly one database: the `DATABASE_*` variables, from the
environment or from `.env` next to `postgresledger.toml` (`--env-file` or
`PGL_ENV_FILE` for another file; a variable in the environment wins). It
never holds a list of environments. A developer's machine, UAT and prod each
run `pgl` with their own `.env`; what a database has is recorded in the
database itself.

| File | Holds | In git |
|---|---|---|
| `postgresledger.toml` | project settings, the same for every database: managed schema, paths, tracking schema | yes |
| `.env` | `DATABASE_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_NAME`, `_SSLMODE` of the one database | no |
| `.pgschemaignore` | what pgschema leaves out; from `pgl init`: every grant and default privilege (roles differ per database) | yes |

## Layout

| Path | What it holds |
|---|---|
| `pyproject.toml` | package `postgresledger`, commands `pgl` and `postgresledger` (uv_build) |
| `src/postgresledger/cli.py` | argument parsing and the help text, one list of commands |
| `src/postgresledger/commands.py` | the commands, including `init` and its `postgresledger.toml` / `.env` / `.pgschemaignore` templates |
| `src/postgresledger/core.py` | psql / pgschema calls, migration files, shadow database, tracking table |
| `src/postgresledger/config.py` | `postgresledger.toml` loading, the `DATABASE_*` variables from `.env` or the environment |
| `src/postgresledger/scratch.py` | the throwaway Postgres: pinned build, download, start / stop per command |
| `src/postgresledger/compare.py` | object-by-object comparison of two schema dumps |
| `src/postgresledger/pgschema.py` | pinned pgschema version + SHA256, download into `~/.cache/postgresledger/` |
| `tests/` | unit tests, and the full flow against the local Postgres (`make test`) |
| `postgresledger.toml`, `.env` | this repository's settings; `.env` points at the local `dev` |
| `database/schema/schema.sql` | desired state — the source of truth |
| `database/migrations/NNNN_name.sql` | generated migrations |
| `database/snapshots/<name>.sql` | `pgl snapshot` output, for `pgl compare` |
| `database/seed/` | demo schema + data, loaded only into `dev` |
| `docker/` | local Postgres 17 (`localhost:55432`) with `dev`, `stage`, `prod` |
| `docs/` | this document and the backlog |

## Scratch Postgres

Building "every migration applied" needs a database that can be dropped and
recreated. That never happens on a real server. Commands that need it
(`baseline`, `new`, `drift`, `capture`) start a Postgres on the
machine running `pgl`:

- binaries: a pinned relocatable build (`io.zonky.test.postgres` 17.11.0 from
  Maven Central), downloaded once into `~/.cache/postgresledger/postgres-<version>/`,
  SHA256 checked;
- `initdb` into a temporary folder, trust auth, `127.0.0.1` on a free port,
  no Unix socket, `fsync=off`; stopped and the folder deleted when the
  command ends, also on errors;
- inside it: `shadow` (every migration applied) and `plan` (pgschema's
  `--plan-db`, where it validates the desired state).

It cannot run as root (an `initdb` restriction). `apply`, `status`,
`snapshot` and `compare` don't start it.

On the target database postgresledger creates only `<meta_schema>`.

## Tracking

Every database has `<meta_schema>.schema_migrations`
(`version`, `name`, `checksum`, `mode`, `applied_at`, `applied_by`). It is
outside the managed schema (`public`), so pgschema never sees it.
`meta_schema` defaults to `postgresledger`; this repository sets `pgschema_meta`,
the name the tool used before it became a package, so the local databases
keep their history.

| `mode` | Meaning |
|---|---|
| `applied` | the migration was executed on this database |
| `baseline` | the database already had this schema; nothing executed |
| `captured` | this database is where a manual change was found; nothing executed |

The checksum is sha256 of the file. `apply` refuses to run if an applied
file was edited afterwards.

## Flows

**Generate a migration** (`baseline`, `new`, `capture`)

1. Start the scratch Postgres, apply every existing migration to `shadow`.
2. `pgschema plan --file schema.sql` against `shadow` → SQL. Empty = no
   change.
3. Write `NNNN_name.sql` (next number), apply it to `shadow`, plan again —
   it must be empty, otherwise the file is deleted and the command fails.

**Baseline** — without migrations: dump the database into `schema.sql`,
generate `0001_baseline` against an empty `shadow`, record it as `baseline`
on the database. With migrations (an existing database joining later): the
database must not track any yet; its dump is compared with `shadow`, and
only if they are identical every migration is recorded as `baseline`.

**Apply** — pending migrations in order. Each runs with `psql` inside one
transaction together with its row in the tracking table, under
`pg_advisory_xact_lock`. A file containing `CONCURRENTLY` cannot run in a
transaction: its statements run one by one and the row is written at the
end.

**Drift** — the database must have every migration applied. `shadow` is
built from migrations (the expected state), the database is dumped, and
`pgschema plan --file <dump>` against `shadow` lists what was changed by
hand. Exit code 1 on drift.

**Capture** — dump the database into `schema.sql` (manual changes become
the desired state), generate a migration as above, record it as `captured`
on the database. Other databases get it through `apply`.

**No revert** — there is deliberately no command that removes manual
changes from a database: run by accident on prod, it could drop a column
together with its data. Drift is kept with `capture`, or undone by hand /
with a normal migration.

**Snapshot / compare** — `snapshot <name>` writes `pgschema dump` of the
database to `snapshots_dir/<name>.sql`. `compare a b` splits two dumps into
objects (pgschema writes one block per object under `-- Name: …; Type: …`)
and lists objects only in a, only in b, and different (with a line diff).
No database is involved; without `b`, `a` is compared with a fresh dump of
the database. Exit code 1 on differences.

## Rules

- `schema.sql` changes only by hand (followed by `new`) or by `capture`;
  migration files are never edited after their first `apply`.
- Data changes (UPDATE, backfill, `REFRESH MATERIALIZED VIEW`) are added by
  hand to the generated file before it is applied anywhere.
- `capture` needs the database to be up to date first (`apply`). `drift`
  compares the database with the migrations it has and lists the pending
  ones separately.
- The `DATABASE_*` variables are passed to psql and pgschema as `PGHOST`,
  `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` and `PGSSLMODE`, always all
  six, so a `PG*` value left in the shell never points a run at another
  database.
