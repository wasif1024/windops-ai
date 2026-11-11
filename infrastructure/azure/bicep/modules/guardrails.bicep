// Subscription-wide: the budget that warns before the bill grows, and Defender for Containers.
targetScope = 'subscription'

param environment string
param alertEmail string
param budgetAmount int
param budgetStartDate string
param enableDefender bool

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-windops-${environment}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    // Only what this project creates, by its tag.
    filter: {
      tags: {
        name: 'project'
        operator: 'In'
        values: ['windops']
      }
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [alertEmail]
      }
    }
  }
}

// Billed per vCore while nodes run: about $0.06 an hour for this cluster, nothing when it is gone.
resource defender 'Microsoft.Security/pricings@2024-01-01' = if (enableDefender) {
  name: 'Containers'
  properties: {
    pricingTier: 'Standard'
  }
}
