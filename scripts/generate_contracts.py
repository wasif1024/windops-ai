#!/usr/bin/env python3
"""Generate Python code from the Protobuf contracts.

make contracts-gen
"""

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PROTO_ROOT = REPOSITORY / "shared" / "protobuf"
OUTPUT = REPOSITORY / "shared" / "contracts" / "src"


def generate(output: Path) -> None:
    protos = sorted(str(path.relative_to(PROTO_ROOT)) for path in PROTO_ROOT.rglob("*.proto"))
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable, "-m", "grpc_tools.protoc",
            f"--proto_path={PROTO_ROOT}",
            f"--python_out={output}",
            f"--grpc_python_out={output}",
            f"--mypy_out={output}",
            f"--mypy_grpc_out={output}",
            *protos,
        ],
        check=True,
    )  # fmt: skip


if __name__ == "__main__":
    generate(OUTPUT)
    print(f"Generated code in {OUTPUT.relative_to(REPOSITORY)}")
