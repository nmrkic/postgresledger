"""Differences between two schema dumps, object by object, without a database.

pgschema writes every object as its own block under a header
`-- Name: <name>; Type: <type>; Schema: ...`, in a stable order, so two dumps
can be compared block by block.

With several schemas, a snapshot is one dump per schema, each under a
`-- schema: <name>` line, and objects are named <schema>.<name>.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

HEADER_START = "-- Name: "
# starts each schema's part in a snapshot or a migration of several schemas
SCHEMA_MARKER = "-- schema: "
# lines around an object's SQL that are not part of it
SEPARATOR_LINES = ("", "--")


@dataclass(frozen=True, order=True)
class SchemaObject:
    """One object in a dump, e.g. TABLE customer. Sorted by type, then name."""

    type: str
    name: str


def parse_header(line: str) -> SchemaObject | None:
    """The object a `-- Name: <name>; Type: <type>; Schema: ...` line starts; None for any other line."""
    if not line.startswith(HEADER_START):
        return None
    name, _, rest = line.removeprefix(HEADER_START).partition("; Type: ")
    object_type, _, schema = rest.partition("; Schema:")
    if not object_type or not schema:
        return None
    return SchemaObject(type=object_type, name=name)


def strip_separator_lines(lines: list[str]) -> list[str]:
    """`lines` without the empty and `--` lines at the start and the end."""
    start = 0
    end = len(lines)
    while start < end and lines[start].strip() in SEPARATOR_LINES:
        start += 1
    while end > start and lines[end - 1].strip() in SEPARATOR_LINES:
        end -= 1
    return lines[start:end]


def add_object(sql_by_object: dict[SchemaObject, str], schema_object: SchemaObject, lines: list[str]) -> None:
    # same type and name twice (overloaded functions): the next ones become "name #2", "name #3", ...
    unique_object = schema_object
    number = 2
    while unique_object in sql_by_object:
        unique_object = SchemaObject(schema_object.type, f"{schema_object.name} #{number}")
        number += 1
    sql_by_object[unique_object] = "\n".join(strip_separator_lines(lines))


def objects(dump: str) -> dict[SchemaObject, str]:
    """The SQL of every object in a pgschema dump."""
    sql_by_object: dict[SchemaObject, str] = {}
    current_object = None
    current_lines: list[str] = []
    # set by a `-- schema: <name>` line; a dump of one schema has none
    current_schema = ""
    for line in dump.splitlines():
        if line.startswith(SCHEMA_MARKER):
            # the previous object ends where the next schema starts
            if current_object is not None:
                add_object(sql_by_object, current_object, current_lines)
            current_object = None
            current_schema = line.removeprefix(SCHEMA_MARKER).strip()
            continue
        header_object = parse_header(line)
        if header_object is not None:
            if current_object is not None:
                add_object(sql_by_object, current_object, current_lines)
            if current_schema:
                header_object = SchemaObject(header_object.type, f"{current_schema}.{header_object.name}")
            current_object = header_object
            current_lines = []
        elif current_object is not None:
            current_lines.append(line)
    if current_object is not None:
        add_object(sql_by_object, current_object, current_lines)
    return sql_by_object


@dataclass(frozen=True)
class Comparison:
    difference_count: int  # 0 when the dumps are identical
    report: str  # readable, for the terminal


def compare(first_dump: str, second_dump: str, first_label: str, second_label: str) -> Comparison:
    first_objects = objects(first_dump)
    second_objects = objects(second_dump)
    only_in_first = sorted(item for item in first_objects if item not in second_objects)
    only_in_second = sorted(item for item in second_objects if item not in first_objects)
    changed = sorted(item for item in first_objects
                     if item in second_objects and first_objects[item] != second_objects[item])
    difference_count = len(only_in_first) + len(only_in_second) + len(changed)
    # the type column is as wide as the longest type
    width = max([len(item.type) for item in [*only_in_first, *only_in_second, *changed]] + [4])

    report_lines = []
    for title, missing_objects in [(f"only in {first_label}", only_in_first),
                                   (f"only in {second_label}", only_in_second)]:
        if missing_objects:
            report_lines.append(f"{title} ({len(missing_objects)}):")
            for item in missing_objects:
                report_lines.append(f"  {item.type:<{width}}  {item.name}")
            report_lines.append("")
    if changed:
        report_lines.append(f"different ({len(changed)}):")
        for item in changed:
            report_lines.append(f"  {item.type:<{width}}  {item.name}")
            diff = difflib.unified_diff(first_objects[item].splitlines(), second_objects[item].splitlines(),
                                        first_label, second_label, n=1, lineterm="")
            for diff_line in diff:
                report_lines.append(f"      {diff_line}")
        report_lines.append("")
    if difference_count:
        report_lines.append(f"✘ {difference_count} difference(s) between {first_label} ({len(first_objects)} objects) "
                            f"and {second_label} ({len(second_objects)} objects)")
    else:
        report_lines.append(f"✔ {first_label} and {second_label} are identical ({len(first_objects)} objects)")
    return Comparison(difference_count=difference_count, report="\n".join(report_lines))
