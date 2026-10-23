"""One function per `pgl` command. Every command works on one database: the DATABASE_* variables."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from . import pgschema, scratch
from .compare import compare
from .config import CONFIG_NAME, Config, Connection, relative
from .core import (
    applied,
    apply_file,
    build_shadow,
    check_checksums,
    ensure_meta,
    generate_migration,
    list_migrations,
    pgschema_dump,
    pgschema_plan,
    psql,
    record_migration_sql,
    require_up_to_date,
)


def config_file_text(folder: str) -> str:
    """The postgresledger.toml `pgl init` writes, with every path inside `folder`."""
    return f"""\
# postgresledger project settings - the same for every database, committed to git.
# Paths are relative to this file. The database itself is DATABASE_* in .env
# (or in the environment), one database per run.

[project]
schema = "public"                                # schema managed by postgresledger
schema_file = "{folder}/schema/schema.sql"          # desired state, the source of truth
migrations_dir = "{folder}/migrations"
snapshots_dir = "{folder}/snapshots"                # pgl snapshot <name> writes here
meta_schema = "postgresledger"                         # tracking table: <meta_schema>.schema_migrations
"""


def dotenv_file_text(database: str) -> str:
    """The .env `pgl init` writes: a local database called `database`."""
    return f"""\
# The database pgl works on. Never commit this file.
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres
DATABASE_NAME={database}
"""


def database_name_from_folder(folder_name: str) -> str:
    """The project folder name as a database name: lowercase letters, digits and _ ("My-App" -> "my_app")."""
    allowed_characters = "abcdefghijklmnopqrstuvwxyz0123456789_"
    database = ""
    for character in folder_name.lower():
        if character in allowed_characters:
            database += character
        elif not database.endswith("_"):
            # a run of other characters becomes one _
            database += "_"
    return database.strip("_") or "app"


def cmd_init(args) -> int:
    root = Path.cwd()
    config_file = root / CONFIG_NAME
    if config_file.exists():
        sys.exit(f"{CONFIG_NAME} already exists here")
    for subfolder in ("schema", "migrations"):
        (root / args.dir / subfolder).mkdir(parents=True, exist_ok=True)
    (root / args.dir / "migrations" / ".gitkeep").touch()
    config_file.write_text(config_file_text(args.dir))
    created = [CONFIG_NAME, f"{args.dir}/schema/", f"{args.dir}/migrations/"]

    dotenv_file = root / ".env"
    if not dotenv_file.exists():
        dotenv_file.write_text(dotenv_file_text(database_name_from_folder(root.name)))
        created.append(".env")

    gitignore = root / ".gitignore"
    ignored = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".env" not in ignored:
        # start on a new line when the file does not end with one
        needs_newline = bool(ignored) and ignored[-1] != ""
        with gitignore.open("a") as gitignore_file:
            gitignore_file.write(("\n" if needs_newline else "") + ".env\n")
        created.append(".gitignore (.env)")

    print(f"✔ created {', '.join(created)}")
    exit_code = 0 if args.no_setup else pgschema.setup()
    print(f"""
