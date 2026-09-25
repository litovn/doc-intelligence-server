using './main.bicep'

// Values come from the shell: `set -a && . <(tr -d '\r' < .env) && set +a` first.
param appImage = readEnvironmentVariable('APP_IMAGE', '')
param pgAdminPassword = readEnvironmentVariable('PG_ADMIN_PASSWORD')
param openaiApiKey = readEnvironmentVariable('OPENAI_API_KEY')
param embeddingModel = readEnvironmentVariable('EMBEDDING_MODEL')
param chatModel = readEnvironmentVariable('CHAT_MODEL')
param azureDiEndpoint = readEnvironmentVariable('AZURE_DI_ENDPOINT')
param azureDiKey = readEnvironmentVariable('AZURE_DI_KEY')
param mcpApiKeyEmployee = readEnvironmentVariable('MCP_API_KEY_EMPLOYEE')
param mcpApiKeyManager = readEnvironmentVariable('MCP_API_KEY_MANAGER')
