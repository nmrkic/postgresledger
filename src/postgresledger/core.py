"""psql / pgschema calls, migration files, the scratch databases and the tracking table."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from . import pgschema
from .compare import SCHEMA_MARKER
from .config import Config, Connection, relative

MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
ADVISORY_LOCK_KEY = 727274  # same key everywhere -> two applies on one database never run in parallel
# databases on the scratch server only
SHADOW_DB = "shadow"
PLAN_DB = "plan"


def run(command: list[str], connection: Connection, *, stdin: str | None = None,
        show_stderr: bool = False) -> subprocess.CompletedProcess:
    """Run psql or pgschema against `connection`; if it fails, show its output and stop pgl.

    With `show_stderr` the program's log goes straight to the terminal while it runs
    (only stdout is captured), e.g. pgschema --debug.
    """
    # the shell environment (PATH, HOME, ...) plus the PG* variables of this connection
    environment = {**os.environ, **connection.environment()}
    program = Path(command[0]).name
    stderr = None if show_stderr else subprocess.PIPE
    if show_stderr:
        # pgl's own lines first: when stdout is a pipe (CI, | less) Python would print them later
        sys.stdout.flush()
    try:
        return subprocess.run(command, input=stdin, env=environment, text=True,
                              stdout=subprocess.PIPE, stderr=stderr, check=True)
    except subprocess.CalledProcessError as error:
        # the program ran and failed: its own output says why (stderr is None when already shown)
        program_output = (error.stdout + (error.stderr or "")).strip()
        sys.exit(f"{program} failed on {connection.label}:\n{program_output}")
    except OSError as error:
        # the program could not run at all, e.g. psql is not installed
        sys.exit(f"could not run {program}: {error}")


def psql(connection: Connection, sql: str) -> str:
    """Run SQL (one statement or a whole script) on `connection` and return psql's output."""
    command = [
        "psql",
        "--no-psqlrc",  # ignore the user's ~/.psqlrc
        "--quiet",
        "--set", "ON_ERROR_STOP=1",  # stop at the first error, exit code != 0
        "--no-align",  # plain values, separated by |
        "--tuples-only",  # no headers, no row count
    ]
    return run(command, connection, stdin=sql).stdout.strip()


@dataclass
class Migration:
    version: str
    name: str
    path: Path

    @property
    def id(self) -> str:
        return f"{self.version}_{self.name}"


def list_migrations(cfg: Config) -> list[Migration]:
    cfg.migrations_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(cfg.migrations_dir.iterdir()):
        if m := MIGRATION_RE.match(p.name):
            out.append(Migration(m.group(1), m.group(2), p))
    versions = [m.version for m in out]
    if len(versions) != len(set(versions)):
        sys.exit("two migrations share the same version number")
    return out


# Tracking table (on the target database)


def ensure_meta(cfg: Config, connection: Connection) -> None:
    psql(connection, f"""
        CREATE SCHEMA IF NOT EXISTS {cfg.meta_schema};
        CREATE TABLE IF NOT EXISTS {cfg.meta_table} (
            version     text PRIMARY KEY,
            name        text NOT NULL,
            checksum    text NOT NULL,
            mode        text NOT NULL CHECK (mode IN ('applied', 'baseline', 'captured')),
            applied_at  timestamptz NOT NULL DEFAULT now(),
            applied_by  text NOT NULL DEFAULT current_user
        );""")


@dataclass(frozen=True)
class AppliedMigration:
    """One row of the tracking table: a migration this database has."""

    version: str
    name: str
    checksum: str
    mode: str  # applied, baseline or captured
    applied_at: str
    applied_by: str  # the database user that ran it


def applied(cfg: Config, connection: Connection) -> dict[str, AppliedMigration]:
    """The migrations this database has, by version."""
    ensure_meta(cfg, connection)
    # Postgres returns the rows as one JSON array of objects, so every value is read by its column name
    rows_as_json = psql(connection, f"""
        SELECT coalesce(json_agg(json_build_object(
                   'version', version,
                   'name', name,
                   'checksum', checksum,
                   'mode', mode,
                   'applied_at', to_char(applied_at, 'YYYY-MM-DD HH24:MI:SS'),
                   'applied_by', applied_by
               ) ORDER BY version), '[]')
        FROM {cfg.meta_table}""")
    applied_by_version = {}
    for row in json.loads(rows_as_json):
        applied_by_version[row["version"]] = AppliedMigration(**row)
    return applied_by_version


