from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str

    # OpenAI 
    openai_base_url: str | None = None
    openai_api_key: str
    embedding_model: str = "text-embedding-3-small"
    chat_model: str = "gpt-5.4-mini"

    # Azure Document Intelligence
    azure_di_endpoint: str 
    azure_di_key: str 

    # MCP auth
    mcp_api_key_employee: str
    mcp_api_key_manager: str
    mcp_rate_limit_per_minute: int = 300
    public_host: str = "localhost:8000"

    # RAG
    relevance_floor: float = 0.37
    hybrid_search: bool = False


settings = Settings()