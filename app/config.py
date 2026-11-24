from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str
    gemini_model_fast: str = "gemini-3.8-flash"
    gemini_model_audit: str = "gemini-3.8-flash"
    gemini_fallback_models: str = "gemini-3.8-flash-lite"

    database_url: str
    google_maps_api_key: str | None = None

    base_cidade: str = "Vinhedo"
    base_uf: str = "SP"
    raio_maximo_km: float = 500.0

    margem_min_4x4: float = 25.0
    margem_min_caminhao: float = 25.0
    margem_min_maquina: float = 28.0
    margem_min_imovel: float = 22.0

    def obter_cadeia_modelos(self, modelo_preferencial: str | None = None) -> list[str]:
        brutos = [
            modelo_preferencial,
            self.gemini_model_fast,
            self.gemini_model_audit,
            *[m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()],
        ]
        vistos: list[str] = []
        for item in brutos:
            if not item:
                continue
            nome = item.replace("models/", "").strip()
            if nome and nome not in vistos:
                vistos.append(nome)
        return vistos


settings = Settings()
