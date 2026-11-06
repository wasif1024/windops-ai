#!/usr/bin/env python3
"""Create a service from the template, so every service starts the same way.

make service NAME=telemetry-service
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from scripts.setup.provision_postgres import SERVICE_NAMESPACES

REPOSITORY = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().parent / "service_template"
# Added for a service that stores state (DAT-001).
DATABASE_TEMPLATE = Path(__file__).resolve().parent / "service_database_template"

# SRS section 11.2. Every other service runs in windops-application.
SYSTEM_NAMESPACE_SERVICES = {"identity", "authorization", "tenant", "web-bff"}
# APISIX is configured, not written in Python, so it does not use this template (M1.8).
NOT_PYTHON_SERVICES = {"api-gateway"}
NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")


class ScaffoldError(Exception):
    """The service cannot be created."""


def template_values(directory: str, *, database: bool | None = None) -> dict[str, str]:
    if not NAME_PATTERN.fullmatch(directory):
        raise ScaffoldError(f"{directory!r} must use lowercase letters, digits and hyphens")
    if directory in NOT_PYTHON_SERVICES:
        raise ScaffoldError(f"{directory} is not a Python service, so it has no template")
    short = directory.removesuffix("-service")
    uses_database = short in SERVICE_NAMESPACES if database is None else database
    return {
        "extra_dependencies": ', "windops-migrations"' if uses_database else "",
        "extra_sources": "\nwindops-migrations = { workspace = true }" if uses_database else "",
        "directory": directory,
        "short": short,
        "distribution": f"windops-{short}",
        "package": f"windops_{short.replace('-', '_')}",
        "workload": f"windops-{short}",
        "namespace": "windops-system"
        if short in SYSTEM_NAMESPACE_SERVICES
        else "windops-application",
    }


def _render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    if "{{" in text:
        raise ScaffoldError(f"The template has an unknown placeholder: {text[text.index('{{') :]}")
    return text


def _destination(source: Path, template: Path, values: dict[str, str]) -> Path:
    relative = source.relative_to(template).with_suffix("")
    if relative == Path("src/init.py"):
        return Path("src") / values["package"] / "__init__.py"
    if relative.parts[0] == "src":
        return Path("src") / values["package"] / Path(*relative.parts[1:])
    if relative == Path("tests/test_service.py"):
        return Path("tests") / f"test_{values['package']}.py"
    return relative


def create_service(directory: str, root: Path, *, database: bool | None = None) -> Path:
    """Write the service's files under root/services, and return its directory.

    A service in SERVICE_NAMESPACES gets migrations, unless `database` says otherwise.
    """
    values = template_values(directory, database=database)
    target = root / "services" / directory
    existing = {path.name for path in target.iterdir()} - {".gitkeep"} if target.exists() else set()
    if existing:
        raise ScaffoldError(f"{target} already has files, so it was not changed")
    templates = [TEMPLATE]
    if values["extra_dependencies"]:
        templates.append(DATABASE_TEMPLATE)
    for template in templates:
        for source in sorted(template.rglob("*.tmpl")):
            output = target / _destination(source, template, values)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(_render(source.read_text(), values))
    (target / "src" / values["package"] / "py.typed").touch()
    (target / ".gitkeep").unlink(missing_ok=True)
    return target


def _add_to_list(pyproject: str, key: str, additions: list[str]) -> str:
    found = re.search(rf"^{key} = \[(.*?)\]", pyproject, re.S | re.M)
    if found is None:
        raise ScaffoldError(f"The root pyproject.toml has no {key} list")
    items = sorted(set(re.findall(r'"([^"]+)"', found.group(1))) | set(additions))
    listed = "".join(f'    "{item}",\n' for item in items)
    return pyproject[: found.start()] + f"{key} = [\n{listed}]" + pyproject[found.end() :]


def add_workspace_member(directory: str, pyproject: str) -> str:
    """Return the root pyproject text with the service in the workspace and type checks."""
    base = f"services/{directory}"
    paths = [f"{base}/src", f"{base}/tests"]
    pyproject = _add_to_list(pyproject, "members", [base])
    pyproject = _add_to_list(pyproject, "files", paths)
    return _add_to_list(pyproject, "mypy_path", paths)


def add_service_to_kustomization(directory: str, kustomization: str) -> str:
    """Return the base services kustomization with the service's manifests listed."""
    found = re.search(r"^resources:.*\Z", kustomization, re.S | re.M)
    if found is None:
        raise ScaffoldError("The services kustomization has no resources list")
    items = set(re.findall(r"^  - (\S+)$", found.group(0), re.M))
    items.add(f"../../../../services/{directory}/deploy")
    listed = "".join(f"  - {item}\n" for item in sorted(items))
    return kustomization[: found.start()] + f"resources:\n{listed}"


def register_in_workspace(directory: str, root: Path) -> None:
    """Add the service to the workspace, lockfile and cluster, so all checks include it."""
    values = template_values(directory)
    services = root / "infrastructure" / "kubernetes" / "base" / "services" / "kustomization.yaml"
    services.write_text(add_service_to_kustomization(directory, services.read_text()))
    pyproject = root / "pyproject.toml"
    pyproject.write_text(add_workspace_member(directory, pyproject.read_text()))
    subprocess.run(["uv", "add", "--dev", values["distribution"]], cwd=root, check=True)
    # Line wrapping and import order depend on the name, so format the result.
    target = f"services/{directory}"
    subprocess.run(["uv", "run", "ruff", "check", "--fix", "-q", target], cwd=root, check=True)
    subprocess.run(["uv", "run", "ruff", "format", "-q", target], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="The directory name in SRS 11.5, such as telemetry-service")
    parser.add_argument(
        "--database",
        action="store_true",
        help="Add migrations, for a service not in SERVICE_NAMESPACES",
    )
    arguments = parser.parse_args()
    try:
        target = create_service(arguments.name, REPOSITORY, database=arguments.database or None)
        register_in_workspace(arguments.name, REPOSITORY)
    except ScaffoldError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Created {target.relative_to(REPOSITORY)}. Next, follow the steps in its README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
