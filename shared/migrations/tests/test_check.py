"""A breaking change must never pass as a harmless one (DAT-001)."""

from pathlib import Path

import pytest
from windops_migrations import find_violations

TEMPLATE = """
from alembic import op
import sqlalchemy as sa

phase{annotation} = "{phase}"

def upgrade() -> None:
    {body}

def downgrade() -> None:
    op.drop_table("anything")
"""


def write(tmp_path: Path, body: str, phase: str = "expand", annotation: str = ": str") -> Path:
    path = tmp_path / "rev_1_example.py"
    path.write_text(TEMPLATE.format(body=body, phase=phase, annotation=annotation))
    return path


@pytest.mark.parametrize(
    "body",
    [
        'op.create_table("items", sa.Column("id", sa.Integer(), primary_key=True))',
        'op.add_column("items", sa.Column("note", sa.Text(), nullable=True))',
        'op.add_column("items", sa.Column("note", sa.Text()))',
        'op.add_column("items", sa.Column("n", sa.Integer(), nullable=False, server_default="0"))',
        'op.create_index("items_note", "items", ["note"])',
        'op.alter_column("items", "note", nullable=True)',
        'op.execute("CREATE INDEX items_note ON items (note)")',
        "op.execute(\"UPDATE items SET note = 'dropped' WHERE note IS NULL\")",
        'op.execute("CREATE TRIGGER t BEFORE TRUNCATE ON items EXECUTE FUNCTION f()")',
    ],
)
def test_safe_changes_pass_as_expand(tmp_path: Path, body: str) -> None:
    assert find_violations(write(tmp_path, body)) == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('op.drop_table("items")', "drops a table"),
        ('op.drop_column("items", "note")', "drops a column"),
        ('op.rename_table("items", "things")', "renames a table"),
        ('op.drop_index("items_note")', "drops an index"),
        ('op.drop_constraint("items_key", "items")', "drops a constraint"),
        ('op.alter_column("items", "note", new_column_name="text")', "renames a column"),
        ('op.alter_column("items", "note", type_=sa.Text())', "changes a column's type"),
        ('op.alter_column("items", "note", nullable=False)', "makes a column required"),
        ('op.add_column("items", sa.Column("n", sa.Integer(), nullable=False))', "required column"),
        ('op.execute("DROP TABLE items")', "breaking SQL"),
        ('op.execute("drop   column note")', "breaking SQL"),
        ('op.execute("ALTER TABLE items RENAME TO things")', "breaking SQL"),
        ('op.execute("ALTER TABLE items RENAME COLUMN a TO b")', "breaking SQL"),
        ('op.execute("TRUNCATE items")', "breaking SQL"),
        ('op.execute("ALTER TABLE i ALTER COLUMN n SET NOT NULL")', "breaking SQL"),
        ('op.execute("ALTER TABLE i ALTER COLUMN n TYPE text")', "breaking SQL"),
        ("op.execute(statement)", "cannot be read"),
        ('op.execute(f"DROP TABLE {name}")', "cannot be read"),
    ],
)
def test_breaking_changes_fail_as_expand(tmp_path: Path, body: str, reason: str) -> None:
    violations = find_violations(write(tmp_path, body))
    assert len(violations) == 1
    assert reason in violations[0].message
    assert 'phase = "contract"' in violations[0].message


def test_a_breaking_change_passes_when_it_declares_contract(tmp_path: Path) -> None:
    assert find_violations(write(tmp_path, 'op.drop_table("items")', phase="contract")) == []


def test_a_nested_breaking_change_is_found(tmp_path: Path) -> None:
    body = 'if True:\n        op.drop_table("items")'
    assert len(find_violations(write(tmp_path, body))) == 1


def test_a_breaking_downgrade_does_not_count(tmp_path: Path) -> None:
    assert find_violations(write(tmp_path, "pass")) == []


def test_a_phase_without_a_type_annotation_is_read(tmp_path: Path) -> None:
    assert find_violations(write(tmp_path, "pass", annotation="")) == []


@pytest.mark.parametrize("phase", ["", "maybe", "EXPAND"])
def test_a_revision_must_declare_a_known_phase(tmp_path: Path, phase: str) -> None:
    violations = find_violations(write(tmp_path, "pass", phase=phase))
    assert "Declare phase" in violations[0].message


def test_a_revision_with_no_phase_fails(tmp_path: Path) -> None:
    path = tmp_path / "rev_1_example.py"
    path.write_text("def upgrade() -> None:\n    pass\n")
    assert "Declare phase" in find_violations(path)[0].message


def test_a_violation_names_its_file_and_line(tmp_path: Path) -> None:
    path = write(tmp_path, 'op.drop_table("items")')
    violation = find_violations(path)[0]
    assert str(violation).startswith(f"{path}:{violation.line}:")
    assert violation.line > 1
