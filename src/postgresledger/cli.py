"""pgl - versioned PostgreSQL migrations from plain SQL, without an ORM."""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, commands
from .config import CONFIG_NAME, Config, relative

# commands that run without a postgresledger.toml
NO_CONFIG = {"init", "setup"}
# commands that read the project but never touch a database
# (compare with two snapshots; compare with one checks DATABASE_NAME itself)
NO_DATABASE = {"new", "compare"}

INTRO = """\
Versioned PostgreSQL migrations from plain SQL, without an ORM.

Works on one database: DATABASE_* from .env (or the environment). schema.sql
is the desired state; every change to it becomes a numbered migration. The
database records which migrations it has, and changes made by hand (drift)
are found and can be captured as a migration. Scratch work happens in
a throwaway Postgres on this machine, deleted after each command."""

EXAMPLES = """\
examples:
  pgl init                   start in a new project
  pgl baseline               snapshot the existing database
  pgl new add_email          after editing schema.sql
  pgl apply                  bring the database up to date
  pgl drift                  was the database changed by hand?
  pgl compare uat prod       how do two saved snapshots differ?

`pgl <command> -h` shows the details of one command.
postgresledger.toml is read from the current folder (or --config)."""


def add_init_arguments(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("--dir", default="database",
                                help="folder for schema/ and migrations/ (default: database)")
    command_parser.add_argument("--no-setup", action="store_true",
                                help="don't download pgschema and the scratch Postgres")


def add_name_argument(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("name", help="lowercase letters, digits and _")


def add_new_arguments(command_parser: argparse.ArgumentParser) -> None:
    add_name_argument(command_parser)
    command_parser.add_argument("--note", help="comment written into the migration header")


def add_apply_arguments(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("--dry-run", action="store_true", help="only list what would be applied")


def add_snapshot_arguments(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("name", help="label for the database, e.g. prod")


def add_compare_arguments(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("a", help="snapshot name or file")
    command_parser.add_argument("b", nargs="?", help="snapshot name or file (default: the database)")


@dataclass(frozen=True)
class Command:
    """One `pgl` command as the help shows it. It runs commands.cmd_<name>."""

    group: str  # a key of GROUPS
    name: str
    usage: str  # the arguments shown in the overview
    summary: str
    details: str
    add_arguments: Callable[[argparse.ArgumentParser], None] | None = None


COMMANDS = [
    Command(
        "setup", "init", "[--dir D]", "create postgresledger.toml, .env and folders",
        "Creates postgresledger.toml (project settings, committed) and .env with DATABASE_* (never committed, "
        "added to .gitignore) in the current folder, plus <dir>/schema/ and <dir>/migrations/, then runs setup.",
        add_init_arguments),
    Command(
        "setup", "setup", "", "download pgschema + scratch Postgres, check psql",
        "Downloads the pinned pgschema and a pinned Postgres server into ~/.cache/postgresledger/ (once per "
        "machine, SHA256 checked) and checks that psql is installed. The Postgres server is only ever "
        "started as a throwaway scratch database on this machine."),

    Command(
        "migrations", "baseline", "", "snapshot the database as 0001_baseline",
        "No migrations yet: dumps the database into schema.sql and creates 0001_baseline.sql from it, "
        "recorded as 'baseline' (not executed). Migrations exist: an existing database that already has "
        "exactly their schema gets them recorded as 'baseline' instead of applying them; if it differs, "
        "the differences are listed and nothing is recorded."),
    Command(
        "migrations", "new", "<name> [--note T]", "new migration from the changes in schema.sql",
        "Builds the state of every existing migration in a scratch Postgres, compares it with schema.sql "
        "and writes the difference as NNNN_<name>.sql. The file is checked there and deleted if it does "
        "not reproduce schema.sql. Nothing is written when there is no change. The database is not touched.",
        add_new_arguments),
    Command(
        "migrations", "apply", "[--dry-run]", "apply pending migrations",
        "Runs every migration the database does not have yet, in order, each in one transaction together "
        "with its record. Refuses to run when an already applied file was edited.",
        add_apply_arguments),
    Command(
        "migrations", "status", "", "which migrations the database has",
        "Every migration with its state on the database: applied, baseline, captured or PENDING."),

    Command(
        "drift", "drift", "", "list manual changes (exit 1 if any)",
        "Compares the database with the state its migrations describe (built in a scratch Postgres) and "
        "lists every difference. The database must be up to date first (apply)."),
    Command(
        "drift", "capture", "<name>", "keep manual changes as a new migration",
        "Takes the manual changes into schema.sql and creates NNNN_<name>.sql from them. It is recorded "
        "as 'captured' on this database (not executed) and reaches the other databases with apply.",
        add_name_argument),

    Command(
        "compare", "snapshot", "<name>", "save the database schema as snapshots/<name>.sql",
        "Dumps the schema of the database into <snapshots_dir>/<name>.sql, e.g. `pgl snapshot prod` with "
        "the prod .env. Commit it to compare databases later.",
        add_snapshot_arguments),
    Command(
        "compare", "compare", "<a> [b]", "differences between two snapshots",
        "Compares two snapshots (names from snapshots_dir or file paths) object by object: only in a, "
        "only in b, different. Without b, a is compared with the database itself. Exit 1 on differences.",
        add_compare_arguments),
]

GROUPS = {
    "setup": "setup",
    "migrations": "migrations",
    "drift": "drift (changes made by hand on the database)",
    "compare": "comparing databases",
}


def overview() -> str:
    """Every command with its summary, grouped, for the main help."""
    width = max(len(f"{command.name} {command.usage}".strip()) for command in COMMANDS) + 2
    lines = []
    for group, title in GROUPS.items():
        lines.append(f"{title}:")
        for command in COMMANDS:
            if command.group == group:
                name_and_usage = f"{command.name} {command.usage}".strip()
                lines.append(f"  {name_and_usage:<{width}}{command.summary}")
        lines.append("")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(
        prog="pgl", usage="pgl [--env-file PATH] <command> [arguments]",
        description=f"{INTRO}\n\n{overview()}\n{EXAMPLES}",
        formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)
    options = main_parser.add_argument_group("options")
    options.add_argument("-h", "--help", action="help", help="show this help and exit")
    options.add_argument("--version", action="version", version=f"postgresledger {__version__}")
    options.add_argument("--config", metavar="PATH",
                         help="postgresledger.toml to use (default: the one in the current folder)")
    options.add_argument("--env-file", metavar="PATH",
                         help="read DATABASE_* from this file (default: .env next to postgresledger.toml)")
    subparsers = main_parser.add_subparsers(dest="cmd", required=True, metavar="<command>", help=argparse.SUPPRESS)
    for command in COMMANDS:
        command_parser = subparsers.add_parser(
            command.name, help=command.summary,
            description=f"{command.summary}.\n\n{textwrap.fill(command.details, 78)}",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        command_function = getattr(commands, "cmd_" + command.name.replace("-", "_"))
        command_parser.set_defaults(fn=command_function)
        if command.add_arguments is not None:
            command.add_arguments(command_parser)
    return main_parser


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.cmd in NO_CONFIG:
        sys.exit(args.fn(args) or 0)

    config_file = Path(args.config) if args.config else Path(CONFIG_NAME)
    env_file = Path(args.env_file) if args.env_file else None
    cfg = Config(config_file, env_file)
    if args.cmd in NO_DATABASE:
        sys.exit(args.fn(cfg, args) or 0)

    # every other command works on the one database: checked here, once
    if cfg.target is None:
        sys.exit(f"no DATABASE_NAME - set it in {relative(cfg.env_file)} or in the environment "
                 "(with DATABASE_HOST, DATABASE_PORT, DATABASE_USER, DATABASE_PASSWORD)")
    sys.exit(args.fn(cfg, cfg.target, args) or 0)


if __name__ == "__main__":
    main()
