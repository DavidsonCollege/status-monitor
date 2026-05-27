// Grants AcrPull on the shared registry to the monitor's web + job managed
// identities. The registry lives in dc-zendesk-dispatcher-rg, so this is applied
// SEPARATELY from main.bicep to keep the main deployment from touching that RG.
//
// Deploy AFTER main.bicep (which creates the identities), passing the principal
// IDs from main.bicep's outputs:
//
//   az deployment group create \
//     --resource-group dc-zendesk-dispatcher-rg \
//     --template-file infra/acr-pull-role.bicep \
//     --parameters acrName=dczendeskdispatcheracr \
//       webPrincipalId=<webPrincipalId> jobPrincipalId=<jobPrincipalId>
//
// Requires the deployer to have role-assignment write on dc-zendesk-dispatcher-rg
// (Nathaniel's x365 account has subscription-scoped User Access Administrator).

param acrName string
param webPrincipalId string
param jobPrincipalId string

var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource acrPullWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, webPrincipalId, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: webPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPullJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, jobPrincipalId, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: jobPrincipalId
    principalType: 'ServicePrincipal'
  }
}
