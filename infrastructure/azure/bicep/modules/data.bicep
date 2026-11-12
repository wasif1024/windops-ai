// The data resource group: everything that outlives a demo (docs/azure-plan.md).

param location string
param environment string
param suffix string
param tags object
@description('Allowed to Service Bus: the operator, and the cluster\'s outbound address when it exists.')
param allowedIpRanges array
@description('Allowed to Key Vault from outside the network: the operator only.')
param operatorIpRanges array
@description('Each as { principalId, principalType }: the operator, and the pipeline when it deploys.')
param admins array
param alertEmail string

// Made from the code by scripts/generate/azure_foundation.py, so Azure matches the cluster.
var workloads = loadJsonContent('../generated/workloads.json')
var topology = loadJsonContent('../generated/servicebus.json')
var ruleGroups = loadJsonContent('../../monitoring/prometheus-rule-groups.json')
var serviceBusAlerts = loadJsonContent('../../monitoring/servicebus-alerts.json')

// Built-in roles (https://learn.microsoft.com/azure/role-based-access-control/built-in-roles). The vault
// ones are Key Vault Secrets User, Key Vault Secrets Officer and Key Vault Certificates Officer.
var roles = {
  serviceBusSender: '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
  serviceBusReceiver: '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
  serviceBusOwner: '090c5cfd-751d-490a-894a-3ce6f1109419'
  vaultReader: '4633458b-17de-408a-b874-0445c86b69e6'
  vaultWriter: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  vaultCertificateWriter: 'a4417e6f-fecd-4de8-b567-7b0420556985'
  networkContributor: '4d97b98b-1d4f-4787-a291-c67834d212e7'
}
var keyVaultName = 'kv-windops-${environment}-${suffix}'

// ------------------------------------------------------------------------------------- network

resource aksNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-windops-${environment}-aks'
  location: location
  tags: tags
}

resource postgresNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-windops-${environment}-postgres'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'postgres-from-aks-only'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '10.40.4.0/22'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '5432'
        }
      }
      {
        name: 'deny-other-vnet-inbound'
        properties: {
          priority: 4000
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

// App Gateway v2 needs GatewayManager's ports and Azure's load balancer, and serves 80 and 443.
resource gatewayNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-windops-${environment}-appgw'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'https-and-redirect-from-internet'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRanges: ['80', '443']
        }
      }
      {
        name: 'gateway-manager'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'GatewayManager'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '65200-65535'
        }
      }
      {
        name: 'azure-load-balancer'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-windops-${environment}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.40.0.0/16']
    }
    subnets: [
      {
        name: 'snet-appgw'
        properties: {
          addressPrefix: '10.40.0.0/24'
          networkSecurityGroup: { id: gatewayNsg.id }
          // App Gateway reads its certificate from Key Vault, through the vault's firewall.
          serviceEndpoints: [{ service: 'Microsoft.KeyVault' }]
        }
      }
      {
        // Nodes only: pods get overlay addresses from Cilium, outside this network.
        name: 'snet-aks'
        properties: {
          addressPrefix: '10.40.4.0/22'
          networkSecurityGroup: { id: aksNsg.id }
          serviceEndpoints: [{ service: 'Microsoft.KeyVault' }]
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: '10.40.8.0/28'
          networkSecurityGroup: { id: postgresNsg.id }
          delegations: [
            {
              name: 'postgres'
              properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
            }
          ]
        }
      }
    ]
  }
}

// ------------------------------------------------------------------------------------ identities

// One per workload, federated to its own service account once the cluster exists (access.bicep).
resource workloadIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for workload in workloads: {
    name: 'id-${workload.name}'
    location: location
    tags: tags
  }
]

// Runs the in-cluster job that creates each service's database and role: PostgreSQL's Entra admin.
resource postgresAdmin 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-windops-postgres-admin'
  location: location
  tags: tags
}

resource controlPlane 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-windops-${environment}-aks'
  location: location
  tags: tags
}

resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-windops-${environment}-appgw'
  location: location
  tags: tags
}

// The cluster manages load balancers in its own subnet, such as APISIX's internal one.
resource controlPlaneNetwork 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(network.id, controlPlane.id, roles.networkContributor)
  scope: network
  properties: {
    principalId: controlPlane.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.networkContributor)
  }
}

// ----------------------------------------------------------------------------------- Service Bus

resource serviceBus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-windops-${environment}-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    // Entra only: no shared access keys exist to leak.
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource serviceBusNetwork 'Microsoft.ServiceBus/namespaces/networkRuleSets@2024-01-01' = {
  parent: serviceBus
  name: 'default'
  properties: {
    publicNetworkAccess: 'Enabled'
    defaultAction: 'Deny'
    trustedServiceAccessEnabled: false
    ipRules: [for range in allowedIpRanges: { ipMask: range, action: 'Allow' }]
  }
}

resource topics 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' = [
  for topic in topology.topics: {
    parent: serviceBus
    name: topic.name
    properties: {
      requiresDuplicateDetection: true
      duplicateDetectionHistoryTimeWindow: topic.duplicateWindow
      defaultMessageTimeToLive: topic.messageTimeToLive
      maxSizeInMegabytes: topic.maxSizeInMegabytes
    }
  }
]

