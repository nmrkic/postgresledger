"""Unit tests: no database needed."""

from __future__ import annotations

import tomllib

import pytest

from postgresledger import cli
from postgresledger.commands import database_name_from_folder
from postgresledger.compare import SchemaObject, compare, objects
from postgresledger.config import Config
from postgresledger.core import Plan, list_migrations
from postgresledger.data import DataSettings, compare_data

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


def test_init_leaves_grants_out_with_pgschemaignore(project):
    ignore = tomllib.loads((project / ".pgschemaignore").read_text())
    assert ignore["privileges"]["patterns"] == ["*"]
    assert ignore["default_privileges"]["patterns"] == ["*"]


def test_init_keeps_an_existing_pgschemaignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pgschemaignore").write_text('[tables]\npatterns = ["temp_*"]\n')
    run("init", "--no-setup")
    assert (tmp_path / ".pgschemaignore").read_text() == '[tables]\npatterns = ["temp_*"]\n'


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
    assert cfg.schema_files == {"public": project / "database" / "schema" / "schema.sql"}
    assert cfg.meta_table == "postgresledger.schema_migrations"
    assert not cfg.names_schemas


def write_project_settings(project, settings: str) -> None:
    (project / "postgresledger.toml").write_text(f"[project]\n{settings}\n")


def test_several_schemas_get_one_file_each(project):
    write_project_settings(project, 'schemas = ["public", "billing"]\nschema_dir = "db/schema"')
    cfg = Config(project / "postgresledger.toml")
    assert cfg.schemas == ["public", "billing"]
    assert cfg.schema_files["billing"] == project / "db" / "schema" / "billing.sql"
    assert cfg.names_schemas


@pytest.mark.parametrize("settings", [
    'schemas = ["public", "billing"]\nschema = "public"',  # both ways at once
    "schemas = []",
    'schemas = ["public", "public"]',
    'schemas = ["public", "Billing"]',  # would need quoting in SQL
    'schemas = ["public", "postgresledger"]',  # the tracking schema
])
def test_invalid_schemas_are_refused(project, settings):
    write_project_settings(project, settings)
    with pytest.raises(SystemExit):
        Config(project / "postgresledger.toml")


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


def test_objects_of_several_schemas_are_named_with_their_schema():
    # the same table name in two schemas is two objects, and no object runs into the next schema
    snapshot = f"-- schema: public\n{DUMP_A}\n-- schema: billing\n{DUMP_A}"
    found = objects(snapshot)
    assert set(found) == {SchemaObject("TABLE", "public.customer"), SchemaObject("INDEX", "public.customer_name_idx"),
                          SchemaObject("TABLE", "billing.customer"), SchemaObject("INDEX", "billing.customer_name_idx")}
    assert found[SchemaObject("INDEX", "public.customer_name_idx")] == "CREATE INDEX customer_name_idx ON customer (name);"


PLAN_HUMAN = """Plan: 2 to add.

Summary by type:
  tables: 2 to add

Tables:
  + customer
    + ix_customer_name (index)
  + orders

DDL to be executed:
--------------------------------------------------

CREATE TABLE IF NOT EXISTS customer (id integer);
"""


def test_plan_changes_leave_out_the_sql():
    plan = Plan(sql="CREATE TABLE ...", human=PLAN_HUMAN)
    assert plan.changes.endswith("  + orders")
    assert "CREATE TABLE" not in plan.changes and "DDL to be executed" not in plan.changes
    assert plan.headline == "2 to add"


def test_empty_plan():
    plan = Plan(sql="", human="")
    assert plan.changes == "" and plan.headline == ""


def test_verbose_only_on_commands_with_a_plan():
    main_parser = cli.parser()
    for command in ("baseline", "drift"):
        assert main_parser.parse_args([command, "--verbose"]).verbose
        assert not main_parser.parse_args([command]).verbose
    assert main_parser.parse_args(["new", "add_email", "-v"]).verbose
    assert main_parser.parse_args(["capture", "orders_notes", "-v"]).verbose
    assert main_parser.parse_args(["snapshot", "prod", "--verbose"]).verbose
    assert main_parser.parse_args(["compare", "prod", "-v"]).verbose
    with pytest.raises(SystemExit):
        main_parser.parse_args(["status", "--verbose"])