def record_migration_sql(cfg: Config, migration: Migration, mode: str) -> str:
    """The INSERT that records `migration` in the tracking table, as text.

    Returned instead of run, so it can go into the same transaction as the migration itself.
    applied_at and applied_by are filled in by the table's defaults.
    """
    checksum = hashlib.sha256(migration.path.read_bytes()).hexdigest()
    # none of the values can contain a quote: version is digits, name is [a-z0-9_],
    # checksum is hex and mode is one of applied / baseline / captured
    return (f"INSERT INTO {cfg.meta_table} (version, name, checksum, mode) VALUES "
            f"('{migration.version}', '{migration.name}', '{checksum}', '{mode}');")


def apply_file(cfg: Config, connection: Connection, migration: Migration, mode: str = "applied") -> None:
    """Apply one migration and record it in the tracking table - atomically whenever possible."""
    migration_sql = migration.path.read_text()
    record_sql = record_migration_sql(cfg, migration, mode)
    # CREATE INDEX CONCURRENTLY and the like cannot run inside a transaction
    runs_in_transaction = "CONCURRENTLY" not in migration_sql.upper()
    if runs_in_transaction:
        script = (f"BEGIN;\nSELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});\n"
                  f"\\i {migration.path}\n{record_sql}\nCOMMIT;\n")
    else:
        script = f"\\i {migration.path}\n{record_sql}\n"
    psql(connection, script)


def check_checksums(connection: Connection, migrations: list[Migration],
                    applied_by_version: dict[str, AppliedMigration]) -> None:
    """Stop when an applied migration file was edited since; warn about applied migrations missing here."""
    for migration in migrations:
        applied_migration = applied_by_version.get(migration.version)
        if applied_migration is None:
            continue
        checksum = hashlib.sha256(migration.path.read_bytes()).hexdigest()
        if applied_migration.checksum != checksum:
            sys.exit(f"{migration.path.name} was edited after it was applied to {connection.label} (checksum mismatch)")
    known_versions = {migration.version for migration in migrations}
    for applied_migration in applied_by_version.values():
        if applied_migration.version not in known_versions:
            print(f"! {connection.label} has migration {applied_migration.version}_{applied_migration.name} "
                  "that is not in the repository")


def require_up_to_date(cfg: Config, connection: Connection) -> None:
    applied_by_version = applied(cfg, connection)
    pending = [migration.id for migration in list_migrations(cfg) if migration.version not in applied_by_version]
    if pending:
        sys.exit(f"{connection.label} is not up to date (pending: {', '.join(pending)}) - run `pgl apply` first")


# pgschema and the scratch server


