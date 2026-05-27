// Parameterized module for one monitor: Storage + Key Vault + Container App (web)
// + Container Apps Job (cron) + least-privilege role assignments on the resources
// it owns (Key Vault + Storage). The ACR AcrPull assignment lives in
// acr-pull-role.bicep because the registry is in a different resource group
// (dc-zendesk-dispatcher-rg) and we keep this module from touching that RG.

@description('Logical app name, e.g. status-monitor. Drives resource names.')
param appName string

@description('Azure region. Must match the Container Apps Environment region.')
param location string = resourceGroup().location

@description('Globally-unique storage account name (3-24 lowercase alphanumeric).')
param storageAccountName string

@description('Key Vault name.')
param keyVaultName string = 'dc-${appName}-kv'

@description('Resource ID of the existing shared Container Apps Environment.')
param caeResourceId string

@description('ACR login server, e.g. dczendeskdispatcheracr.azurecr.io.')
param acrLoginServer string

@description('''Container image reference. Defaults to a public Microsoft placeholder
so the Container App + Job can provision a valid first revision BEFORE the real
image exists in ACR (chicken-and-egg: a Container App can't create its initial
revision from an image that can't be pulled, which fails the whole deployment).
The deploy workflow swaps in the real ACR image (status-monitor:<sha>) on first
push, after AcrPull is granted to the managed identities.''')
param image string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Job cron schedule (standard 5-field, Kubernetes-style). Default every 5 minutes.')
param cronSchedule string = '*/5 * * * *'

@description('''Web app ingress target port. Defaults to 80 to match the public
placeholder image so the initial revision passes ingress readiness and reaches
Succeeded. Production is 8000 (the uvicorn port) — deploy.yml flips this on first
real push. If you re-run this template for production, pass targetPort=8000.''')
param targetPort int = 80

@description('''Cron job container command override. Defaults to empty (use the
placeholder image's own entrypoint) so the job provisions cleanly during the
placeholder phase. Production is ["python","-m","cron.run"] — deploy.yml sets it
on first real push. If you re-run this template for production, pass that array.''')
param jobCommand array = []

@description('Entra app (client) ID for Easy Auth.')
param entraAppId string

@description('Entra client secret for Easy Auth.')
@secure()
param entraClientSecret string

@description('Entra tenant ID (issuer).')
param tenantId string = subscription().tenantId

@description('Public base URL used in notifications.')
param baseUrl string = ''

@description('Default Slack channel id (non-secret config).')
param slackDefaultChannel string = ''

@description('Zoom user JID (non-secret config).')
param zoomUserJid string = ''

param minReplicas int = 1
param maxReplicas int = 3

var webAppName = 'dc-${appName}-web'
var cronJobName = 'dc-${appName}-cron'
var clientSecretSettingName = 'aad-client-secret'

// Built-in role definition IDs
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleStorageTableContributor = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var roleStorageBlobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// ── Storage ────────────────────────────────────────────────────────────────
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource tableConfig 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: 'teamsConfig'
}
resource tableState 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: 'state'
}
resource tableChanges 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: 'changeRequests'
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}
resource feedsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'feeds'
  properties: { publicAccess: 'None' }
}

// ── Key Vault ──────────────────────────────────────────────────────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
  }
}

// ── Shared container template (web + job use the same image and env) ─────────
var commonEnv = [
  { name: 'STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'KEY_VAULT_NAME', value: keyVaultName }
  { name: 'BASE_URL', value: baseUrl }
  { name: 'SLACK_DEFAULT_CHANNEL', value: slackDefaultChannel }
  { name: 'ZOOM_USER_JID', value: zoomUserJid }
]

var registries = [
  {
    server: acrLoginServer
    identity: 'system'
  }
]

// Job container: include the command override only when jobCommand is non-empty,
// so the placeholder phase (empty default) uses the placeholder image's own
// entrypoint and provisions cleanly.
var jobContainerBase = {
  name: appName
  image: image
  resources: { cpu: json('0.5'), memory: '1Gi' }
  env: commonEnv
}
var jobContainer = empty(jobCommand) ? jobContainerBase : union(jobContainerBase, { command: jobCommand })

// ── Container App (web / HTTP server) ────────────────────────────────────────
resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: caeResourceId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
      }
      registries: registries
      secrets: [
        { name: clientSecretSettingName, value: entraClientSecret }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: commonEnv
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: maxReplicas }
    }
  }
}

// Easy Auth: allow anonymous through (public dashboard) and inject the Entra
// principal header when present. Per-path enforcement (/admin, /api/admin/*) is
// done in the FastAPI app via app/auth.require_authenticated.
resource webAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: webApp
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'AllowAnonymous' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraAppId
          clientSecretSettingName: clientSecretSettingName
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [ 'api://${entraAppId}' ]
        }
      }
    }
    login: { preserveUrlFragmentsForLogins: false }
  }
}

// ── Container Apps Job (cron) ────────────────────────────────────────────────
resource cronJob 'Microsoft.App/jobs@2024-03-01' = {
  name: cronJobName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: caeResourceId
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 300
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: cronSchedule
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: registries
    }
    template: {
      containers: [ jobContainer ]
    }
  }
}

// ── Role assignments (least privilege) on resources this module owns ─────────
resource kvSecretsUserWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webApp.id, roleKeyVaultSecretsUser)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource kvSecretsUserJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, cronJob.id, roleKeyVaultSecretsUser)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
    principalId: cronJob.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableContribWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, webApp.id, roleStorageTableContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageTableContributor)
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource tableContribJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, cronJob.id, roleStorageTableContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageTableContributor)
    principalId: cronJob.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource blobContribWeb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, webApp.id, roleStorageBlobContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobContributor)
    principalId: webApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
resource blobContribJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, cronJob.id, roleStorageBlobContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobContributor)
    principalId: cronJob.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output webFqdn string = webApp.properties.configuration.ingress.fqdn
output webPrincipalId string = webApp.identity.principalId
output jobPrincipalId string = cronJob.identity.principalId
output webAppName string = webAppName
output cronJobName string = cronJobName
