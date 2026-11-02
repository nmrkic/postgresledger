"""Reference data (lookup tables): read from a database and compared row by row.

Which tables count is decided by the database itself: a registry table lists
them, one table name per row and column (`[data] registry` and
`table_columns`), so the set matches what the application treats as reference
data. The registry rows are compared too. Rows are matched by primary key.

A snapshot of the data is JSON next to the schema snapshot:
{"tables": {"<schema>.<table>": {"key": "<column>", "rows": [{...}, ...]}},
 "missing": [names the registry lists that the database doesn't have],
 "unregistered": [tables named like reference data that the registry doesn't list]}
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from .config import CONFIG_NAME, Config, Connection
from .core import psql
from .decisions import Decisions, accepted

# the most rows printed per table and kind of difference; the counts are always complete
ROWS_SHOWN = 10


@dataclass(frozen=True)
class DataSettings:
    """`[data]` in postgresledger.toml."""

    registry: str  # <schema>.<table> that lists the reference tables
    table_columns: list[str]  # the registry's columns that hold a table name
    search_path: list[str]  # how the application resolves names without a schema
    ignore_columns: list[str] = field(default_factory=list)  # e.g. updated_at, in every table
    exclude: list[str] = field(default_factory=list)  # <schema>.<table> never compared
    name_suffix: str = ""  # tables ending in it but missing from the registry are listed

    @staticmethod
    def from_config(cfg: Config) -> DataSettings | None:
        settings = cfg.data_settings
        if not settings:
            return None
        for required in ("registry", "table_columns"):
            if required not in settings:
                sys.exit(f"{CONFIG_NAME}: [data] needs `{required}`")
        if "." not in settings["registry"]:
            sys.exit(f"{CONFIG_NAME}: [data] registry must be <schema>.<table>")
        return DataSettings(
            registry=settings["registry"],
            table_columns=list(settings["table_columns"]),
            search_path=list(settings.get("search_path", ["public"])),
            ignore_columns=list(settings.get("ignore_columns", [])),
            exclude=list(settings.get("exclude", [])),
            name_suffix=settings.get("name_suffix", ""),
        )


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def tables_sql(settings: DataSettings, schemas: list[str]) -> str:
    """Every table the registry lists, resolved as the application resolves it, as one JSON value."""
    names = " UNION ".join(
        f"SELECT {column}::text AS name FROM {settings.registry} WHERE coalesce({column}::text, '') <> ''"
        for column in settings.table_columns)
    managed = "ARRAY[" + ", ".join(sql_literal(schema) for schema in schemas) + "]::text[]"
    suffix = settings.name_suffix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return f"""
