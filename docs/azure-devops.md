# Deploying with Azure DevOps

Azure Pipelines deploys WindOps AI to Azure. GitHub Actions still checks every pull request, and the pipeline deploys
what is merged to `main`. It deploys one environment, **staging**: the test stage the plan prices
([docs/azure-plan.md](azure-plan.md)), and the one shown in demos. A `live` production environment would use the same
pipeline with its own environment and variable group.

`test` is not used as an Azure environment's name: `WINDOPS_ENVIRONMENT=test` belongs to the unit tests, and is the
only value that allows plaintext gRPC.

## What a run does

[infrastructure/azure/pipelines/azure-pipelines.yml](../infrastructure/azure/pipelines/azure-pipelines.yml):

| Stage | What it does | Cost |
| --- | --- | --- |
| Validate | The Azure tests, and a `what-if` of every change. Creates nothing | $0 |
| Deploy | `make azure-up`, after someone approves the `windops-staging` environment | $0.98 an hour from here |
| Prove | The AKS nodes are Ready, and events pass their five-scenario contract on the deployed Service Bus | |
| Remove compute | `make azure-down`, when Prove or Deploy failed and when the run was cancelled, so no vCPU is left running | back to about $20 a month |

Cancelling a run stops only the agent: Azure Resource Manager keeps creating resources. So Remove compute still runs
after a cancel, cancels any deployment still running in Azure, and then deletes the compute. When a run is rejected
or cancelled before anything was deployed, it finds no resource group and creates nothing.

A run takes about an hour, so it costs about $1. To keep the compute for a live demo, tick **Keep the compute running**
when starting the run, and run `make azure-down` afterwards. The **Delete everything** option runs `make
azure-destroy` instead, after the same approval.

Azure DevOps itself is free here. Its free tier gives one Microsoft-hosted job at a time, up to 60 minutes each, and
1,800 minutes a month. Enabling it needs the organization linked to the Azure subscription for billing, which costs
nothing on the free tier.

## Signing in with no secret

The pipeline signs in to Azure as its own managed identity, `id-windops-pipeline`, through **workload identity
federation**. Azure trusts tokens from only this project's `windops-staging` service connection, so no password,
key or certificate exists to leak or expire.

| The identity has | So it can | It cannot |
| --- | --- | --- |
| Contributor on the subscription | Create the resources in the Bicep | Grant access to anyone |
| Role Based Access Control Administrator, with a condition | Grant and remove only the built-in roles the Bicep uses | Make anyone an Owner, or grant any other role |

The deploy gives it the same Key Vault, Service Bus and cluster roles as the operator, through the Bicep's `admins`.
Each run allows the agent's IP address through the firewalls while it deploys, and `make azure-down` removes it
again, keeping only `AZURE_OPERATOR_IP_RANGES`.

## Set it up once

1. **Create the organization and project.** At [dev.azure.com](https://dev.azure.com), create an organization and a
   private project named `windops-ai`. Under **Organization settings, Billing**, link the Azure subscription, which
   turns on the free tier of parallel jobs.
2. **Create the service connection.** In **Project settings, Service connections**, create a new **Azure Resource
   Manager** connection. Choose **App registration or managed identity (manual)** with **Workload identity
   federation**, and name it `windops-staging`. Use the **Azure Cloud** environment and the **Subscription** scope
   level, with your subscription's ID and name. Azure DevOps then shows an **Issuer** and a **Subject identifier**:
   leave the page open. New connections show Entra's issuer, `https://login.microsoftonline.com/<tenant>/v2.0`,
   with a `/eid1/...` subject; older ones showed `https://vstoken.dev.azure.com/...` with `sc://...`. Both work.
3. **Create the pipeline's identity in Azure.** Put the two values in `.env` as `AZURE_PIPELINE_ISSUER` and
   `AZURE_PIPELINE_SUBJECT`, then, signed in with `az login`, run:

   ```bash
   make azure-pipeline-identity
   ```

   It checks the issuer is your subscription's tenant, asks first, creates the free identity and its two roles, and
   prints the subscription, client and tenant IDs. Enter them in the service connection, then select **Verify and
   save**.
4. **Create the variable group.** In **Pipelines, Library**, add a variable group named `windops-staging` with the
   values the command printed, plus:

   | Variable | Value |
   | --- | --- |
   | `AZURE_SUBSCRIPTION_ID` | Printed by step 3 |
   | `AZURE_OPERATOR_OBJECT_ID` | Printed by step 3: your own Entra object ID |
   | `AZURE_PIPELINE_PRINCIPAL_ID` | Printed by step 3 |
   | `AZURE_ALERT_EMAIL` | Where budget and alert emails go |
   | `AZURE_OPERATOR_IP_RANGES` | Your public IPv4 address, so you can reach the cluster too |

   None of them is a secret, so none needs to be marked secret.
5. **Create the environment and its approval.** In **Pipelines, Environments**, create `windops-staging`. Under
   **Approvals and checks**, add an **Approvals** check with yourself as the approver. Every deploy and destroy then
   waits for you.
6. **Create the pipeline.** In **Pipelines**, choose **New pipeline**, then **GitHub**, and select
   `wasif1024/windops-ai`. This installs the Azure Pipelines app on the repository. Then choose **Existing Azure
   Pipelines YAML file** and `infrastructure/azure/pipelines/azure-pipelines.yml`. Grant it the service connection and
   variable group when the first run asks.

After that, a merge that changes the Azure files starts a run, and **Run pipeline** starts one by hand.
