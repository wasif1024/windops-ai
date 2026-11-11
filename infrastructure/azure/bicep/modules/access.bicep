// Once the cluster exists: each identity trusts only its own Kubernetes service account, and the
// nodes may pull images. Run again on every azure-up, as a new cluster has a new OIDC issuer.

param oidcIssuerUrl string
param kubeletObjectId string
param registryName string

var workloads = loadJsonContent('../generated/workloads.json')
var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
// The in-cluster job that creates each service's database and role.
var postgresAdmin = { identity: 'id-windops-postgres-admin', namespace: 'windops-data', account: 'windops-postgres-admin' }

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [
  for workload in workloads: {
    name: 'id-${workload.name}'
  }
]

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [
  for (workload, index) in workloads: {
    parent: identities[index]
    name: 'aks'
    properties: {
      issuer: oidcIssuerUrl
      subject: 'system:serviceaccount:${workload.namespace}:${workload.name}'
      audiences: ['api://AzureADTokenExchange']
    }
  }
]

resource postgresAdminIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: postgresAdmin.identity
}

resource postgresAdminFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: postgresAdminIdentity
  name: 'aks'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${postgresAdmin.namespace}:${postgresAdmin.account}'
    audiences: ['api://AzureADTokenExchange']
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, kubeletObjectId, acrPull)
  scope: registry
  properties: {
    principalId: kubeletObjectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPull)
  }
}
