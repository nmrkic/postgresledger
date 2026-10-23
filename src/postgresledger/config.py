"""Settings: postgresledger.toml (the project, the same for every database) and
the one database this run works on, from DATABASE_* variables (.env or the environment)."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CONFIG_NAME = "postgresledger.toml"
DEFAULT_META_SCHEMA = "postgresledger"


@dataclass(frozen=True)
class Connection:
    """How to reach one database. Another database on the same server:
    dataclasses.replace(connection, database="other")."""

    host: str
    port: int
    user: str
    password: str
    database: str
    sslmode: str = "prefer"

    def environment(self) -> dict[str, str]:
        """The PG* variables psql and pgschema read to connect to this database.

        All of them are always set, so a value left in the shell never applies to this database.
        """
        return {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
            "PGDATABASE": self.database,
            "PGSSLMODE": self.sslmode,
        }

    @property
    def label(self) -> str:
        return f"{self.database}@{self.host}:{self.port}"


class Config:
    def __init__(self, path: Path = Path(CONFIG_NAME), env_file: Path | None = None):
        if not path.is_file():
            sys.exit(f"no {path} here - run `pgl init` first")
        self.path = path.resolve()
        self.root = self.path.parent  # paths in the config file are relative to it
        project = tomllib.loads(self.path.read_text()).get("project", {})
        self.schema = project.get("schema", "public")  # the schema pgschema manages
        self.schema_file = self.root / project.get("schema_file", "database/schema/schema.sql")
        self.migrations_dir = self.root / project.get("migrations_dir", "database/migrations")
        self.snapshots_dir = self.root / project.get("snapshots_dir", "database/snapshots")
        self.meta_schema = project.get("meta_schema", DEFAULT_META_SCHEMA)
        self.meta_table = f"{self.meta_schema}.schema_migrations"
        self.env_file = env_file or Path(os.environ.get("PGL_ENV_FILE") or self.root / ".env")

        # the one database this run works on; None when DATABASE_NAME is not set.
        # Each DATABASE_* variable comes from the environment if set there, else from .env
        # (load_dotenv never overrides a variable that is already set).
        load_dotenv(self.env_file)
        self.target: Connection | None = None
        if database := os.environ.get("DATABASE_NAME"):
            self.target = Connection(
                host=os.environ.get("DATABASE_HOST", "localhost"),
                port=int(os.environ.get("DATABASE_PORT", "5432")),
                user=os.environ.get("DATABASE_USER", "postgres"),
                password=os.environ.get("DATABASE_PASSWORD", ""),
                database=database,
                sslmode=os.environ.get("DATABASE_SSLMODE", "prefer"),
            )


def relative(path: Path) -> str:
    """The path as the user would type it from the current folder."""
    return os.path.relpath(path, Path.cwd())