SET search_path TO {", ".join(settings.search_path)};
WITH registered AS (
    SELECT {sql_literal(settings.registry)} AS name, to_regclass({sql_literal(settings.registry)}) AS oid
    UNION
    SELECT name, to_regclass(name) FROM ({names}) AS listed
), resolved AS (
    SELECT registered.name, c.oid, n.nspname || '.' || c.relname AS qualified,
           quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS quoted,
           (SELECT array_agg(a.attname ORDER BY array_position(k.conkey, a.attnum))
              FROM pg_constraint k JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
             WHERE k.conrelid = c.oid AND k.contype = 'p') AS key
      FROM registered
      LEFT JOIN pg_class c ON c.oid = registered.oid
      LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
)
SELECT jsonb_build_object(
    'tables', (SELECT coalesce(jsonb_agg(jsonb_build_object('name', qualified, 'quoted', quoted, 'key', key)
                                         ORDER BY qualified), '[]')
                 FROM (SELECT DISTINCT qualified, quoted, key FROM resolved WHERE oid IS NOT NULL) AS found),
    'missing', (SELECT coalesce(jsonb_agg(DISTINCT name), '[]') FROM resolved WHERE oid IS NULL),
    'unregistered', (SELECT coalesce(jsonb_agg(n.nspname || '.' || c.relname ORDER BY n.nspname, c.relname), '[]')
                       FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                      WHERE c.relkind = 'r' AND {sql_literal(suffix)} <> ''
                        AND c.relname LIKE '%' || {sql_literal(suffix)}
                        AND n.nspname = ANY ({managed})
                        AND c.oid NOT IN (SELECT oid FROM resolved WHERE oid IS NOT NULL))
)::text;
"""


def read_data(cfg: Config, settings: DataSettings, connection: Connection) -> dict:
    """The reference data of one database, in the snapshot format."""
    listing = json.loads(psql(connection, tables_sql(settings, cfg.schemas)).split("\n")[-1])
    tables = [table for table in listing["tables"] if table["name"] not in settings.exclude]
    without_key = [table["name"] for table in tables if not table["key"]]
    if without_key:
        print(f"! no primary key, compared as a whole: {', '.join(without_key)}", file=sys.stderr)
    # one line per table: jsonb text escapes \n, but not every character splitlines() splits on
    # (e.g. U+2028 in reference texts), so split on \n only
    queries = []
    for table in tables:
        order = ", ".join(f'"{column}"' for column in table["key"] or []) or "1"
        queries.append(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY {order}), '[]')::text FROM {table['quoted']} AS t;")
    lines = psql(connection, "\n".join(queries)).split("\n") if queries else []
    if len(lines) != len(tables):
        sys.exit(f"reading reference data from {connection.label}: expected {len(tables)} tables, got {len(lines)}")
    data_tables = {}
    for table, line in zip(tables, lines, strict=True):
        key = table["key"][0] if table["key"] and len(table["key"]) == 1 else None
        data_tables[table["name"]] = {"key": key, "rows": json.loads(line)}
    return {"tables": data_tables, "missing": sorted(listing["missing"]), "unregistered": listing["unregistered"]}


@dataclass
class TableDifference:
    table: str
    only_in_first: list[dict]
    only_in_second: list[dict]
    changed: list[tuple[str, dict, dict]]  # key, then the columns that differ: {column: value} in each

    @property
    def count(self) -> int:
        return len(self.only_in_first) + len(self.only_in_second) + len(self.changed)


@dataclass(frozen=True)
class DataComparison:
    difference_count: int
    report: str
    tables: list[TableDifference]


def rows_by_key(table: dict, ignore_columns: list[str]) -> dict[str, dict]:
    """Rows by primary key value; without a single-column key, a row is its own key."""
    rows = {}
    for row in table["rows"]:
        kept = {column: value for column, value in row.items() if column not in ignore_columns}
        key = str(row[table["key"]]) if table["key"] else json.dumps(kept, sort_keys=True)
        rows[key] = kept
    return rows


def compare_table(name: str, first: dict, second: dict, ignore_columns: list[str]) -> TableDifference:
    first_rows = rows_by_key(first, ignore_columns)
    second_rows = rows_by_key(second, ignore_columns)
    changed = []
    for key in first_rows.keys() & second_rows.keys():
        first_row, second_row = first_rows[key], second_rows[key]
        columns = sorted(column for column in first_row.keys() | second_row.keys()
                         if first_row.get(column) != second_row.get(column))
        if columns:
            changed.append((key, {c: first_row.get(c) for c in columns}, {c: second_row.get(c) for c in columns}))
    return TableDifference(
        table=name,
        only_in_first=[first_rows[key] for key in sorted(first_rows.keys() - second_rows.keys(), key=sort_key)],
        only_in_second=[second_rows[key] for key in sorted(second_rows.keys() - first_rows.keys(), key=sort_key)],
        changed=sorted(changed, key=lambda item: sort_key(item[0])),
    )


def sort_key(key: str) -> tuple:
    """Numeric keys in numeric order, before the others."""
    return (0, int(key), "") if key.lstrip("-").isdigit() else (1, 0, key)


def short(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)[:200]


def compare_data(first: dict, second: dict, first_label: str, second_label: str,
                 ignore_columns: list[str], decisions: Decisions | None = None,
                 show_accepted: bool = False) -> DataComparison:
    decisions = decisions or Decisions()
    found = {}

    def shown(table: str, count: int) -> bool:
        found[table] = decisions.find(f"DATA {table}", (first_label, second_label), count)
        return show_accepted or not accepted(found[table])

    lines = []
    count = 0
    for title, names in [
        (f"reference tables only in {first_label}", sorted(first["tables"].keys() - second["tables"].keys())),
        (f"reference tables only in {second_label}", sorted(second["tables"].keys() - first["tables"].keys())),
        (f"registered but missing in {first_label} only",
         sorted(set(first["missing"]) - set(second["missing"]))),
        (f"registered but missing in {second_label} only",
         sorted(set(second["missing"]) - set(first["missing"]))),
        (f"not registered in {first_label} only", sorted(set(first["unregistered"]) - set(second["unregistered"]))),
        (f"not registered in {second_label} only", sorted(set(second["unregistered"]) - set(first["unregistered"]))),
    ]:
        count += len(names)
        names = [name for name in names if shown(name, 1)]
        if names:
            lines.append(f"{title} ({len(names)}):")
            lines.extend(f"  {name}{decisions.mark(found[name])}" for name in names)
            lines.append("")

    tables = []
    for name in sorted(first["tables"].keys() & second["tables"].keys()):
        difference = compare_table(name, first["tables"][name], second["tables"][name], ignore_columns)
        if difference.count:
            count += difference.count
            if shown(name, difference.count):
                tables.append(difference)
    if tables:
        lines.append(f"reference tables with different rows ({len(tables)}):")
        for difference in tables:
            parts = [f"{len(difference.only_in_first)} only in {first_label}",
                     f"{len(difference.only_in_second)} only in {second_label}",
                     f"{len(difference.changed)} changed"]
            mark = decisions.mark(found[difference.table])
            lines.append(f"  {difference.table}: {', '.join(parts)}{mark}")
            for label, rows in [(first_label, difference.only_in_first), (second_label, difference.only_in_second)]:
                for row in rows[:ROWS_SHOWN]:
                    lines.append(f"      only in {label}: {short(row)}")
            for key, first_values, second_values in difference.changed[:ROWS_SHOWN]:
                lines.append(f"      key {key}: {first_label} {short(first_values)} -> {second_label} {short(second_values)}")
            if max(len(difference.only_in_first), len(difference.only_in_second), len(difference.changed)) > ROWS_SHOWN:
                lines.append(f"      (first {ROWS_SHOWN} of each kind shown)")
        lines.append("")

    table_count = len(first["tables"].keys() | second["tables"].keys())
    if count:
        lines.append(f"✘ {count} reference data difference(s) between {first_label} and {second_label} "
                     f"({table_count} tables)")
    else:
        lines.append(f"✔ reference data of {first_label} and {second_label} is identical ({table_count} tables)")
    return DataComparison(difference_count=count, report="\n".join(lines), tables=tables)
