// What Managed Grafana may read in the data group. Each deploy makes a new Grafana, with a new
// identity, so each assignment is named after the identity, and `make azure-down` removes them.

param grafanaPrincipalId string
param prometheusWorkspaceName string

var monitoringDataReader = 'b0d8363b-8ddd-447d-831f-62ca05bff136'
var monitoringReader = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

resource prometheus 'Microsoft.Monitor/accounts@2023-04-03' existing = {
  name: prometheusWorkspaceName
}

resource readsPrometheus 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(prometheus.id, grafanaPrincipalId, monitoringDataReader)
  scope: prometheus
  properties: {
    principalId: grafanaPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringDataReader)
  }
}

// For Service Bus's metrics and the logs, on this group only.
resource readsGroup 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, grafanaPrincipalId, monitoringReader)
  properties: {
    principalId: grafanaPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReader)
  }
}
