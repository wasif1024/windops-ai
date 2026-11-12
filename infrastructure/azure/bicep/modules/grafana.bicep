// Azure Managed Grafana, in the compute group: made with the cluster and deleted with it, as Azure
// offers only the Standard SKU, about $31 a month always on (docs/azure-plan.md). The dashboards
// are code, imported each deploy, and the metrics stay in the data group's Monitor workspace.

param location string
param environment string
param suffix string
param tags object
param operatorObjectId string
param prometheusWorkspaceId string

var grafanaAdmin = '22926164-76b3-42b3-bc55-97df8dab3e41'

resource grafana 'Microsoft.Dashboard/grafana@2024-10-01' = {
  // At most 23 characters, so it leaves out "windops": amg-staging-<suffix> is 17.
  name: 'amg-${environment}-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    apiKey: 'Disabled'
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
    grafanaIntegrations: {
      azureMonitorWorkspaceIntegrations: [
        { azureMonitorWorkspaceResourceId: prometheusWorkspaceId }
      ]
    }
  }
}

resource operatorGrafana 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(grafana.id, operatorObjectId, grafanaAdmin)
  scope: grafana
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grafanaAdmin)
  }
}

output endpoint string = grafana.properties.endpoint
output principalId string = grafana.identity.principalId
