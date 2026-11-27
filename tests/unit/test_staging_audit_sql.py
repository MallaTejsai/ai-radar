"""SQL construction of the read-only staging audit.

The audit runs every statement through ``cur.execute(sql, params)``. Psycopg
parses the statement for placeholders whenever a parameter sequence is supplied,
including an empty one, and accepts only ``%s``, ``%b``, ``%t`` (plus ``%%``).
A literal ``LIKE '%provider%'`` therefore raised::

    only '%s', '%b', '%t' are allowed as placeholders, got '%p'

These tests pin the fix: LIKE patterns are bound values, every statement the
module can send has valid placeholders, and nothing in the module writes.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts" / "staging_audit.py"

ALLOWED_PLACEHOLDERS = frozenset({"s", "b", "t", "%"})
WRITE_KEYWORDS = (
    "insert ",
    "update ",
    "delete ",
    "drop ",
    "truncate ",
    "alter ",
    "create ",
    "grant ",
    "revoke ",
    "copy ",
    "vacuum ",
)


def load_audit_module():
    spec = importlib.util.spec_from_file_location("staging_audit_script", AUDIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def placeholder_violations(sql: str) -> list[str]:
    """Mirror psycopg's placeholder scan and report the offending positions."""
    violations: list[str] = []
    index = 0
    while index < len(sql):
        if sql[index] != "%":
            index += 1
            continue
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if nxt and nxt in ALLOWED_PLACEHOLDERS:
            index += 2
            continue
        violations.append(f"offset {index}: '{sql[index:index + 2]}'")
        index += 1
    return violations


def placeholder_count(sql: str) -> int:
    return len(re.findall(r"%(?:s|b|t)", sql))


class FakeCursor:
    """Records what would be sent to PostgreSQL. Never talks to a server."""

    def __init__(self, row=("CHECK (provider = ANY (ARRAY['sarvam']))",)) -> None:
        self.calls: list[tuple[str, object]] = []
        self._row = row
        self.description = [type("Column", (), {"name": "pg_get_constraintdef"})()]

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return [self._row]


# --------------------------------------------------------------------- scanner
def test_scanner_rejects_the_original_bug():
    """Control: the pre-fix statement must be reported, or the tests are blind."""
    violations = placeholder_violations("LIKE '%provider%'")
    assert violations, "the scanner must reject the pre-fix statement"
    assert violations[0] == "offset 6: '%p'"


def test_scanner_accepts_escaped_and_bound_placeholders():
    assert placeholder_violations("SELECT 1 WHERE a LIKE %s") == []
    assert placeholder_violations("SELECT 1 WHERE a LIKE '%%provider%%'") == []
    assert placeholder_violations("SELECT %(x)s") == ["offset 7: '%('"]


# ---------------------------------------------------------------- provider SQL
def test_provider_constraint_query_has_no_literal_percent():
    module = load_audit_module()
    assert placeholder_violations(module.PROVIDER_CONSTRAINT_SQL) == []


def test_provider_constraint_binds_the_like_pattern():
    module = load_audit_module()
    assert "LIKE %s" in module.PROVIDER_CONSTRAINT_SQL
    assert "%provider%" not in module.PROVIDER_CONSTRAINT_SQL
    assert module.PROVIDER_CONSTRAINT_PARAMS == ("%provider%",)
    assert placeholder_count(module.PROVIDER_CONSTRAINT_SQL) == len(module.PROVIDER_CONSTRAINT_PARAMS)


def test_provider_constraint_query_is_read_only():
    module = load_audit_module()
    sql = module.PROVIDER_CONSTRAINT_SQL.strip().upper()
    assert sql.startswith("SELECT")
    for keyword in WRITE_KEYWORDS:
        assert keyword not in sql.upper()


# ------------------------------------------------------------------ alert SQL
def test_alert_lookup_binds_its_like_pattern():
    module = load_audit_module()
    assert placeholder_violations(module.ALERT_LOOKUP_SQL) == []
    assert "LIKE %s" in module.ALERT_LOOKUP_SQL
    params = module.alert_lookup_params()
    assert params == (f"%{module.ALERT_TITLE_HINT}%",)
    assert module.ALERT_TITLE_HINT not in module.ALERT_LOOKUP_SQL
    assert placeholder_count(module.ALERT_LOOKUP_SQL) == len(params)


# ------------------------------------------------------- whole-module coverage
def test_every_module_sql_constant_has_valid_placeholders():
    module = load_audit_module()
    constants = {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert {"PROVIDER_CONSTRAINT_SQL", "ALERT_LOOKUP_SQL"} <= set(constants)
    for name, sql in constants.items():
        assert placeholder_violations(sql) == [], f"{name} contains an invalid placeholder"


def test_every_sql_literal_in_the_source_is_read_only_and_percent_safe():
    """Static sweep: no SQL literal in the file can carry a bare percent sign."""
    tree = ast.parse(AUDIT_PATH.read_text(encoding="utf-8"))
    sql_literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if "select " in lowered or " from " in lowered:
                sql_literals.append(node.value)

    assert sql_literals, "expected SQL literals in the audit module"
    for sql in sql_literals:
        assert placeholder_violations(sql) == [], f"invalid placeholder in: {sql[:80]!r}"
        lowered = sql.lower()
        for keyword in WRITE_KEYWORDS:
            assert keyword not in lowered, f"write statement found: {sql[:80]!r}"


# ------------------------------------------------------- binding + read-only IO
def test_scalar_forwards_parameters_unchanged():
    import asyncio

    module = load_audit_module()
    audit = module.Audit.__new__(module.Audit)  # no config, no connection
    cursor = FakeCursor()

    value = asyncio.run(
        audit._scalar(cursor, module.PROVIDER_CONSTRAINT_SQL, module.PROVIDER_CONSTRAINT_PARAMS)
    )

    assert value == "CHECK (provider = ANY (ARRAY['sarvam']))"
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert sql is module.PROVIDER_CONSTRAINT_SQL
    assert params == ("%provider%",)
    assert isinstance(params, tuple)


def test_rows_forwards_parameters_unchanged():
    import asyncio

    module = load_audit_module()
    audit = module.Audit.__new__(module.Audit)
    cursor = FakeCursor()

    rows = asyncio.run(audit._rows(cursor, module.ALERT_LOOKUP_SQL, module.alert_lookup_params()))

    assert rows == [{"pg_get_constraintdef": "CHECK (provider = ANY (ARRAY['sarvam']))"}]
    sql, params = cursor.calls[0]
    assert params == ("%art of play%",)


def test_audit_session_is_read_only_and_only_selects():
    source = AUDIT_PATH.read_text(encoding="utf-8")
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in source
    # The read-only statement must be issued before the report queries run.
    assert source.index("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY") < source.index(
        "await self._collect(cur)"
    )


def test_dsn_is_never_used_in_sql():
    """The audit must not interpolate a connection string into a statement."""
    module = load_audit_module()
    for name, value in vars(module).items():
        if name.endswith("_SQL") and isinstance(value, str):
            assert "postgres://" not in value
            assert "postgresql://" not in value


@pytest.mark.parametrize(
    "pattern",
    ["%p", "%x", "%(name)s", "%"],
)
def test_scanner_flags_common_mistakes(pattern):
    assert placeholder_violations(f"LIKE '{pattern}'") != []
