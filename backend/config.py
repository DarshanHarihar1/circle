from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    DATABASE_URL: str

    # Swiggy MCP
    SWIGGY_MCP_BASE_URL: str = "https://mcp.swiggy.com"
    SWIGGY_REDIRECT_URI: str = "http://localhost:8000/auth/callback"

    # Token vault
    VAULT_FERNET_KEY: str

    # OpenRouter
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # App
    SECRET_KEY: str
    FRONTEND_URL: str = "http://localhost:3000"


settings = Settings()
