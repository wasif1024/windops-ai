// The compute resource group: AKS and App Gateway, created for a demo and deleted after.

param location string
param environment string
param tags object
param kubernetesVersion string
param operatorIpRanges array
param operatorObjectId string
param aksSubnetId string
param appGatewaySubnetId string
param controlPlaneIdentityId string
param appGatewayIdentityId string
param outboundIpId string
param gatewayIpId string
param logAnalyticsId string
param prometheusRuleId string
param prometheusEndpointId string
param containerInsightsRuleId string
param deployGateway bool
param gatewayCertificateUri string
param gatewayTrustedRootCertificate string
param gatewayInternalIp string

var clusterAdmin = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b' // Azure Kubernetes Service RBAC Cluster Admin
// APISIX's certificate names, from services/api-gateway/deploy/certificate.yaml.
var gatewayHostName = 'windops-gateway.windops-system.svc'
var gatewayName = 'agw-windops-${environment}'

resource cluster 'Microsoft.ContainerService/managedClusters@2025-05-01' = {
  name: 'aks-windops-${environment}'
  location: location
  tags: tags
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${controlPlaneIdentityId}': {}
    }
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: 'windops-${environment}'
    nodeResourceGroup: 'rg-windops-${environment}-nodes'
    agentPoolProfiles: [
      {
        // System pods only: everything of ours runs on the user pool.
        name: 'system'
        mode: 'System'
        vmSize: 'Standard_D2ads_v5'
        count: 1
        osType: 'Linux'
        osSKU: 'AzureLinux'
        osDiskType: 'Ephemeral'
        osDiskSizeGB: 64
        vnetSubnetID: aksSubnetId
        nodeTaints: ['CriticalAddonsOnly=true:NoSchedule']
        upgradeSettings: { maxSurge: '1' }
      }
      {
        name: 'user'
        mode: 'User'
        vmSize: 'Standard_D4ads_v5'
        count: 1
        enableAutoScaling: true
        minCount: 1
        maxCount: 2
        osType: 'Linux'
        osSKU: 'AzureLinux'
        osDiskType: 'Ephemeral'
        osDiskSizeGB: 128
        vnetSubnetID: aksSubnetId
        upgradeSettings: { maxSurge: '1' }
      }
    ]
    // Azure CNI Overlay powered by Cilium: the same network policy engine as the local cluster.
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      podCidr: '192.168.0.0/16'
      serviceCidr: '10.0.0.0/16'
      dnsServiceIP: '10.0.0.10'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      loadBalancerProfile: {
        outboundIPs: {
          publicIPs: [{ id: outboundIpId }]
        }
      }
    }
    // Kubernetes access through Entra ID and Azure RBAC only: no local admin credentials.
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      tenantID: tenant().tenantId
    }
    disableLocalAccounts: true
    apiServerAccessProfile: {
      authorizedIPRanges: operatorIpRanges
    }
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
      imageCleaner: {
        enabled: true
        intervalHours: 168
      }
      defender: {
        logAnalyticsWorkspaceResourceId: logAnalyticsId
        securityMonitoring: {
          enabled: true
        }
      }
    }
    addonProfiles: {
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
          rotationPollInterval: '5m'
        }
      }
      azurepolicy: {
        enabled: true
      }
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalyticsId
          useAADAuth: 'true'
        }
      }
    }
    azureMonitorProfile: {
      metrics: {
        enabled: true
      }
    }
    autoUpgradeProfile: {
      upgradeChannel: 'patch'
      nodeOSUpgradeChannel: 'NodeImage'
    }
  }
}

resource prometheusEndpointLink 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'configurationAccessEndpoint'
  scope: cluster
  properties: {
    dataCollectionEndpointId: prometheusEndpointId
  }
}

resource prometheusLink 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'windops-prometheus'
  scope: cluster
  properties: {
    dataCollectionRuleId: prometheusRuleId
  }
}

resource containerInsightsLink 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: 'ContainerInsightsExtension'
  scope: cluster
  properties: {
    dataCollectionRuleId: containerInsightsRuleId
  }
}

resource operatorAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(cluster.id, operatorObjectId, clusterAdmin)
  scope: cluster
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', clusterAdmin)
  }
}

// ----------------------------------------------------------------------------------- App Gateway

