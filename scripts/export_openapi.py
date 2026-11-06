#!/usr/bin/env python3
"""Write Web BFF's OpenAPI document to docs/api, which a test keeps up to date (API-002).

make openapi
"""

import json
import sys
from pathlib import Path
from typing import Any

from windops_web_bff.api import Dependencies, create_router, openapi_document

DOCUMENT = Path(__file__).resolve().parent.parent / "docs" / "api" / "web-bff-v1.openapi.json"


def generated() -> str:
    # The routes describe the API without running it, so nothing here is called.
    unused: Any = None
    router = create_router(
        Dependencies(
            verifier=unused,
            limiter=unused,
            identity_secret="",
            tenant=unused,
            authorization=unused,
        )
    )
    return json.dumps(openapi_document(router), indent=2, sort_keys=True) + "\n"


def main() -> int:
    DOCUMENT.write_text(generated())
    print(f"Wrote {DOCUMENT.relative_to(DOCUMENT.parents[2])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
