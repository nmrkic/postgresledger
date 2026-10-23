# postgresledger

[![PyPI](https://img.shields.io/pypi/v/postgresledger)](https://pypi.org/project/postgresledger/)
[![Python](https://img.shields.io/pypi/pyversions/postgresledger)](https://pypi.org/project/postgresledger/)
[![Tests](https://github.com/nmrkic/postgresledger/actions/workflows/tests.yml/badge.svg)](https://github.com/nmrkic/postgresledger/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/pypi/l/postgresledger)](https://github.com/nmrkic/postgresledger/blob/main/LICENSE)

Versioned migrations for PostgreSQL from plain SQL, without an ORM (tables,
views, materialized views, functions, triggers), built on
[pgschema](https://www.pgschema.com). The command is `pgl`.

`pgl` works on **one database per run**: the `DATABASE_*` variables from `.env`
(or the environment). Every database records which migrations it has; the
tool never needs to know about the others.

How it works, layout and rules:
[`docs/architecture.md`](https://github.com/nmrkic/postgresledger/blob/main/docs/architecture.md).

## What it does

- **Baseline** — dumps an existing database into `database/schema/schema.sql`
  and `0001_baseline.sql`, so an empty database can be recreated from it.
  A database that already has the same schema is recorded as up to date.
- **Migrations** — edit `schema.sql`, run `pgl new <name>`, get the next
  `NNNN_name.sql` (verified before it is kept).
- **Apply** — runs the pending migrations and records them in
  `postgresledger.schema_migrations` on the database.
- **Drift** — compares the database with what its migrations say it should
  be, and lists changes made by hand.
- **Capture** — keep a manual change: a new migration, recorded as already
  there on that database. (There is no command that drops manual changes on
  a database; that is done by hand or with a normal migration, on purpose.)
- **Snapshot / compare** — save a database's schema to a file and list the
  differences between two databases, object by object.

On the server, postgresledger only ever creates its tracking schema. Everything
that needs a scratch database (building "all migrations applied",
generating, drift, capture) runs in a throwaway Postgres on the
machine running `pgl`, deleted after each command (see
[Scratch Postgres](#scratch-postgres)).

## How it works

### Components

```mermaid
flowchart LR
    dev([developer / CI]) -- pgl --> pgl[postgresledger]
    pgl -- reads --> toml[postgresledger.toml<br/>project settings]
    pgl -- reads --> env[.env<br/>DATABASE_*]
    pgl -- dump / plan / apply --> pgschema[pgschema]
    pgl -- run migrations --> psql[psql]

    subgraph repo[Repository: database/]
        schema[schema/schema.sql<br/>desired state]
        migs[migrations/NNNN_name.sql]
        snaps[snapshots/name.sql]
    end

    subgraph target[The database in .env]
        db[(schema public<br/>+ postgresledger.schema_migrations)]
    end

    subgraph local[This machine, per command]
        scratch[(throwaway Postgres<br/>shadow + plan)]
    end

    pgl <--> schema
    pgl --> migs
    pgl --> snaps
    psql --> db
    pgschema --> db
    psql --> scratch
    pgschema --> scratch
```

### Scratch Postgres

Some commands need a database that can be dropped and recreated: `new`,
`drift`, `capture` and `baseline` build "every migration applied"
in one. That never happens on a real server, and it doesn't use Docker
either. pgl runs Postgres directly, as an ordinary process under your user:

- **Once per machine**, `pgl setup` downloads a ready-made Postgres 17 build
  (the server programs and their libraries, from the
  [zonky](https://github.com/zonkyio/embedded-postgres-binaries) project)
  and unpacks it into `~/.cache/postgresledger/postgres-<version>/`. Nothing is
  installed system-wide; no root, no `apt`, no Docker.
- **Per command**, pgl runs `initdb` into a fresh temp folder, starts the
  server on `127.0.0.1` on a random free port (not reachable from outside
  the machine), does its work in it, then stops it and deletes the folder,
  also when the command fails.

```mermaid
flowchart LR
    C([pgl drift]) --> I[initdb in /tmp/postgresledger-xxxx]
    I --> S[start postgres<br/>127.0.0.1, free port]
    S --> W[shadow: apply every migration<br/>compare with the database]
    W --> X[stop postgres<br/>delete the temp folder]
```

It takes about a second; afterwards no process, file or port is left. `apply`,
`status`, `snapshot` and `compare` don't start it. It cannot run as root
(Postgres refuses to), and needs Linux or macOS on amd64 or arm64.

Why not Docker: it would have to be installed and running everywhere `pgl`
runs (CI runners, servers), many of those don't allow it, and a container
starts slower than a local process.

### Workflow

Each arrow runs with the `.env` of the database it acts on.

```mermaid
flowchart TD
    A[Existing database, e.g. dev] -->|pgl baseline| B[schema.sql + 0001_baseline.sql<br/>recorded as baseline on dev]
    B -->|pgl apply, on uat / prod| C[Every database up to date]

    C -->|edit schema.sql<br/>pgl new name| D[NNNN_name.sql]
    D -->|pgl apply, on each database| C

    C -->|pgl drift| E{Manual changes?}
    E -->|no, exit 0| C
    E -->|yes, exit 1| F{Keep them?}
    F -->|pgl capture name, on prod| G[NNNN_name.sql<br/>recorded as captured on prod,<br/>not executed there]
    G -->|pgl apply on the other databases| C
    F -->|no: undo by hand,<br/>then pgl drift| C
```

### Generating a migration (`baseline`, `new`, `capture`)

```mermaid
flowchart TD
    S([start]) --> R[Start a throwaway Postgres on this machine]
    R --> A[Apply every existing migration to it]
    A --> P[pgschema plan<br/>schema.sql vs that state]
    P --> Q{Plan empty?}
    Q -->|yes| N([no changes, nothing written])
    Q -->|no| W[Write next NNNN_name.sql]
    W --> V[Apply it there too, plan again]
    V --> Q2{Plan empty?}
    Q2 -->|yes| OK([migration kept, throwaway Postgres deleted])
    Q2 -->|no| X([file deleted, command fails])
```

`baseline` dumps the database into `schema.sql` first and records `0001` as
`baseline` there; `capture` dumps the database into `schema.sql` first and
records the new migration as `captured` there. `new` does not touch the
database at all.

### Drift and capture

```mermaid
sequenceDiagram
    actor U as developer
    participant T as pgl
    participant S as throwaway Postgres
    participant P as prod (.env)

    Note over P: someone changes prod by hand
    U->>T: pgl drift
    T->>P: all migrations applied?
    T->>S: start, apply all migrations (expected state)
    T->>P: pgschema dump (actual state)
    T->>T: pgschema plan actual vs expected
    T-->>U: DRIFT: list of manual changes, exit 1

    U->>T: pgl capture orders_notes
    T->>P: pgschema dump -> schema.sql
    T->>S: generate + verify NNNN_orders_notes.sql
    T->>P: record NNNN as captured (not executed)
    Note over U: with the uat / dev .env: pgl apply
```

### Apply

```mermaid
flowchart LR
    S([pgl apply]) --> C{already applied files<br/>unchanged? checksum}
    C -->|no| E([fail: file was edited])
    C -->|yes| F[next pending NNNN_name.sql]
    F --> K{contains CONCURRENTLY?}
    K -->|no| T[BEGIN<br/>pg_advisory_xact_lock<br/>migration SQL<br/>INSERT schema_migrations<br/>COMMIT]
    K -->|yes| N[statements one by one<br/>then INSERT schema_migrations]
    T -->|more pending| F
    N -->|more pending| F
```

## Install

Requires Python 3.11+ and `psql`. `pgl init` / `pgl setup` download the rest
once per machine into `~/.cache/postgresledger/`: pgschema and a Postgres
server for the throwaway scratch databases (both pinned, SHA256 checked).

```bash
uv tool install postgresledger      # or: pipx install postgresledger
pgl --version
```

| OS | Install the prerequisites |
|---|---|
| Linux (Debian/Ubuntu) | `sudo apt install python3 postgresql-client` |
| macOS | `brew install python libpq && brew link --force libpq` |
| Windows | no pgschema build — use [WSL2](https://learn.microsoft.com/windows/wsl/install) and follow the Linux row |

Supported: Linux and macOS, amd64 and arm64. The scratch Postgres cannot
run as root (run `pgl` as a normal user, also in CI containers). To bump
pgschema or the scratch Postgres, change `VERSION` and the hashes in
`src/postgresledger/pgschema.py` / `src/postgresledger/scratch.py`.

## Usage

In the project that owns the database:

```bash
pgl init                       # postgresledger.toml, .env (git-ignored), database/schema/, database/migrations/
# set the DATABASE_* variables in .env (see below)

pgl baseline                   # 1. snapshot the existing database as 0001_baseline

# 2. edit database/schema/schema.sql, then
pgl new add_loyalty_and_coupons
pgl apply                      # --dry-run to preview
pgl status

# 3. someone changed the database by hand
pgl drift                      # exit code 1 on drift
pgl capture orders_notes       # keep it: other databases get it with `pgl apply`
                               # or undo it by hand, then `pgl drift` again

# 4. how do two databases differ?
pgl snapshot prod              # with prod's .env -> database/snapshots/prod.sql
pgl snapshot uat               # with uat's .env
pgl compare uat prod           # or `pgl compare prod`: the snapshot vs the database itself
```

The database comes from these variables in `.env`:

```bash
DATABASE_HOST=localhost        # default: localhost
DATABASE_PORT=5432             # default: 5432
DATABASE_USER=postgres         # default: postgres
DATABASE_PASSWORD=secret       # default: empty
DATABASE_NAME=app              # required
DATABASE_SSLMODE=require       # default: prefer
```

Each server or CI job has its own `.env`, or sets the variables in its
environment; a variable set there wins over `.env`. `pgl --env-file .env.uat …`
(or `PGL_ENV_FILE=.env.uat`) picks another file for one run.
Run `pgl` in the folder with `postgresledger.toml`, or point to it with
`--config PATH`. `pgl <command> -h` describes each command.
`baseline`, `new`, `capture` and `drift` print only what changed;
`--verbose` (`-v`) prints pgschema's whole plan, SQL included.

## Developing postgresledger

Needs [uv](https://docs.astral.sh/uv/) and Docker. This repository is also
the tool's test bed: a local Postgres in `docker/`
with `dev`, `stage` and `prod`, and demo data in `database/seed/`. `.env`
points at `dev`; `make <target> ENV=stage` runs one command on another
database, and `make apply-all` / `drift-all` / `status-all` /
`snapshot-all` loop over the three.

```bash
make setup        # uv sync + pgl setup
make up           # postgres:17 on localhost:55432
make seed         # demo schema + data into dev
make test         # unit tests + the full flow against the local Postgres
```

`make help` lists every target; `make reset` starts from scratch (empty
Postgres, seeded dev, no schema, migrations or snapshots).

## License

MIT, see [LICENSE](https://github.com/nmrkic/postgresledger/blob/main/LICENSE).
