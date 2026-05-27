// RG-level orchestration for one monitor. Deploy into dc-monitors-rg:
//
//   az deployment group create \
//     --resource-group dc-monitors-rg \
//     --template-file infra/main.bicep \
//     --parameters @infra/main.parameters.json \
//     --parameters appName=status-monitor storageAccountName=dcstatusmonitor \
//       cronSchedule='0 */5 * * * *' entraAppId=<APP_ID> \
//       entraClientSecret=<SECRET> caeResourceId=<CAE_ID>
//
// NOTE: this template only creates resources in the target RG (dc-monitors-rg).
// The AcrPull role assignment on the shared registry lives in acr-pull-role.bicep
// and must be applied separately against dc-zendesk-dispatcher-rg.

param appName string
param location string = resourceGroup().location
param storageAccountName string
param caeResourceId string
param acrLoginServer string = 'dczendeskdispatcheracr.azurecr.io'
param cronSchedule string = '*/5 * * * *'
param entraAppId string
@secure()
param entraClientSecret string
param baseUrl string = ''
param slackDefaultChannel string = ''
param zoomUserJid string = ''

module monitor 'monitor.bicep' = {
  name: 'monitor-${appName}'
  params: {
    appName: appName
    location: location
    storageAccountName: storageAccountName
    caeResourceId: caeResourceId
    acrLoginServer: acrLoginServer
    cronSchedule: cronSchedule
    entraAppId: entraAppId
    entraClientSecret: entraClientSecret
    baseUrl: baseUrl
    slackDefaultChannel: slackDefaultChannel
    zoomUserJid: zoomUserJid
  }
}

output webFqdn string = monitor.outputs.webFqdn
output webPrincipalId string = monitor.outputs.webPrincipalId
output jobPrincipalId string = monitor.outputs.jobPrincipalId
output webAppName string = monitor.outputs.webAppName
output cronJobName string = monitor.outputs.cronJobName
