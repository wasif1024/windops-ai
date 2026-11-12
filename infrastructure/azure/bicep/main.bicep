// WindOps AI on Azure (ADR 0006, docs/azure-plan.md).
//
// Two resource groups, so compute can be deleted between demos without touching data:
//
// - rg-windops-<env>-data exists the whole time: the network, Service Bus, PostgreSQL, Key Vault,
//   the registry, monitoring, every managed identity, and their roles. About $19 a month.
// - rg-windops-<env>-compute exists only while the platform runs: AKS, App Gateway and their
//   public addresses and Managed Grafana. About $1.03 an hour. deployCompute=false leaves it out.
//
// Deploy through make azure-plan, azure-up and azure-down, which show a what-if first. Nothing
// here holds a secret: every workload signs in with Microsoft Entra Workload ID.
targetScope = 'subscription'

@description('The region for every resource. Sweden Central was the cheapest compared (docs/azure-plan.md).')
param location string = 'swedencentral'

@description('staging is the test stage the pipeline deploys and proves; live is production. Workloads read it as WINDOPS_ENVIRONMENT.')
@allowed(['dev', 'staging', 'live'])
param environment string = 'staging'

@description('Create the compute resource group: AKS and App Gateway. false keeps only the data.')
param deployCompute bool = false

@description('The operator\'s public IPv4 ranges, allowed to the AKS API server, Key Vault and Service Bus.')
@minLength(1)
param operatorIpRanges array

@description('The operator\'s Microsoft Entra object ID: cluster admin, and writer of secrets and certificates.')
param operatorObjectId string

@description('The Azure DevOps pipeline\'s identity, given the same admin roles as the operator. Empty when run by hand.')
param pipelinePrincipalId string = ''

@description('Where budget and alert emails go. From .env, never committed.')
param alertEmail string

@description('The monthly budget in USD. Alerts at 50%, 80% and 100% of it, and at 100% forecast.')
param budgetAmount int = 100

@description('The first month the budget covers, as yyyy-MM-01.')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

@description('The AKS Kubernetes minor version, the same as the local cluster.')
param kubernetesVersion string = '1.35'

@description('Key Vault secret ID of App Gateway\'s HTTPS certificate. Empty leaves App Gateway out.')
param gatewayCertificateUri string = ''

@description('The cluster CA certificate, base64 DER, that App Gateway trusts for APISIX.')
param gatewayTrustedRootCertificate string = ''

@description('The private address of APISIX\'s internal load balancer, inside the AKS subnet.')
param gatewayInternalIp string = '10.40.7.250'

@description('Turn on Defender for Containers for the subscription. Billed per running vCore.')
param enableDefender bool = true

var suffix = take(uniqueString(subscription().id, 'windops', environment), 5)
var tags = {
  project: 'windops'
  environment: environment
  owner: 'windops-operator'
}
var dataGroupName = 'rg-windops-${environment}-data'
var computeGroupName = 'rg-windops-${environment}-compute'
// Who may write secrets and certificates, use Service Bus, and run the cluster.
var admins = concat(
  [{ principalId: operatorObjectId, principalType: 'User' }],
  empty(pipelinePrincipalId) ? [] : [{ principalId: pipelinePrincipalId, principalType: 'ServicePrincipal' }]
)
var deployGateway = deployCompute && !empty(gatewayCertificateUri) && !empty(gatewayTrustedRootCertificate)

resource dataGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: dataGroupName
  location: location
  tags: tags
}

resource computeGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = if (deployCompute) {
  name: computeGroupName
  location: location
  tags: tags
}

// The public addresses come first, so Service Bus can allow the cluster's outbound address.
module edge 'modules/edge.bicep' = if (deployCompute) {
  name: 'windops-edge'
  scope: computeGroup
  params: {
    location: location
    environment: environment
    suffix: suffix
    tags: tags
  }
}

