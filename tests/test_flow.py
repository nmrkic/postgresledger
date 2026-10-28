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
READER_ROLE = "pgltest_reader"


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
    # roles belong to the whole server: one for the grants test, dropped again below
    sql("postgres", f"DROP ROLE IF EXISTS {READER_ROLE}")
    sql("postgres", f"CREATE ROLE {READER_ROLE}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PGL_CONFIG", raising=False)
    assert run("init", "--no-setup") == 0
    yield tmp_path
    for db in DBS:
        sql("postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    sql("postgres", f"DROP ROLE IF EXISTS {READER_ROLE}")


def use(monkeypatch, db: str) -> None:
    """Point pgl at one database, the way a different .env would."""
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", PORT)
    monkeypatch.setenv("DATABASE_USER", "postgres")
    monkeypatch.setenv("DATABASE_PASSWORD", "postgres")
    monkeypatch.setenv("DATABASE_NAME", db)


def databases() -> set[str]:
    return set(sql("postgres", "SELECT datname FROM pg_database").splitlines())


def test_full_flow(project, monkeypatch, capfd):
    schema = project / "database" / "schema" / "schema.sql"
    migrations = project / "database" / "migrations"
    before = databases()

    # an existing database becomes the baseline; prod is built from it
    sql("pgltest_dev", "CREATE TABLE customer (id int PRIMARY KEY, name text NOT NULL)")
    sql("pgltest_dev", f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {READER_ROLE}")
    use(monkeypatch, "pgltest_dev")
    assert run("baseline") == 0
    assert (migrations / "0001_baseline.sql").exists()
    # the .pgschemaignore from `pgl init` keeps grants out of the migrations
    assert READER_ROLE not in (migrations / "0001_baseline.sql").read_text()
    assert READER_ROLE not in schema.read_text()
    # the SQL is in the file; only --verbose prints it
    output = capfd.readouterr().out
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
    capfd.readouterr()
    assert run("snapshot", "uat") == 0
    quiet = capfd.readouterr()
    assert "uat.sql written in" in quiet.out and "level=DEBUG" not in quiet.err
    # --verbose: pgschema's debug log, shown while it runs (it is a separate process, hence capfd)
    assert run("snapshot", "uat", "--verbose") == 0
    assert "Database connection established" in capfd.readouterr().err
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


TWO_SCHEMAS = """
    CREATE TABLE public.customer (id int PRIMARY KEY, name text NOT NULL);
    CREATE SCHEMA billing;
    CREATE TABLE billing.invoice (id int PRIMARY KEY, customer_id int NOT NULL REFERENCES public.customer (id));
    CREATE VIEW billing.invoice_count AS
        SELECT c.name, count(i.id) AS invoices FROM public.customer c JOIN billing.invoice i ON i.customer_id = c.id
        GROUP BY c.name;
"""


def test_flow_with_two_schemas(project, monkeypatch):
    (project / "postgresledger.toml").write_text('[project]\nschemas = ["public", "billing"]\n')
    schema_dir = project / "database" / "schema"
    migrations = project / "database" / "migrations"

    # dev has both schemas, billing refers to public: one baseline with a part per schema
    sql("pgltest_dev", TWO_SCHEMAS)
    use(monkeypatch, "pgltest_dev")
    assert run("baseline") == 0
    baseline = (migrations / "0001_baseline.sql").read_text()
    assert "-- schema: public\nSET search_path TO public;" in baseline
    assert "-- schema: billing\nCREATE SCHEMA IF NOT EXISTS billing;\nSET search_path TO billing;" in baseline
    assert "CREATE TABLE IF NOT EXISTS invoice" in (schema_dir / "billing.sql").read_text()

    # an empty database gets every object in its own schema
    use(monkeypatch, "pgltest_prod")
    assert run("apply") == 0
    assert sql("pgltest_prod", "SELECT table_schema FROM information_schema.tables "
                               "WHERE table_name = 'invoice'") == "billing"
    assert run("drift") == 0

    # a database that already has both schemas is adopted
    sql("pgltest_uat", TWO_SCHEMAS)
    use(monkeypatch, "pgltest_uat")
    assert run("baseline") == 0

    # a change in billing.sql only: the migration has only the billing part
    billing_file = schema_dir / "billing.sql"
    billing_file.write_text(billing_file.read_text().replace("customer_id integer NOT NULL,",
                                                             "customer_id integer NOT NULL,\n    total numeric,"))
    assert run("new", "invoice_total") == 0
    new_migration = (migrations / "0002_invoice_total.sql").read_text()
    assert "-- schema: billing\nSET search_path TO billing;" in new_migration
    assert "-- schema: public" not in new_migration
    for db in DBS[1:]:
        use(monkeypatch, db)
        assert run("apply") == 0
        assert run("drift") == 0

    # a change by hand in billing: drift, captured into billing.sql, applied elsewhere
    sql("pgltest_prod", "CREATE INDEX invoice_customer_idx ON billing.invoice (customer_id)")
    use(monkeypatch, "pgltest_prod")
    assert run("drift") == 1
    assert run("snapshot", "prod") == 0
    assert run("compare", "prod") == 0
    assert run("capture", "invoice_index") == 0
    assert "invoice_customer_idx" in billing_file.read_text()
    use(monkeypatch, "pgltest_uat")
    assert run("compare", "prod") == 1
    assert run("apply") == 0
    assert run("drift") == 0
    assert sql("pgltest_uat", "SELECT schemaname FROM pg_indexes WHERE indexname = 'invoice_customer_idx'") == "billing"


def test_baseline_refuses_a_database_that_differs(project, monkeypatch):
    sql("pgltest_dev", "CREATE TABLE customer (id int PRIMARY KEY)")
    use(monkeypatch, "pgltest_dev")
    assert run("baseline") == 0
    sql("pgltest_uat", "CREATE TABLE customer (id int PRIMARY KEY, extra text)")
    use(monkeypatch, "pgltest_uat")
    assert run("baseline") == 1
    assert sql("pgltest_uat", "SELECT count(*) FROM postgresledger.schema_migrations") == "0"
