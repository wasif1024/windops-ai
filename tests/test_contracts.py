"""Check the Protobuf contracts against the rules in SRS section 7.4."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_contracts import generate

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "shared" / "protobuf"
TELEMETRY = Path("windops/telemetry/v1/telemetry.proto")
PROTO_FILES = sorted(MODULE.rglob("*.proto"))

STATE_CHANGING = re.compile(
    r"^(Create|Update|Delete|Set|Add|Remove|Submit|Approve|Reject|Cancel|Acknowledge|Reserve|Complete"
    r"|Grant|Revoke|Start|End|Disable|Reactivate|Suspend)"
)
UNVERIFIED_IDENTITY_FIELDS = {"tenant_id", "user_id"}


def request_fields(proto: str) -> dict[str, set[str]]:
    messages = re.findall(r"message (\w+Request) \{(.*?)\n\}", proto, flags=re.DOTALL)
    return {name: set(re.findall(r"(\w+) = \d+;", body)) for name, body in messages}


def missing_idempotency_keys(proto: str) -> list[str]:
    """GRP-007: state-changing calls accept an idempotency key."""
    fields = request_fields(proto)
    return [
        rpc
        for rpc, request in re.findall(r"rpc (\w+)\((\w+)\)", proto)
        if STATE_CHANGING.match(rpc) and "idempotency_key" not in fields.get(request, set())
    ]


def identity_in_requests(proto: str) -> list[str]:
    """GRP-004: tenant and user context comes from verified metadata, not request fields."""
    return [
        request
        for request, names in request_fields(proto).items()
        if names & UNVERIFIED_IDENTITY_FIELDS
    ]


def buf(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["buf", *args], capture_output=True, text=True, check=False)


def copy_module(destination: Path) -> Path:
    copy = destination / "protobuf"
    shutil.copytree(MODULE, copy)
    return copy


def test_a_contract_exists() -> None:
    assert PROTO_FILES


@pytest.mark.parametrize("proto", PROTO_FILES, ids=lambda path: path.name)
def test_state_changing_calls_accept_an_idempotency_key(proto: Path) -> None:
    assert missing_idempotency_keys(proto.read_text()) == []


@pytest.mark.parametrize("proto", PROTO_FILES, ids=lambda path: path.name)
def test_requests_do_not_carry_identity(proto: Path) -> None:
    assert identity_in_requests(proto.read_text()) == []


def test_rule_checks_catch_violations() -> None:
    bad = """
service Example {
  rpc CreateThing(CreateThingRequest) returns (Thing);
}
message CreateThingRequest {
  string tenant_id = 1;
}
"""
    assert missing_idempotency_keys(bad) == ["CreateThing"]
    assert identity_in_requests(bad) == ["CreateThingRequest"]


def test_lint_passes() -> None:
    result = buf("lint", str(MODULE))
    assert result.returncode == 0, result.stdout


def test_lint_rejects_an_unversioned_package(tmp_path: Path) -> None:
    copy = copy_module(tmp_path)
    proto = copy / TELEMETRY
    proto.write_text(proto.read_text().replace("windops.telemetry.v1", "windops.telemetry"))
    assert buf("lint", str(copy)).returncode != 0


def test_breaking_check_accepts_a_new_field(tmp_path: Path) -> None:
    copy = copy_module(tmp_path)
    proto = copy / TELEMETRY
    proto.write_text(
        proto.read_text().replace(
            "string content_hash = 2;", "string content_hash = 2;\n  string source = 3;"
        )
    )
    result = buf("breaking", str(copy), "--against", str(MODULE))
    assert result.returncode == 0, result.stdout


def test_breaking_check_rejects_a_removed_field(tmp_path: Path) -> None:
    copy = copy_module(tmp_path)
    proto = copy / TELEMETRY
    proto.write_text(proto.read_text().replace("  string content_hash = 2;\n", ""))
    assert buf("breaking", str(copy), "--against", str(MODULE)).returncode != 0


def test_generated_code_matches_the_contracts(tmp_path: Path) -> None:
    """When this fails, run: make contracts-gen"""
    generate(tmp_path)
    committed = ROOT / "shared" / "contracts" / "src"
    generated_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.py*"))
    assert generated_files
    for relative in generated_files:
        assert (committed / relative).read_text() == (tmp_path / relative).read_text(), relative