def reset_database(scratch: Connection, database: str) -> Connection:
    """Drop and recreate `database` on the scratch server and return a connection to it."""
    psql(scratch, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    psql(scratch, f'CREATE DATABASE "{database}"')
    return replace(scratch, database=database)


def build_shadow(cfg: Config, scratch: Connection, migrations: list[Migration]) -> Connection:
    """Scratch database = empty database + `migrations`: the state those migrations describe."""
    shadow = reset_database(scratch, SHADOW_DB)
    ensure_meta(cfg, shadow)
    for migration in migrations:
        apply_file(cfg, shadow, migration)
    return shadow


def existing_schemas(cfg: Config, connection: Connection) -> list[str]:
    """The managed schemas `connection` has, in the order of the config."""
    # schema names are checked in config.py: lowercase letters, digits and _ only
    quoted_names = ", ".join(f"'{schema}'" for schema in cfg.schemas)
    found = psql(connection, f"SELECT nspname FROM pg_namespace WHERE nspname IN ({quoted_names})").splitlines()
    return [schema for schema in cfg.schemas if schema in found]


def create_missing_schemas(cfg: Config, scratch_database: Connection) -> list[str]:
    """Create the managed schemas a scratch database doesn't have yet and return them.

    pgschema plans only inside a schema that exists; the schemas created here are the ones
    no migration has created yet.
    """
    present = existing_schemas(cfg, scratch_database)
    missing = [schema for schema in cfg.schemas if schema not in present]
    for schema in missing:
        psql(scratch_database, f"CREATE SCHEMA {schema}")
    return missing


def schema_files_in(cfg: Config, folder: Path) -> dict[str, Path]:
    """One <schema>.sql in `folder` for every managed schema, e.g. for dumps that are thrown away."""
    files = {}
    for schema in cfg.schemas:
        files[schema] = folder / f"{schema}.sql"
    return files


def load_plan_database(cfg: Config, scratch: Connection, desired_files: dict[str, Path]) -> None:
    """Recreate the plan database; with several schemas, load the desired state of all of them into it.

    pgschema loads the desired state of one schema into a temporary schema of the plan database.
    References to another schema (REFERENCES public.customer) must find that object there.
    """
    plan_database = reset_database(scratch, PLAN_DB)
    if len(cfg.schemas) == 1:
        return
    # a function body may use a schema that is loaded after it
    script = ["SET check_function_bodies = off;"]
    for schema, desired in desired_files.items():
        script.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
        script.append(f"SET search_path TO {schema};")
        script.append(f"\\i {desired}")
    psql(plan_database, "\n".join(script))


def plan_arguments(scratch: Connection) -> list[str]:
    """pgschema validates the desired state in a database on the scratch server (not its own embedded one)."""
    return [
        "--plan-host", scratch.host,
        "--plan-port", str(scratch.port),
        "--plan-user", scratch.user,
        "--plan-db", PLAN_DB,
        "--plan-sslmode", "disable",
    ]


def pgschema_dump(schema: str, connection: Connection, output_file: Path, verbose: bool = False) -> None:
    """One schema of `connection` into `output_file`.

    With `verbose`, pgschema's debug log is shown as it happens: it says when the connection
    is being made and when it is up, so a slow connection and a slow dump can be told apart.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)
    command = [pgschema.locate(), "dump", "--schema", schema]
    if verbose:
        command.append("--debug")
    dump = run(command, connection, show_stderr=verbose).stdout
    output_file.write_text(dump)


def dump_schemas(cfg: Config, connection: Connection, output_files: dict[str, Path], verbose: bool = False) -> None:
    """Every managed schema of `connection` into its file in `output_files`.

    A schema the database doesn't have gets an empty file: an empty schema, as pgschema
    cannot dump a schema that doesn't exist.
    """
    present = existing_schemas(cfg, connection)
    for schema, output_file in output_files.items():
        if schema in present:
            pgschema_dump(schema, connection, output_file, verbose)
        else:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("")


def dump_text(cfg: Config, connection: Connection, verbose: bool = False) -> str:
    """The managed schemas of `connection` as one text, for snapshots and compare.

    Only `public`: the plain pgschema dump. Otherwise every schema's dump under `-- schema: <name>`.
    """
    with tempfile.TemporaryDirectory() as temporary_dir:
        dump_files = schema_files_in(cfg, Path(temporary_dir))
        dump_schemas(cfg, connection, dump_files, verbose)
        if not cfg.names_schemas:
            return dump_files["public"].read_text()
        parts = []
        for schema, dump_file in dump_files.items():
            parts.append(f"{SCHEMA_MARKER}{schema}\n{dump_file.read_text()}")
        return "\n".join(parts)


# where pgschema's readable plan starts repeating the whole SQL
DDL_HEADER = "DDL to be executed:"


@dataclass(frozen=True)
class Plan:
    """What pgschema would change, in two forms. Both are empty when there is nothing to change."""

    sql: str  # the statements, e.g. for a migration file
    human: str  # the same changes as a readable list for the terminal, followed by the SQL

    @property
    def changes(self) -> str:
        """`human` without the SQL: the summary and the list of changed objects."""
        return self.human.split(DDL_HEADER)[0].strip()

    @property
    def headline(self) -> str:
        """The size of the plan, e.g. "52 to add" from pgschema's first line "Plan: 52 to add."."""
        first_line = self.human.splitlines()[0] if self.human else ""
        return first_line.removeprefix("Plan:").strip().removesuffix(".")


def print_plans(plans: dict[str, Plan], verbose: bool) -> None:
    """The changed objects of every schema that changes, and with --verbose also the SQL."""
    for schema, plan in plans.items():
        if not plan.sql:
            continue
        if len(plans) > 1:
            print(f"schema {schema}:")
        print(plan.human if verbose else plan.changes)


def has_changes(plans: dict[str, Plan]) -> bool:
    """Whether any schema's plan changes something."""
    return any(plan.sql for plan in plans.values())


def plans_headline(plans: dict[str, Plan]) -> str:
    """The size of the plans, e.g. "1 to add", or with several schemas "public: 1 to add, billing: 2 to add"."""
    if len(plans) == 1:
        only_plan = next(iter(plans.values()))
        return only_plan.headline
    headlines = []
    for schema, plan in plans.items():
        if plan.sql:
            headlines.append(f"{schema}: {plan.headline}")
    return ", ".join(headlines)


def pgschema_plan(schema: str, connection: Connection, desired: Path, scratch: Connection) -> Plan:
    """The plan that turns `schema` on `connection` into the state from `desired`."""
    with tempfile.TemporaryDirectory() as temporary_dir:
        sql_file = Path(temporary_dir) / "plan.sql"
        human_file = Path(temporary_dir) / "plan.txt"
        command = [
            pgschema.locate(), "plan",
            "--schema", schema,
            "--file", str(desired),
            "--output-sql", str(sql_file),
            "--output-human", str(human_file),
            "--no-color",
            *plan_arguments(scratch),
        ]
        run(command, connection)
        # pgschema writes no file when there is nothing to change
        sql = sql_file.read_text() if sql_file.exists() else ""
        human = human_file.read_text() if human_file.exists() else ""
    return Plan(sql=sql.strip(), human=human.strip())


def plan_schemas(cfg: Config, scratch_database: Connection, desired_files: dict[str, Path],
                 scratch: Connection) -> dict[str, Plan]:
    """For every managed schema, the plan that turns it on `scratch_database` into its desired file."""
    load_plan_database(cfg, scratch, desired_files)
    plans = {}
    for schema, desired in desired_files.items():
        plans[schema] = pgschema_plan(schema, scratch_database, desired, scratch)
    return plans


def migration_sql(cfg: Config, plans: dict[str, Plan], new_schemas: list[str]) -> str:
    """The plans of every schema as the SQL of one migration, in the order of the config.

    pgschema writes a schema's own objects without the schema name, so each part starts with
    SET search_path; a schema no earlier migration has is created first.
    """
    if not cfg.names_schemas:
        return plans["public"].sql
    parts = []
    for schema, plan in plans.items():
        if not plan.sql:
            continue
        lines = [f"{SCHEMA_MARKER}{schema}"]
        if schema in new_schemas:
            lines.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
        lines.append(f"SET search_path TO {schema};\n")
        lines.append(plan.sql)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# Generating migrations


def is_valid_migration_name(name: str) -> bool:
    """Lowercase letters, digits and _ only, so the name is safe in a file name and in SQL."""
    allowed_characters = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    return name != "" and set(name) <= allowed_characters


def write_migration(cfg: Config, name: str, sql: str, note: str) -> Migration:
    if not is_valid_migration_name(name):
        sys.exit("migration name: lowercase letters, digits and _ only")
    existing = list_migrations(cfg)
    next_number = int(existing[-1].version) + 1 if existing else 1
    version = f"{next_number:04d}"
    path = cfg.migrations_dir / f"{version}_{name}.sql"
    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    header = (f"-- migration: {version}_{name}\n"
              f"-- generated: {generated_at} by postgresledger/pgschema\n"
              f"-- {note}\n\n")
    path.write_text(header + sql + "\n")
    return Migration(version, name, path)


def generate_migration(cfg: Config, scratch: Connection, name: str, note: str,
                       verbose: bool = False) -> Migration | None:
    """schema files vs (every migration applied) -> new migration, then check that it reproduces them.

    The plan is only printed with `verbose`: it is the content of the new file anyway.
    """
    for schema, desired in cfg.schema_files.items():
        if not desired.is_file():
            sys.exit(f"no {relative(desired)} - the desired state of schema {schema} "
                     "(an empty file is an empty schema)")
    existing = list_migrations(cfg)
    print(f"- building the state of {len(existing)} existing migrations (scratch Postgres)")
    shadow = build_shadow(cfg, scratch, existing)
    new_schemas = create_missing_schemas(cfg, shadow)
    plans = plan_schemas(cfg, shadow, cfg.schema_files, scratch)
    if not has_changes(plans):
        print("no changes compared to the existing migrations")
        return None
    migration = write_migration(cfg, name, migration_sql(cfg, plans, new_schemas), note)
    if verbose:
        print_plans(plans, verbose=True)
    print(f"- verifying {migration.path.name}")
    apply_file(cfg, shadow, migration)
    # with the new migration applied, nothing should be left to change
    leftover = plan_schemas(cfg, shadow, cfg.schema_files, scratch)
    if has_changes(leftover):
        # always in full: this is the case to debug
        print_plans(leftover, verbose=True)
        migration.path.unlink()
        sys.exit("after applying the migration the result still differs from the schema files - migration deleted")
    print(f"✔ created {relative(migration.path)} ({plans_headline(plans)})")
    return migration
