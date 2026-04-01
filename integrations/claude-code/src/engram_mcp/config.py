from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    memory_api_url: str = "http://localhost:8920"
    memory_api_token: str = ""
    memory_namespace: str = "claude-code"
    memory_read_namespaces: str = "claude-code,claude-web,grok"
    memory_default_scope: str = "machine"

    model_config = {"env_prefix": ""}


settings = Settings()