def test_ctrl_c_exits_without_a_traceback(monkeypatch, capsys):
    def interrupted_command(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_command", interrupted_command)
    assert run("snapshot", "prod") == 130
    assert capsys.readouterr().err.strip() == "interrupted"


def test_every_command_is_there():
    names = set(cli.parser()._subparsers._group_actions[0].choices)
    assert names == {"init", "setup", "baseline", "new", "apply", "status", "drift", "capture",
                     "snapshot", "compare"}

def test_reference_data_needs_registry_and_columns(project):
    write_project_settings(project, '[data]\ntable_columns = ["name"]')
    with pytest.raises(SystemExit):
        DataSettings.from_config(Config(project / "postgresledger.toml"))
    write_project_settings(project, '[data]\nregistry = "registry"\ntable_columns = ["name"]')
    with pytest.raises(SystemExit):
        DataSettings.from_config(Config(project / "postgresledger.toml"))


def test_reference_data_without_data_settings_is_off(project):
    assert DataSettings.from_config(Config(project / "postgresledger.toml")) is None


def test_compare_data_matches_rows_by_key_and_ignores_columns():
    first = {"tables": {"public.color": {"key": "id", "rows": [
        {"id": 1, "name": "red", "updated_at": "a"}, {"id": 2, "name": "green", "updated_at": "a"},
        {"id": 10, "name": "black", "updated_at": "a"}]}}, "missing": [], "unregistered": []}
    second = {"tables": {"public.color": {"key": "id", "rows": [
        {"id": 2, "name": "GREEN", "updated_at": "b"}, {"id": 10, "name": "black", "updated_at": "b"},
        {"id": 3, "name": "blue", "updated_at": "b"}]},
        "public.size": {"key": "id", "rows": []}}, "missing": ["public.shape"], "unregistered": []}
    comparison = compare_data(first, second, "dev", "prod", ["updated_at"])
    [color] = comparison.tables
    assert [row["id"] for row in color.only_in_first] == [1]
    assert [row["id"] for row in color.only_in_second] == [3]
    assert color.changed == [("2", {"name": "green"}, {"name": "GREEN"})]
    # 3 rows + a table only in prod + a table missing in prod only
    assert comparison.difference_count == 5
    assert "reference tables only in prod (1):\n  public.size" in comparison.report
    assert "registered but missing in prod only (1):\n  public.shape" in comparison.report


def test_compare_data_numeric_keys_sort_numerically():
    rows = [{"id": i} for i in (10, 9, 100)]
    first = {"tables": {"t": {"key": "id", "rows": rows}}, "missing": [], "unregistered": []}
    second = {"tables": {"t": {"key": "id", "rows": []}}, "missing": [], "unregistered": []}
    [table] = compare_data(first, second, "a", "b", []).tables
    assert [row["id"] for row in table.only_in_first] == [9, 10, 100]


def test_a_migration_that_fails_on_the_shadow_is_deleted(project, monkeypatch):
    from postgresledger import core

    cfg = Config(project / "postgresledger.toml")
    for desired in cfg.schema_files.values():
        desired.write_text("")
    monkeypatch.setattr(core, "build_shadow", lambda *args: None)
    monkeypatch.setattr(core, "create_missing_schemas", lambda *args: [])
    monkeypatch.setattr(core, "plan_schemas", lambda *args: {"public": Plan("ALTER TABLE x;", "Plan: 1 to modify.")})

    def fail(*args, **kwargs):
        raise SystemExit("psql failed")

    monkeypatch.setattr(core, "apply_file", fail)
    with pytest.raises(SystemExit):
        core.generate_migration(cfg, None, "broken", "")
    assert list_migrations(cfg) == []


def test_postgres_version_defaults_to_17_and_is_checked(project):
    assert Config(project / "postgresledger.toml").postgres_version == "17"
    write_project_settings(project, "postgres_version = 14")
    assert Config(project / "postgresledger.toml").postgres_version == "14"
    write_project_settings(project, 'postgres_version = "15"')
    with pytest.raises(SystemExit):
        Config(project / "postgresledger.toml")
