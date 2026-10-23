"""Unit tests: no database needed."""

from __future__ import annotations

import tomllib

import pytest

from postgresledger import cli
from postgresledger.commands import database_name_from_folder
from postgresledger.compare import SchemaObject, compare, objects
from postgresledger.config import Config
from postgresledger.core import list_migrations

DATABASE_VARIABLES = ["DATABASE_HOST", "DATABASE_PORT", "DATABASE_USER", "DATABASE_PASSWORD",
                      "DATABASE_NAME", "DATABASE_SSLMODE", "PGL_ENV_FILE"]


def run(*argv: str) -> int:
    try:
        cli.main(list(argv))
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    return 0


@pytest.fixture(autouse=True)
def no_database_variables(monkeypatch):
    """Every test starts without DATABASE_*. Config loads .env into os.environ,
    so setenv first: monkeypatch then also removes what a test loaded."""
    for name in DATABASE_VARIABLES:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("init", "--no-setup") == 0
    return tmp_path


def test_init_creates_config_env_and_folders(project):
    cfg = tomllib.loads((project / "postgresledger.toml").read_text())
    assert cfg["project"]["migrations_dir"] == "database/migrations"
    assert "envs" not in cfg and "connection" not in cfg
    assert f"DATABASE_NAME={database_name_from_folder(project.name)}\n" in (project / ".env").read_text()
    assert ".env" in (project / ".gitignore").read_text().splitlines()
    assert (project / "database" / "schema").is_dir()
    assert (project / "database" / "migrations" / ".gitkeep").exists()


def test_init_env_is_read_by_config(project):
    target = Config(project / "postgresledger.toml").target
    assert (target.host, target.port, target.user, target.password) == ("localhost", 5432, "postgres", "postgres")


def test_init_keeps_existing_env_and_gitignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATABASE_NAME=mine\n")
    (tmp_path / ".gitignore").write_text("node_modules/")
    run("init", "--no-setup")
    assert (tmp_path / ".env").read_text() == "DATABASE_NAME=mine\n"
    assert (tmp_path / ".gitignore").read_text() == "node_modules/\n.env\n"


def test_init_refuses_to_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "postgresledger.toml").write_text("x")
    assert run("init", "--no-setup") != 0
    assert (tmp_path / "postgresledger.toml").read_text() == "x"


def test_config_must_be_in_the_current_folder(project, monkeypatch):
    monkeypatch.chdir(project / "database" / "migrations")
    assert run("status") != 0


def test_paths_are_relative_to_the_config(project):
    cfg = Config(project / "postgresledger.toml")
    assert cfg.schema_file == project / "database" / "schema" / "schema.sql"
    assert cfg.meta_table == "postgresledger.schema_migrations"


def test_database_from_env_file_and_environment(project, monkeypatch):
    (project / ".env").write_text(
        "# comment\nexport DATABASE_HOST=db.internal\nDATABASE_PORT=6543\nDATABASE_USER=app\n"
        'DATABASE_PASSWORD="s@cret"\nDATABASE_NAME=shop\nDATABASE_SSLMODE=require\n')
    target = Config(project / "postgresledger.toml").target
    assert (target.host, target.port, target.user, target.password, target.database) == (
        "db.internal", 6543, "app", "s@cret", "shop")
    assert target.environment()["PGSSLMODE"] == "require"
    monkeypatch.setenv("DATABASE_NAME", "other")
    assert Config(project / "postgresledger.toml").target.database == "other"  # the environment wins over .env


def test_defaults_when_only_the_name_is_set(project):
    (project / ".env").write_text("DATABASE_NAME=shop\n")
    target = Config(project / "postgresledger.toml").target
    assert (target.host, target.port, target.user, target.password, target.sslmode) == (
        "localhost", 5432, "postgres", "", "prefer")


def test_env_file_option(project):
    (project / ".env.prod").write_text("DATABASE_HOST=prod-host\nDATABASE_NAME=prod\n")
    assert Config(project / "postgresledger.toml", project / ".env.prod").target.host == "prod-host"


def test_missing_database_name(project):
    (project / ".env").write_text("")
    assert Config(project / "postgresledger.toml").target is None
    assert run("status") != 0


def test_duplicate_versions_are_rejected(project):
    migs = project / "database" / "migrations"
    (migs / "0001_a.sql").write_text("")
    (migs / "0001_b.sql").write_text("")
    with pytest.raises(SystemExit):
        list_migrations(Config(project / "postgresledger.toml"))


DUMP_A = """--
-- pgschema database dump
--

--
-- Name: customer; Type: TABLE; Schema: -; Owner: -
--

CREATE TABLE customer (
    id integer,
    name text
);

--
-- Name: customer_name_idx; Type: INDEX; Schema: -; Owner: -
--

CREATE INDEX customer_name_idx ON customer (name);
"""
DUMP_B = DUMP_A.replace("    name text\n", "    name text,\n    email text\n").split("--\n-- Name: customer_name_idx")[0] + """--
-- Name: orders; Type: TABLE; Schema: -; Owner: -
--

CREATE TABLE orders (id integer);
"""


def test_objects_split_a_dump():
    customer_table = SchemaObject("TABLE", "customer")
    name_index = SchemaObject("INDEX", "customer_name_idx")
    assert set(objects(DUMP_A)) == {customer_table, name_index}
    assert objects(DUMP_A)[name_index] == "CREATE INDEX customer_name_idx ON customer (name);"


def test_compare_reports_each_kind_of_difference():
    comparison = compare(DUMP_A, DUMP_B, "uat", "prod")
    report = comparison.report
    assert comparison.difference_count == 3
    assert "only in uat (1):" in report and "customer_name_idx" in report
    assert "only in prod (1):" in report and "orders" in report
    assert "different (1):" in report and "+    email text" in report
    assert compare(DUMP_A, DUMP_A, "a", "b").difference_count == 0


def test_every_command_is_there():
    names = set(cli.parser()._subparsers._group_actions[0].choices)
    assert names == {"init", "setup", "baseline", "new", "apply", "status", "drift", "capture",
                     "snapshot", "compare"}
