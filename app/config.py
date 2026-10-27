from pathlib import Path
import secrets

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    database_url: str | None = None
    # Agency (offline) mode: the Render database the sync worker replicates with, the frontend build this server
    # serves at "/", and how often the worker syncs. All unset on Render itself.
    remote_database_url: str | None = None
    frontend_dir: str | None = None
    sync_interval_seconds: int = 15
    db_schema: str = "dbo"
    db_table: str = "Sinistre"
    db_client_table: str = "TClient"
    db_user_table: str = "RUtilisateur_Depot"
    db_prestataire_table: str = "TPrestataire"
    db_prestataire_branch_table: str = "RPrestataireBranche"
    db_prestataire_company_table: str = "RPrestataireCompagnie"
    db_family_prestataire_table: str = "TFamille_Prestataire"
    db_region_table: str = "TRegion"
    db_company_table: str = "TCompagnie"
    db_branch_type_table: str = "TType_Branche"
    db_branch_table: str = "TBranche"
    db_document_table: str = "TDocument"
    db_unpaid_table: str = "TTempEtatImpaye"
    # AI assistant (set ANTHROPIC_API_KEY on the server; ASSISTANT_MODEL overrides the model).
    anthropic_api_key: str | None = None
    # Free alternative for reading scanned files only (Google AI Studio key); GEMINI_MODEL can be changed if Google renames it.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    # Free OCR (https://ocr.space/ocrapi/freekey): "helloworld" is a shared test key, set OCR_SPACE_API_KEY for your own.
    ocr_space_api_key: str = "helloworld"
    assistant_model: str = "claude-opus-5-5"
    # Predefined administrator, authenticated without the user table (override with ADMIN_PASSWORD).
    admin_username: str = "admin"
    admin_password: str = "123"
    auth_secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    @field_validator("auth_secret_key")
    @classmethod
    def validate_auth_secret_key(cls, value: str) -> str:
        if len(value) < 32 or value == "replace_with_a_long_random_secret":
            raise ValueError("AUTH_SECRET_KEY must be at least 32 characters and randomly generated")
        return value

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()



