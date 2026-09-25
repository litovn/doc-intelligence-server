// Azure: Postgres + ACR + Container Apps. First run with APP_IMAGE unset creates the infra;
// push the image, then run again with APP_IMAGE set to create/update the app. See README "Deploy to Azure".
param location string = 'italynorth'
param appName string = 'indigo-kb'
@description('Full image ref, e.g. <acr>.azurecr.io/indigo-kb:v1. Empty = infra only.')
param appImage string = ''
@secure()
@description('URL-safe (hex): it is embedded in DATABASE_URL.')
param pgAdminPassword string
@secure()
param openaiApiKey string
param embeddingModel string
param chatModel string
param azureDiEndpoint string
@secure()
param azureDiKey string
@secure()
param mcpApiKeyEmployee string
@secure()
param mcpApiKeyManager string

var suffix = uniqueString(resourceGroup().id)
var pgAdmin = 'kbadmin'

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: '${appName}-pg-${suffix}'
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '17'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: 32 }
    highAvailability: { mode: 'Disabled' }
  }
}

// pgvector must be allowlisted before `CREATE EXTENSION vector` (app/rag/db.py).
resource pgVector 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: pg
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
}

// 0.0.0.0 = "allow Azure services": consumption Container Apps have no static outbound IP.
// ponytail: public PG + password; VNet-integrated env + private access if this outlives the demo.
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2025-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
  dependsOn: [pgVector] // the server rejects concurrent child updates
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'indigokb${suffix}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource env 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

var publicHost = '${appName}.${env.properties.defaultDomain}'

resource app 'Microsoft.App/containerApps@2026-01-01' = if (!empty(appImage)) {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000 }
      registries: [{ server: acr.properties.loginServer, username: acr.listCredentials().username, passwordSecretRef: 'acr-password' }]
      secrets: [
        { name: 'acr-password', value: acr.listCredentials().passwords[0].value }
        { name: 'database-url', value: 'postgresql://${pgAdmin}:${pgAdminPassword}@${pg.properties.fullyQualifiedDomainName}:5432/postgres?sslmode=require' }
        { name: 'openai-api-key', value: openaiApiKey }
        { name: 'azure-di-key', value: azureDiKey }
        { name: 'mcp-api-key-employee', value: mcpApiKeyEmployee }
        { name: 'mcp-api-key-manager', value: mcpApiKeyManager }
      ]
    }
    template: {
      containers: [{
        name: appName
        image: appImage
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: [
          { name: 'DATABASE_URL', secretRef: 'database-url' }
          { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
          { name: 'EMBEDDING_MODEL', value: embeddingModel }
          { name: 'CHAT_MODEL', value: chatModel }
          { name: 'AZURE_DI_ENDPOINT', value: azureDiEndpoint }
          { name: 'AZURE_DI_KEY', secretRef: 'azure-di-key' }
          { name: 'MCP_API_KEY_EMPLOYEE', secretRef: 'mcp-api-key-employee' }
          { name: 'MCP_API_KEY_MANAGER', secretRef: 'mcp-api-key-manager' }
          { name: 'PUBLIC_HOST', value: publicHost } // else /mcp answers 421
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 } // ingestion runs in-process (README §9)
    }
  }
}

output acrLoginServer string = acr.properties.loginServer
output pgHost string = pg.properties.fullyQualifiedDomainName
output url string = 'https://${publicHost}'
