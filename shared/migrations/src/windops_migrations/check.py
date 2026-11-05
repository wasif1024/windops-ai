"""Find revisions that would break a running service, by reading their code (DAT-001).

Old and new versions run side by side during a rollout. So an `expand` revision must leave the
database working for the old version, and anything else must declare itself `contract`.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

PHASES = {"expand", "contract"}
_BREAKING_OPERATIONS = {
    "drop_table": "drops a table",
    "drop_column": "drops a column",
    "rename_table": "renames a table",
    "drop_constraint": "drops a constraint",
    "drop_index": "drops an index",
}
_BREAKING_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN|CONSTRAINT|INDEX|SCHEMA)|RENAME\s+(TO|COLUMN)|(?<!BEFORE\s)TRUNCATE"
    r"|ALTER\s+COLUMN\s+\S+\s+(SET\s+NOT\s+NULL|(SET\s+DATA\s+)?TYPE))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MigrationViolation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _breaking_reason(call: ast.Call) -> str | None:
    if not (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "op"
    ):
        return None
    operation = call.func.attr
    if operation in _BREAKING_OPERATIONS:
        return _BREAKING_OPERATIONS[operation]
    if operation == "alter_column":
        if _keyword(call, "new_column_name") is not None:
            return "renames a column"
        if _keyword(call, "type_") is not None:
            return "changes a column's type"
        if _is_false(_keyword(call, "nullable")):
            return "makes a column required"
    if operation == "add_column":
        return _required_column_reason(call)
    if operation == "execute":
        return _sql_reason(call)
    return None


def _required_column_reason(call: ast.Call) -> str | None:
    """The old version does not know a new column, so its inserts must still succeed."""
    for argument in call.args:
        if (
            isinstance(argument, ast.Call)
            and _is_false(_keyword(argument, "nullable"))
            and _keyword(argument, "server_default") is None
        ):
            return "adds a required column with no server default"
    return None


def _sql_reason(call: ast.Call) -> str | None:
    if not call.args:
        return None
    statement = call.args[0]
    if isinstance(statement, ast.Constant) and isinstance(statement.value, str):
        return "runs breaking SQL" if _BREAKING_SQL.search(statement.value) else None
    return "runs SQL that cannot be read, so declare it contract if it could break a service"


def _declared_phase(tree: ast.Module) -> str | None:
    for statement in tree.body:
        target: ast.expr
        value: ast.expr | None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and target.id == "phase"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def find_violations(path: Path) -> list[MigrationViolation]:
    """Return every rule a revision file breaks."""
    tree = ast.parse(path.read_text(), filename=str(path))
    phase = _declared_phase(tree)
    if phase not in PHASES:
        return [MigrationViolation(path, 1, 'Declare phase = "expand" or phase = "contract"')]
    if phase == "contract":
        return []
    violations = []
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name == "upgrade":
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and (reason := _breaking_reason(node)):
                    violations.append(
                        MigrationViolation(
                            path,
                            node.lineno,
                            f'This {reason}, so it belongs in a phase = "contract" revision',
                        )
                    )
    return violations