resource firewall 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = if (deployGateway) {
  name: 'waf-windops-${environment}'
  location: location
  tags: tags
  properties: {
    policySettings: {
      state: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: true
      maxRequestBodySizeInKb: 128
      fileUploadLimitInMb: 100
    }
    managedRules: {
      managedRuleSets: [
        {
          ruleSetType: 'Microsoft_DefaultRuleSet'
          ruleSetVersion: '2.1'
        }
        {
          ruleSetType: 'Microsoft_BotManagerRuleSet'
          ruleSetVersion: '1.1'
        }
      ]
    }
  }
}

resource gateway 'Microsoft.Network/applicationGateways@2024-05-01' = if (deployGateway) {
  name: gatewayName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appGatewayIdentityId}': {}
    }
  }
  properties: {
    sku: {
      name: 'WAF_v2'
      tier: 'WAF_v2'
    }
    autoscaleConfiguration: {
      minCapacity: 0
      maxCapacity: 2
    }
    firewallPolicy: { id: firewall.id }
    // TLS 1.2 and later, with Microsoft's current strong ciphers.
    sslPolicy: {
      policyType: 'Predefined'
      policyName: 'AppGwSslPolicy20220101S'
    }
    gatewayIPConfigurations: [
      { name: 'gateway', properties: { subnet: { id: appGatewaySubnetId } } }
    ]
    frontendIPConfigurations: [
      { name: 'public', properties: { publicIPAddress: { id: gatewayIpId } } }
    ]
    frontendPorts: [
      { name: 'https', properties: { port: 443 } }
      { name: 'http', properties: { port: 80 } }
    ]
    sslCertificates: [
      { name: 'public', properties: { keyVaultSecretId: gatewayCertificateUri } }
    ]
    trustedRootCertificates: [
      { name: 'cluster-ca', properties: { data: gatewayTrustedRootCertificate } }
    ]
    backendAddressPools: [
      { name: 'apisix', properties: { backendAddresses: [{ ipAddress: gatewayInternalIp }] } }
    ]
    probes: [
      {
        name: 'apisix'
        properties: {
          protocol: 'Https'
          host: gatewayHostName
          path: '/'
          interval: 30
          timeout: 10
          unhealthyThreshold: 3
          // APISIX answers an unknown path with 404, which still shows it is serving.
          match: { statusCodes: ['200-499'] }
        }
      }
    ]
    // TLS again from App Gateway to APISIX, verified against the cluster's CA.
    backendHttpSettingsCollection: [
      {
        name: 'apisix'
        properties: {
          port: 443
          protocol: 'Https'
          hostName: gatewayHostName
          requestTimeout: 60
          trustedRootCertificates: [
            { id: resourceId('Microsoft.Network/applicationGateways/trustedRootCertificates', gatewayName, 'cluster-ca') }
          ]
          probe: { id: resourceId('Microsoft.Network/applicationGateways/probes', gatewayName, 'apisix') }
        }
      }
    ]
    httpListeners: [
      {
        name: 'https'
        properties: {
          protocol: 'Https'
          frontendIPConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', gatewayName, 'public') }
          frontendPort: { id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', gatewayName, 'https') }
          sslCertificate: { id: resourceId('Microsoft.Network/applicationGateways/sslCertificates', gatewayName, 'public') }
        }
      }
      {
        name: 'http'
        properties: {
          protocol: 'Http'
          frontendIPConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', gatewayName, 'public') }
          frontendPort: { id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', gatewayName, 'http') }
        }
      }
    ]
    redirectConfigurations: [
      {
        name: 'to-https'
        properties: {
          redirectType: 'Permanent'
          targetListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', gatewayName, 'https') }
          includePath: true
          includeQueryString: true
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'https'
        properties: {
          ruleType: 'Basic'
          priority: 100
          httpListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', gatewayName, 'https') }
          backendAddressPool: { id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', gatewayName, 'apisix') }
          backendHttpSettings: { id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', gatewayName, 'apisix') }
        }
      }
      {
        name: 'http'
        properties: {
          ruleType: 'Basic'
          priority: 200
          httpListener: { id: resourceId('Microsoft.Network/applicationGateways/httpListeners', gatewayName, 'http') }
          redirectConfiguration: { id: resourceId('Microsoft.Network/applicationGateways/redirectConfigurations', gatewayName, 'to-https') }
        }
      }
    ]
  }
}

output clusterName string = cluster.name
output oidcIssuerUrl string = cluster.properties.oidcIssuerProfile.issuerURL
output kubeletObjectId string = cluster.properties.identityProfile.kubeletidentity.objectId
