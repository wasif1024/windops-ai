#!/usr/bin/env python3
"""Create the Azure DevOps pipeline's identity in Azure, once (docs/azure-devops.md).

    make azure-pipeline-identity ISSUER=https://vstoken.dev.azure.com/... SUBJECT=sc://org/project/windops-staging

Azure DevOps shows the issuer and subject when you create the windops-staging service connection
with "Workload identity federation (manual)". This makes, with your az login:

- a user-assigned managed identity, id-windops-pipeline, in rg-windops-pipeline: both free;
- a federated credential that trusts only that service connection, so no secret exists anywhere;
- Contributor on the subscription, to create the resources;
- Role Based Access Control Administrator, with a condition: it may grant and remove only the
  built-in roles the Bicep uses, so the pipeline can never make anyone an Owner.

It prints the values to paste back into the service connection and the windops-staging variable
group. It asks before creating anything.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.lib import settings
from scripts.setup.azure_deploy import AzureError, az, az_json, confirm

REPOSITORY = Path(__file__).resolve().parents[2]
BICEP = REPOSITORY / "infrastructure" / "azure" / "bicep"
GROUP = "rg-windops-pipeline"
IDENTITY = "id-windops-pipeline"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
RBAC_ADMINISTRATOR = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
ISSUER = re.compile(r"^https://vstoken\.dev\.azure\.com/[0-9a-f-]{36}/?$")
SUBJECT = re.compile(r"^sc://[^/]+/[^/]+/[^/]+$")


def bicep_roles() -> list[str]:
    """Every built-in role the Bicep grants, so the pipeline may grant those and no others."""
    source = "\n".join(path.read_text() for path in sorted(BICEP.rglob("*.bicep")))
    found = re.findall(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", source)
    return sorted(set(found))


def assignment_condition(roles: Sequence[str]) -> str:
    """Azure's ABAC condition: role assignments are written and deleted only for these roles."""
    allowed = ", ".join(roles)
    return (
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {{{allowed}}})) AND "
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {{{allowed}}}))"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--issuer", required=True, help="from the service connection")
    parser.add_argument("--subject", required=True, help="from the service connection")
    arguments = parser.parse_args(argv)
    try:
        if not ISSUER.match(arguments.issuer):
            raise AzureError(f"Not an Azure DevOps issuer: {arguments.issuer}")
        if not SUBJECT.match(arguments.subject):
            raise AzureError(f"Not a service connection subject: {arguments.subject}")
        values = settings.load()
        subscription = settings.get("AZURE_SUBSCRIPTION_ID")
        location = values.get("AZURE_LOCATION") or "swedencentral"
        scope = f"/subscriptions/{subscription}"
        print(f"This creates {IDENTITY} in {GROUP} ({location}), free, trusting only")
        print(f"{arguments.subject}, with Contributor and limited role granting on {scope}.")
        confirm("Create it?")
        az(
            "group", "create", "--name", GROUP, "--location", location,
            "--subscription", subscription, "--tags", "project=windops", "--output", "none",
        )  # fmt: skip
        identity = az_json("identity", "create", "--name", IDENTITY, "--resource-group", GROUP,
                           "--subscription", subscription, "--tags", "project=windops")  # fmt: skip
        az("identity", "federated-credential", "create", "--name", "azure-devops",
           "--identity-name", IDENTITY, "--resource-group", GROUP, "--subscription", subscription,
           "--issuer", arguments.issuer.rstrip("/"), "--subject", arguments.subject,
           "--audiences", "api://AzureADTokenExchange", "--output", "none")  # fmt: skip
        principal = identity["principalId"]
        az("role", "assignment", "create", "--assignee-object-id", principal,
           "--assignee-principal-type", "ServicePrincipal", "--role", CONTRIBUTOR,
           "--scope", scope, "--output", "none")  # fmt: skip
        az("role", "assignment", "create", "--assignee-object-id", principal,
           "--assignee-principal-type", "ServicePrincipal", "--role", RBAC_ADMINISTRATOR,
           "--scope", scope, "--condition", assignment_condition(bicep_roles()),
           "--condition-version", "2.0", "--output", "none")  # fmt: skip
        operator = az("ad", "signed-in-user", "show", "--query", "id", "--output", "tsv")
        tenant = az("account", "show", "--subscription", subscription, "--query", "tenantId",
                    "--output", "tsv")  # fmt: skip
        print("\nIn the service connection, enter:")
        print(json.dumps({"Subscription ID": subscription, "Service Principal Id (client ID)":
                          identity["clientId"], "Tenant ID": tenant}, indent=2))  # fmt: skip
        print("\nIn the windops-staging variable group, add:")
        variables = {
            "AZURE_SUBSCRIPTION_ID": subscription,
            "AZURE_OPERATOR_OBJECT_ID": operator,
            "AZURE_PIPELINE_PRINCIPAL_ID": principal,
        }
        print(json.dumps(variables, indent=2))
        return 0
    except (AzureError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
