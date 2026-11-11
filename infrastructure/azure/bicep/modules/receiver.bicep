// A consumer's right to receive: on its own subscriptions only, never the whole namespace.

param namespaceName string
@description('Each as { topic, subscription }.')
param subscriptions array
param principalId string
param roleId string

resource namespace 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  name: namespaceName
}

resource topics 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' existing = [
  for item in subscriptions: {
    parent: namespace
    name: item.topic
  }
]

resource owned 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' existing = [
  for (item, index) in subscriptions: {
    parent: topics[index]
    name: item.subscription
  }
]

resource receive 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (item, index) in subscriptions: {
    name: guid(owned[index].id, principalId, roleId)
    scope: owned[index]
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    }
  }
]
