"""Decisions about known differences between databases (decisions_file), read by `compare`."""

from __future__ import annotations

import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("fix", "in-flight", "accepted")


@dataclass(frozen=True)
class Decision:
    object: str
    env: str
    decision: str
    ticket: str = ""
    note: str = ""


@dataclass
class Decisions:
    entries: list[Decision] = field(default_factory=list)
    used: set[Decision] = field(default_factory=set)
    counts: Counter = field(default_factory=Counter)  # differences per decision, "undecided" for the rest

    def find(self, name: str, envs: tuple[str, str], count: int = 1) -> Decision | None:
        decision = next((entry for entry in self.entries if entry.object == name and entry.env in envs), None)
        if decision:
            self.used.add(decision)
        self.counts[decision.decision if decision else "undecided"] += count
        return decision

    def mark(self, decision: Decision | None) -> str:
        """The text after a difference in the report; nothing without a decisions file."""
        if not self.entries:
            return ""
        if not decision:
            return "  [undecided]"
        text = " ".join(filter(None, [decision.decision, decision.ticket]))
        return f"  [{text}: {decision.note}]" if decision.note else f"  [{text}]"

    def summary(self, envs: tuple[str, str]) -> str:
        lines = ["decisions: " + ", ".join(f"{self.counts[kind]} {kind}" for kind in ("undecided", *KINDS))]
        for entry in self.entries:
            if entry.env in envs and entry not in self.used:
                lines.append(f"! no difference matches {entry.object} on {entry.env}")
        return "\n".join(lines)


def accepted(decision: Decision | None) -> bool:
    return decision is not None and decision.decision == "accepted"


def load(path: Path) -> Decisions:
    """The decisions in `path`; none when there is no such file."""
    if not path.is_file():
        return Decisions()
    entries = []
    for number, item in enumerate(tomllib.loads(path.read_text()).get("decision", []), start=1):
        if not all(item.get(name) for name in ("object", "env", "decision")):
            sys.exit(f"{path.name}, decision {number}: needs object, env and decision")
        if item["decision"] not in KINDS:
            sys.exit(f"{path.name}, decision {number}: '{item['decision']}' - one of {', '.join(KINDS)}")
        if item["decision"] == "in-flight" and not item.get("ticket"):
            sys.exit(f"{path.name}, decision {number}: in-flight needs a ticket")
        entries.append(Decision(item["object"], item["env"], item["decision"],
                                item.get("ticket", ""), item.get("note", "")))
    return Decisions(entries)
