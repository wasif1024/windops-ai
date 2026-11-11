// The compute group's public addresses: the cluster's outbound address, which Service Bus allows,
// and App Gateway's, the only way in. Standard static IPv4, $0.005 an hour each.

param location string
param environment string
param suffix string
param tags object

resource outbound 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-windops-${environment}-outbound'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource gateway 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-windops-${environment}-gateway'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    // The name is fixed, so its certificate can be made before the address exists.
    dnsSettings: {
      domainNameLabel: 'windops-${environment}-${suffix}'
    }
  }
}

output outboundIpId string = outbound.id
output outboundIpAddress string = outbound.properties.ipAddress
output gatewayIpId string = gateway.id
output gatewayFqdn string = gateway.properties.dnsSettings.fqdn