resource subscriptions 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = [
  for (subscription, index) in topology.subscriptions: {
    name: '${serviceBus.name}/${subscription.topic}/${subscription.name}'
    properties: {
      maxDeliveryCount: subscription.maxDeliveryCount
      lockDuration: subscription.lockDuration
      deadLetteringOnMessageExpiration: true
    }
    dependsOn: [topics]
  }
]

// Replaces the catch-all rule, so each consumer keeps only its own subjects.
resource subscriptionRules 'Microsoft.ServiceBus/namespaces/topics/subscriptions/rules@2024-01-01' = [
  for (subscription, index) in topology.subscriptions: {
    parent: subscriptions[index]
    name: '$Default'
    properties: {
      filterType: 'SqlFilter'
      sqlFilter: {
        sqlExpression: subscription.filter
      }
    }
  }
]

resource senders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (workload, index) in workloads: if (workload.events) {
    name: guid(serviceBus.id, workload.name, roles.serviceBusSender)
    scope: serviceBus
    properties: {
      principalId: workloadIdentities[index].properties.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusSender)
    }
  }
]

// A consumer may receive from its own subscription, and nothing else.
module receivers 'receiver.bicep' = [
  for (workload, index) in workloads: if (!empty(workload.receives)) {
    name: 'receiver-${workload.name}'
    params: {
      namespaceName: serviceBus.name
      subscriptions: workload.receives
      principalId: workloadIdentities[index].properties.principalId
      roleId: roles.serviceBusReceiver
    }
    dependsOn: [subscriptions]
  }
]

// The operator and the pipeline run make servicebus-check from outside the cluster.
resource adminServiceBus 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for admin in admins: {
    name: guid(serviceBus.id, admin.principalId, roles.serviceBusOwner)
    scope: serviceBus
    properties: {
      principalId: admin.principalId
      principalType: admin.principalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusOwner)
    }
  }
]

// ------------------------------------------------------------------------------------ PostgreSQL

resource postgresDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'windops-${environment}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresDns
  name: 'vnet-windops-${environment}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: network.id }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: 'psql-windops-${environment}-${suffix}'
  location: location
  tags: tags
  // General Purpose, as new subscriptions in Europe may not create Burstable servers: Azure refused
  // B2s here. It runs only with the compute, as `make azure-down` and the nightly pipeline stop it.
  sku: {
    name: 'Standard_D2ds_v5'
    tier: 'GeneralPurpose'
  }
  properties: {
    version: '18'
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    // Private access only: an address in its delegated subnet, and none on the internet.
    network: {
      delegatedSubnetResourceId: network.properties.subnets[2].id
      privateDnsZoneArmResourceId: postgresDns.id
      publicNetworkAccess: 'Disabled'
    }
    // Entra sign-in only. No password exists, for an administrator or a service.
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
  }
  dependsOn: [postgresDnsLink]
}

module postgresAdministrator 'postgres-admin.bicep' = {
  name: 'postgres-admin'
  params: {
    serverName: postgres.name
    principalId: postgresAdmin.properties.principalId
    principalName: postgresAdmin.name
  }
}

// ------------------------------------------------------------------------------------- Key Vault

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: [for range in operatorIpRanges: { value: range }]
      virtualNetworkRules: [
        { id: network.properties.subnets[0].id }
        { id: network.properties.subnets[1].id }
      ]
    }
  }
}

resource workloadSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (workload, index) in workloads: if (workload.keyVault) {
    name: guid(vault.id, workload.name, roles.vaultReader)
    scope: vault
    properties: {
      principalId: workloadIdentities[index].properties.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.vaultReader)
    }
  }
]

resource gatewaySecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, gatewayIdentity.id, roles.vaultReader)
  scope: vault
  properties: {
    principalId: gatewayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.vaultReader)
  }
}

// Each admin, for each of the two vault roles: they make the CA and App Gateway's certificate.
var vaultGrants = flatten(map(admins, admin => [
  { admin: admin, role: roles.vaultWriter }
  { admin: admin, role: roles.vaultCertificateWriter }
]))

resource adminSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for grant in vaultGrants: {
    name: guid(vault.id, grant.admin.principalId, grant.role)
    scope: vault
    properties: {
      principalId: grant.admin.principalId
      principalType: grant.admin.principalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grant.role)
    }
  }
]

// ------------------------------------------------------------------------------------- registry

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrwindops${environment}${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    // No admin user: the nodes pull with their own identity (access.bicep). Basic has no
    // anonymous pull either.
    adminUserEnabled: false
  }
}

// ----------------------------------------------------------------------------------- monitoring

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-windops-${environment}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    // A cap, so a noisy day cannot run up the bill.
    workspaceCapping: {
      dailyQuotaGb: 2
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-windops-${environment}'
  location: location
  tags: tags
  kind: 'other'
  properties: {
    Application_Type: 'other'
    WorkspaceResourceId: logs.id
    IngestionMode: 'LogAnalytics'
  }
}