module data 'modules/data.bicep' = {
  name: 'windops-data'
  scope: dataGroup
  params: {
    location: location
    environment: environment
    suffix: suffix
    tags: tags
    allowedIpRanges: concat(operatorIpRanges, deployCompute ? [edge!.outputs.outboundIpAddress] : [])
    operatorIpRanges: operatorIpRanges
    admins: admins
    alertEmail: alertEmail
  }
}

module compute 'modules/compute.bicep' = if (deployCompute) {
  name: 'windops-compute'
  scope: computeGroup
  params: {
    location: location
    environment: environment
    tags: tags
    kubernetesVersion: kubernetesVersion
    operatorIpRanges: operatorIpRanges
    admins: admins
    aksSubnetId: data.outputs.aksSubnetId
    appGatewaySubnetId: data.outputs.appGatewaySubnetId
    controlPlaneIdentityId: data.outputs.controlPlaneIdentityId
    appGatewayIdentityId: data.outputs.appGatewayIdentityId
    outboundIpId: edge!.outputs.outboundIpId
    gatewayIpId: edge!.outputs.gatewayIpId
    logAnalyticsId: data.outputs.logAnalyticsId
    prometheusRuleId: data.outputs.prometheusRuleId
    prometheusEndpointId: data.outputs.prometheusEndpointId
    containerInsightsRuleId: data.outputs.containerInsightsRuleId
    deployGateway: deployGateway
    gatewayCertificateUri: gatewayCertificateUri
    gatewayTrustedRootCertificate: gatewayTrustedRootCertificate
    gatewayInternalIp: gatewayInternalIp
  }
}

// Grafana runs only with the compute: Azure offers it only as Standard, about $31 a month always on.
module grafana 'modules/grafana.bicep' = if (deployCompute) {
  name: 'windops-grafana'
  scope: computeGroup
  params: {
    location: location
    environment: environment
    suffix: suffix
    tags: tags
    operatorObjectId: operatorObjectId
    prometheusWorkspaceId: resourceId(
      subscription().subscriptionId, dataGroupName, 'Microsoft.Monitor/accounts', data.outputs.prometheusWorkspaceName
    )
  }
}

module grafanaAccess 'modules/grafana-access.bicep' = if (deployCompute) {
  name: 'windops-grafana-access'
  scope: dataGroup
  params: {
    grafanaPrincipalId: grafana!.outputs.principalId
    prometheusWorkspaceName: data.outputs.prometheusWorkspaceName
  }
}

// Once the cluster exists: each workload trusts its own service account, and nodes pull images.
module access 'modules/access.bicep' = if (deployCompute) {
  name: 'windops-access'
  scope: dataGroup
  params: {
    oidcIssuerUrl: compute!.outputs.oidcIssuerUrl
    kubeletObjectId: compute!.outputs.kubeletObjectId
    registryName: data.outputs.registryName
  }
}

module guardrails 'modules/guardrails.bicep' = {
  name: 'windops-guardrails'
  params: {
    environment: environment
    alertEmail: alertEmail
    budgetAmount: budgetAmount
    budgetStartDate: budgetStartDate
    enableDefender: enableDefender
  }
}

output dataResourceGroup string = dataGroup.name
output computeResourceGroup string = deployCompute ? computeGroup.name : ''
output clusterName string = deployCompute ? compute!.outputs.clusterName : ''
output gatewayFqdn string = deployCompute ? edge!.outputs.gatewayFqdn : ''
output keyVaultName string = data.outputs.keyVaultName
output registryName string = data.outputs.registryName
output serviceBusNamespace string = data.outputs.serviceBusHost
output postgresHost string = data.outputs.postgresHost
output tenantId string = tenant().tenantId
// Client IDs are not secrets: the overlay's ${AZURE_CLIENT_ID_...} placeholders take them.
output workloadClientIds array = data.outputs.workloadClientIds
output postgresAdminClientId string = data.outputs.postgresAdminClientId
output applicationInsightsName string = data.outputs.applicationInsightsName
output grafanaEndpoint string = deployCompute ? grafana!.outputs.endpoint : ''
