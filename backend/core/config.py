import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MONGO_URL: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    DB_NAME: str = os.getenv("DB_NAME", "evalassist")
    JWT_SECRET: str = os.getenv("JWT_SECRET", "supersecretkey_change_in_prod")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 1 week
    CORS_ORIGINS: list = ["*"]
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    VISION_MODEL: str = os.getenv("VISION_MODEL", "google/gemini-2.0-flash-001")
    TEXT_MODEL: str = os.getenv("TEXT_MODEL", "google/gemini-flash-1.5")

    class Config:
        env_file = ".env"

settings = Settings()