// The Collector reads it from Key Vault (M4A.5). It identifies where traces go, and can only send.
resource insightsConnection 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'appinsights-connection-string'
  properties: {
    value: insights.properties.ConnectionString
  }
}

resource prometheus 'Microsoft.Monitor/accounts@2023-04-03' = {
  name: 'amw-windops-${environment}'
  location: location
  tags: tags
}

resource prometheusEndpoint 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' = {
  name: 'dce-windops-${environment}-prometheus'
  location: location
  tags: tags
  kind: 'Linux'
  properties: {}
}

resource prometheusRule 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-windops-${environment}-prometheus'
  location: location
  tags: tags
  kind: 'Linux'
  properties: {
    dataCollectionEndpointId: prometheusEndpoint.id
    dataSources: {
      prometheusForwarder: [
        {
          name: 'PrometheusDataSource'
          streams: ['Microsoft-PrometheusMetrics']
          labelIncludeFilter: {}
        }
      ]
    }
    destinations: {
      monitoringAccounts: [
        {
          accountResourceId: prometheus.id
          name: 'MonitoringAccount'
        }
      ]
    }
    dataFlows: [
      {
        destinations: ['MonitoringAccount']
        streams: ['Microsoft-PrometheusMetrics']
      }
    ]
  }
}

// Container insights' cost-optimized set: logs and events. Prometheus already has the metrics.
resource containerInsightsRule 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-windops-${environment}-containers'
  location: location
  tags: tags
  kind: 'Linux'
  properties: {
    dataSources: {
      extensions: [
        {
          name: 'ContainerInsightsExtension'
          extensionName: 'ContainerInsights'
          streams: ['Microsoft-ContainerLogV2', 'Microsoft-KubeEvents', 'Microsoft-KubePodInventory']
          extensionSettings: {
            dataCollectionSettings: {
              interval: '1m'
              namespaceFilteringMode: 'Off'
              enableContainerLogV2: true
            }
          }
        }
      ]
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: logs.id
          name: 'LogAnalytics'
        }
      ]
    }
    dataFlows: [
      {
        streams: ['Microsoft-ContainerLogV2', 'Microsoft-KubeEvents', 'Microsoft-KubePodInventory']
        destinations: ['LogAnalytics']
      }
    ]
  }
}

resource alerts 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-windops-${environment}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'windops'
    enabled: true
    emailReceivers: [
      {
        name: 'operator'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// Our Prometheus rules, converted from the local stack's (M4A.5).
resource prometheusAlerts 'Microsoft.AlertsManagement/prometheusRuleGroups@2023-03-01' = [
  for group in ruleGroups.groups: {
    name: 'prg-windops-${environment}-${group.name}'
    location: location
    tags: tags
    properties: {
      enabled: true
      interval: group.interval
      scopes: [prometheus.id]
      rules: [
        for rule in group.rules: {
          alert: rule.alert
          expression: rule.expression
          for: rule.for
          severity: rule.severity
          labels: rule.labels
          annotations: rule.annotations
          enabled: rule.enabled
          actions: [{ actionGroupId: alerts.id }]
        }
      ]
    }
  }
]

// Service Bus's own metrics, in place of the JetStream alerts.
resource serviceBusMetricAlerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [
  for alert in serviceBusAlerts.alerts: {
    name: '${alert.name}-${environment}'
    location: 'global'
    tags: tags
    properties: {
      description: alert.description
      severity: alert.severity
      enabled: true
      scopes: [serviceBus.id]
      evaluationFrequency: alert.evaluationFrequency
      windowSize: alert.windowSize
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allOf: [
          {
            criterionType: 'StaticThresholdCriterion'
            name: alert.criterion.metricName
            metricName: alert.criterion.metricName
            metricNamespace: 'Microsoft.ServiceBus/namespaces'
            operator: alert.criterion.operator
            threshold: alert.criterion.threshold
            timeAggregation: alert.criterion.timeAggregation
          }
        ]
      }
      actions: [{ actionGroupId: alerts.id }]
    }
  }
]

// --------------------------------------------------------------------------------------- outputs

output aksSubnetId string = network.properties.subnets[1].id
output appGatewaySubnetId string = network.properties.subnets[0].id
output controlPlaneIdentityId string = controlPlane.id
output appGatewayIdentityId string = gatewayIdentity.id
output logAnalyticsId string = logs.id
output prometheusRuleId string = prometheusRule.id
output prometheusEndpointId string = prometheusEndpoint.id
output containerInsightsRuleId string = containerInsightsRule.id
output keyVaultName string = vault.name
output registryName string = registry.name
output serviceBusHost string = '${serviceBus.name}.servicebus.windows.net'
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output postgresAdminClientId string = postgresAdmin.properties.clientId
output applicationInsightsName string = insights.name
output prometheusWorkspaceName string = prometheus.name
output workloadClientIds array = [
  for (workload, index) in workloads: {
    name: workload.name
    clientId: workloadIdentities[index].properties.clientId
    // Each service's PostgreSQL login is made for this, not the name (windops_migrations.provision).
    principalId: workloadIdentities[index].properties.principalId
  }
]
