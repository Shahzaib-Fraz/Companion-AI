"""Application Configuration - Fixed (Pydantic v2 compatible)"""
from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from typing import List

class Settings(BaseSettings):
    """Application settings with Pydantic v2 compatible configuration"""
    
    # Database
    DATABASE_URL: str = "postgresql://postgres:root@localhost:5432/ai-companion"
    
    # JWT
    JWT_SECRET: str = "your-secret-key-change-this"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION: int = 3600
    
    # LLM (Groq)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    
    # Vector DB (Qdrant)
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    
    # Email (Mailtrap)
    BREVO_API_KEY: str=""
    BREVO_SENDER_EMAIL: str="worker1062@gmail.com"
    BREVO_SENDER_NAME: str="AI Companion"

    # WhatsApp Cloud API
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_BUSINESS_ACCOUNT_ID: str = ""
    
    # Application
    APP_NAME: str = "AI Companion"
    APP_ENV: str = "development"
    DEBUG: bool = False
    ALLOWED_ORIGINS: List[str] = ["http://localhost:8501", "http://localhost:3000", "http://localhost:8000"]
    
    # Server
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000
    
    # Pydantic v2 configuration - DO NOT use inner Config class!
    model_config = ConfigDict(
        extra='ignore',  # Ignore unknown env vars
        env_file=".env",  # Read from .env file
        case_sensitive=False,  # Allow lowercase env vars
    )

settings = Settings()