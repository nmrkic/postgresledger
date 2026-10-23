"""End-to-end: a new project from `pgl init` through baseline, new, apply,
drift, capture, snapshot and compare, against the local Postgres
(`make up`). Every step works on one database, chosen with DATABASE_*.

Uses its own databases (pgltest_*), dropped at the end; skipped when the
server, pgschema or the scratch Postgres is not there.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from test_cli import run

from postgresledger import pgschema, scratch

PORT = os.environ.get("PGL_TEST_PORT", "55432")
ENV = dict(os.environ, PGHOST="localhost", PGPORT=PORT, PGUSER="postgres", PGPASSWORD="postgres")
DBS = ["pgltest_dev", "pgltest_uat", "pgltest_prod"]


def sql(db: str, statement: str) -> str:
    res = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", statement],
                         env=ENV, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def server_up() -> bool:
    return subprocess.run(["psql", "-X", "-d", "postgres", "-c", "select 1"], env=ENV, capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(
    not server_up() or not pgschema.binary_path().exists() or not (scratch.install_dir() / "bin").exists(),
    reason="needs the local Postgres (make up) and `pgl setup`")


@pytest.fixture
def project(tmp_path, monkeypatch):
    for db in DBS:
        sql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        sql("postgres", f"CREATE DATABASE {db}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PGL_CONFIG", raising=False)
    assert run("init", "--no-setup") == 0
    yield tmp_path
    for db in DBS:
        sql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def use(monkeypatch, db: str) -> None:
    """Point pgl at one database, the way a different .env would."""
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", PORT)
    monkeypatch.setenv("DATABASE_USER", "postgres")
    monkeypatch.setenv("DATABASE_PASSWORD", "postgres")
    monkeypatch.setenv("DATABASE_NAME", db)


def databases() -> set[str]:
    return set(sql("postgres", "SELECT datname FROM pg_database").splitlines())


def test_full_flow(project, monkeypatch, capsys):
    schema = project / "database" / "schema" / "schema.sql"
    migrations = project / "database" / "migrations"
    before = databases()

    # an existing database becomes the baseline; prod is built from it
    sql("pgltest_dev", "CREATE TABLE customer (id int PRIMARY KEY, name text NOT NULL)")
    use(monkeypatch, "pgltest_dev")
    assert run("baseline") == 0
    assert (migrations / "0001_baseline.sql").exists()
    # the SQL is in the file; only --verbose prints it
    output = capsys.readouterr().out
    assert "CREATE TABLE" not in output and "0001_baseline.sql (1 to add)" in output
    assert sql("pgltest_dev", "SELECT mode FROM postgresledger.schema_migrations") == "baseline"
    use(monkeypatch, "pgltest_prod")
    assert run("apply") == 0
    assert sql("pgltest_prod", "SELECT mode FROM postgresledger.schema_migrations") == "applied"

    # uat already has the same schema: baseline records the migrations without running them
    sql("pgltest_uat", "CREATE TABLE customer (id int PRIMARY KEY, name text NOT NULL)")
    use(monkeypatch, "pgltest_uat")
    assert run("baseline") == 0
    assert sql("pgltest_uat", "SELECT mode FROM postgresledger.schema_migrations") == "baseline"

    # a change in schema.sql becomes 0002 and reaches every database
    schema.write_text(schema.read_text().replace("name text NOT NULL", "name text NOT NULL,\n    email text"))
    assert run("new", "add_email") == 0
    assert (migrations / "0002_add_email.sql").exists()
    for db in DBS:
        use(monkeypatch, db)
        assert run("apply") == 0
        assert run("drift") == 0
    assert sql("pgltest_prod", "SELECT count(*) FROM information_schema.columns "
                               "WHERE table_name = 'customer' AND column_name = 'email'") == "1"

    # a change made by hand on prod: found, compared with uat, captured, applied elsewhere
    sql("pgltest_prod", "CREATE INDEX customer_email_idx ON customer (email)")
    use(monkeypatch, "pgltest_prod")
    assert run("drift") == 1
    assert run("snapshot", "prod") == 0
    use(monkeypatch, "pgltest_uat")
    assert run("snapshot", "uat") == 0
    assert run("compare", "uat", "prod") == 1
    assert run("compare", "uat") == 0  # uat snapshot vs uat itself
    use(monkeypatch, "pgltest_prod")
    assert run("capture", "email_index") == 0
    assert sql("pgltest_prod", "SELECT mode FROM postgresledger.schema_migrations WHERE version = '0003'") == "captured"
    use(monkeypatch, "pgltest_uat")
    assert run("apply") == 0
    assert sql("pgltest_uat", "SELECT count(*) FROM pg_indexes WHERE indexname = 'customer_email_idx'") == "1"
    assert run("drift") == 0

    # a change made by hand on dev is undone by hand
    use(monkeypatch, "pgltest_dev")
    assert run("apply") == 0
    sql("pgltest_dev", "ALTER TABLE customer ADD COLUMN note text")
    assert run("drift") == 1
    sql("pgltest_dev", "ALTER TABLE customer DROP COLUMN note")
    assert run("drift") == 0

    # an applied migration that was edited is refused
    m = migrations / "0002_add_email.sql"
    m.write_text(m.read_text() + "\n-- edited\n")
    assert run("apply") != 0

    # nothing but the tracking schema was created on the server
    assert databases() == before


def test_baseline_refuses_a_database_that_differs(project, monkeypatch):
    sql("pgltest_dev", "CREATE TABLE customer (id int PRIMARY KEY)")
    use(monkeypatch, "pgltest_dev")
    assert run("baseline") == 0
    sql("pgltest_uat", "CREATE TABLE customer (id int PRIMARY KEY, extra text)")
    use(monkeypatch, "pgltest_uat")
    assert run("baseline") == 1
    assert sql("pgltest_uat", "SELECT count(*) FROM postgresledger.schema_migrations") == "0"
