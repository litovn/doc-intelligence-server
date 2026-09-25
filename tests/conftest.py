import os

# `app.config.Settings` reads these at import. The tests never reach a database, a model or Azure.
for name, value in {
    "DATABASE_URL": "postgresql://unused",
    "OPENAI_API_KEY": "unused",
    "AZURE_DI_ENDPOINT": "https://unused",
    "AZURE_DI_KEY": "unused",
    "MCP_API_KEY_EMPLOYEE": "unused",
    "MCP_API_KEY_MANAGER": "unused",
}.items():
    os.environ.setdefault(name, value)