next:
  1. set the DATABASE_* variables in .env to your database
  2. pgl baseline        snapshot it as the first migration
     or write {args.dir}/schema/schema.sql and run `pgl new initial`""")
    return exit_code


def cmd_setup(args) -> int:
    return pgschema.setup()


def cmd_baseline(cfg: Config, connection: Connection, args) -> int:
    migrations = list_migrations(cfg)
    with scratch.server() as scratch_server:
        if not migrations:
            cfg.schema_file.parent.mkdir(parents=True, exist_ok=True)
            print(f"- dump {connection.label} -> {relative(cfg.schema_file)}")
            pgschema_dump(cfg, connection, cfg.schema_file)
            baseline = generate_migration(cfg, scratch_server, "baseline", f"baseline taken from {connection.database}")
            if baseline is None:
                sys.exit("the database is empty, nothing to snapshot")
            ensure_meta(cfg, connection)
            psql(connection, record_migration_sql(cfg, baseline, "baseline"))
            print(f"✔ {baseline.id} recorded on {connection.label} (baseline, not executed)")
            return 0
        # migrations exist: adopt a database that already has their schema
        if applied(cfg, connection):
            sys.exit(f"{connection.label} already tracks migrations - use `pgl apply`")
        print(f"- building the state of {len(migrations)} migrations (scratch Postgres)")
        shadow = build_shadow(cfg, scratch_server, migrations)
        with tempfile.TemporaryDirectory() as temporary_dir:
            actual = Path(temporary_dir) / "actual.sql"
            pgschema_dump(cfg, connection, actual)
            plan = pgschema_plan(cfg, shadow, actual, scratch_server)
    if plan.sql:
        print(f"✘ {connection.label} does not match the migrations, so they cannot be recorded as already there:\n")
        print(plan.human)
        print("\nempty database: `pgl apply` instead; otherwise align it first (see `pgl compare`)")
        return 1
    # every record in one transaction: all of them or none
    records = [record_migration_sql(cfg, migration, "baseline") for migration in migrations]
    psql(connection, "BEGIN;\n" + "\n".join(records) + "\nCOMMIT;")
    print(f"✔ {connection.label} matches the migrations: {len(migrations)} recorded as baseline (not executed)")
    return 0


def cmd_new(cfg: Config, args) -> None:
    with scratch.server() as scratch_server:
        generate_migration(cfg, scratch_server, args.name, args.note or "change in schema.sql")


def cmd_apply(cfg: Config, connection: Connection, args) -> None:
    applied_by_version = applied(cfg, connection)
    migrations = list_migrations(cfg)
    check_checksums(connection, migrations, applied_by_version)
    pending = [migration for migration in migrations if migration.version not in applied_by_version]
    if not pending:
        print(f"{connection.label}: all migrations are already applied")
        return
    for migration in pending:
        if args.dry_run:
            print(f"[dry-run] {connection.label}: would apply {migration.id}")
            continue
        print(f"- {connection.label}: applying {migration.id}")
        apply_file(cfg, connection, migration)
    if not args.dry_run:
        print(f"✔ {connection.label}: applied {len(pending)} migration(s)")


def cmd_status(cfg: Config, connection: Connection, args) -> None:
    migrations = list_migrations(cfg)
    applied_by_version = applied(cfg, connection)
    width = max([len(migration.id) for migration in migrations] + [9])
    print(f"{connection.label}\n")
    print(f"{'migration':<{width}}  {'state':<9}  applied at")
    pending_count = 0
    for migration in migrations:
        applied_migration = applied_by_version.get(migration.version)
        if applied_migration is None:
            pending_count += 1
            print(f"{migration.id:<{width}}  {'PENDING':<9}")
        else:
            print(f"{migration.id:<{width}}  {applied_migration.mode:<9}  {applied_migration.applied_at}")
    summary = f"\n{len(migrations) - pending_count} of {len(migrations)} applied"
    if pending_count:
        summary += f", {pending_count} pending - `pgl apply`"
    print(summary)


def cmd_drift(cfg: Config, connection: Connection, args) -> int:
    """Changes made by hand: the database compared with the state of the migrations it has.

    Migrations the database doesn't have yet are reported, not counted as drift.
    """
    applied_migrations = applied(cfg, connection)
    all_migrations = list_migrations(cfg)
    known_versions = {migration.version for migration in all_migrations}

    unknown = [record for record in applied_migrations.values() if record.version not in known_versions]
    if unknown:
        names = ", ".join(f"{record.version}_{record.name}" for record in unknown)
        sys.exit(f"{connection.label} has migrations that are not in the repository ({names}) - "
                 "its expected state cannot be built")

    has = [migration for migration in all_migrations if migration.version in applied_migrations]
    pending = [migration for migration in all_migrations if migration.version not in applied_migrations]
    if pending:
        names = ", ".join(migration.id for migration in pending)
        print(f"! {connection.label} does not have {len(pending)} migration(s) yet: {names} - `pgl apply`")
        print(f"  comparing with the {len(has)} it has, so these are not counted as drift")

    with scratch.server() as scratch_server:
        print(f"- building the state of the {len(has)} migrations {connection.database} has (scratch Postgres)")
        shadow = build_shadow(cfg, scratch_server, has)
        with tempfile.TemporaryDirectory() as tmp_dir:
            actual = Path(tmp_dir) / "actual.sql"
            pgschema_dump(cfg, connection, actual)
            # "what turns the expected state into the actual database" = the manual changes
            plan = pgschema_plan(cfg, shadow, actual, scratch_server)
    if not plan.sql:
        print(f"✔ {connection.label}: no drift, the database matches its migrations")
        return 0
    print(f"✘ {connection.label}: DRIFT - the database was changed outside of migrations:\n")
    print(plan.human)
    if pending:
        print("\nrun `pgl apply` first, then keep or drop the changes:")
    print("\nkeep the changes:   pgl capture <name>")
    print("drop the changes:   undo them by hand (or with a new migration), then `pgl drift` again")
    return 1


def cmd_capture(cfg: Config, connection: Connection, args) -> None:
    require_up_to_date(cfg, connection)
    print(f"- dump {connection.label} -> {relative(cfg.schema_file)} (manual changes become the desired state)")
    # put back when there is nothing to capture
    schema_before = cfg.schema_file.read_text()
    pgschema_dump(cfg, connection, cfg.schema_file)
    note = f"manual changes captured from {connection.database} (recorded as captured there)"
    with scratch.server() as scratch_server:
        migration = generate_migration(cfg, scratch_server, args.name, note)
    if migration is None:
        cfg.schema_file.write_text(schema_before)
        print(f"{connection.label}: no drift, nothing captured")
        return
    psql(connection, record_migration_sql(cfg, migration, "captured"))
    print(f"✔ {migration.id} recorded on {connection.label} (captured, not executed) - "
          "other databases get it with `pgl apply`")


def is_valid_snapshot_name(name: str) -> bool:
    """Letters, digits, _ . and - only, so the name is safe as a file name."""
    allowed_characters = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    return name != "" and set(name) <= allowed_characters


def cmd_snapshot(cfg: Config, connection: Connection, args) -> None:
    if not is_valid_snapshot_name(args.name):
        sys.exit("snapshot name: letters, digits, _ . - only")
    snapshot = cfg.snapshots_dir / f"{args.name}.sql"
    pgschema_dump(cfg, connection, snapshot)
    print(f"✔ {connection.label} -> {relative(snapshot)}")


def snapshot_file(cfg: Config, name_or_path: str) -> Path:
    """A snapshot given as a file path, or by name from snapshots_dir."""
    for candidate in (Path(name_or_path), cfg.snapshots_dir / f"{name_or_path}.sql"):
        if candidate.is_file():
            return candidate
    sys.exit(f"no snapshot '{name_or_path}' (neither a file nor {relative(cfg.snapshots_dir)}/{name_or_path}.sql) - "
             f"`pgl snapshot {name_or_path}`")


def cmd_compare(cfg: Config, args) -> int:
    first_dump = snapshot_file(cfg, args.a).read_text()
    if args.b:
        second_dump = snapshot_file(cfg, args.b).read_text()
        second_label = args.b
    else:
        # no second snapshot: compare with the database itself, dumped right now
        if cfg.target is None:
            sys.exit("comparing with the database needs DATABASE_NAME in .env - or give two snapshots")
        with tempfile.TemporaryDirectory() as temporary_dir:
            live_dump_file = Path(temporary_dir) / "live.sql"
            pgschema_dump(cfg, cfg.target, live_dump_file)
            second_dump = live_dump_file.read_text()
        second_label = cfg.target.label
    comparison = compare(first_dump, second_dump, args.a, second_label)
    print(comparison.report)
    return 1 if comparison.difference_count else 0
