from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str
    gemini_model_fast: str = "gemini-3.8-flash"
    gemini_model_audit: str = "gemini-3.1-pro-preview"

    database_url: str
    google_maps_api_key: str | None = None

    base_cidade: str = "Vinhedo"
    base_uf: str = "SP"
    raio_maximo_km: float = 500.0

    margem_min_4x4: float = 25.0
    margem_min_caminhao: float = 25.0
    margem_min_maquina: float = 28.0
    margem_min_imovel: float = 22.0


settings = Settings()
