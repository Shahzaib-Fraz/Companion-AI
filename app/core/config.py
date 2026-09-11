"""Application Configuration - Fixed (Pydantic v2 compatible)"""
from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator
from typing import List

class Settings(BaseSettings):
    """Application settings with Pydantic v2 compatible configuration"""
    
    # Database
    DATABASE_URL: str = "postgresql://postgres:root@localhost:5432/ai-companion"
    
    # JWT
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION: int = 3600
    
    # LLM (Groq)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_REASONING_EFFORT: str = "low"
    
    # Vector DB (Qdrant)
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    
    # ✅ FIXED Issue #25: Embedding config (now properly loaded from .env)
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384
    
    # ✅ FIXED Issue #23: Memory similarity threshold
    MEMORY_SIMILARITY_THRESHOLD: float = 0.70
    
    # ✅ FIXED Issues #21-22: Memory extraction
    ENABLE_MEMORY_EXTRACTION: bool = True
    MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD: float = 0.80
    
    # ✅ FIXED Issue #24: Treat memory as untrusted
    TREAT_MEMORY_AS_UNTRUSTED: bool = True
    
    # Email (Brevo)
    BREVO_API_KEY: str = ""
    BREVO_SENDER_EMAIL: str = "worker1062@gmail.com"
    BREVO_SENDER_NAME: str = "AI Companion"

    # WhatsApp Cloud API
    WHATSAPP_APP_SECRET: str = ""
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
    
    # Pydantic v2 configuration
    model_config = ConfigDict(
        extra='ignore',
        env_file=".env",
        case_sensitive=False,
    )
    
    # ✅ FIXED Issue #26: Validators
    @field_validator('MEMORY_SIMILARITY_THRESHOLD')
    @classmethod
    def validate_similarity_threshold(cls, v):
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"MEMORY_SIMILARITY_THRESHOLD must be 0.0-1.0, got {v}")
        return float(v)
    
    @field_validator('MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD')
    @classmethod
    def validate_confidence_threshold(cls, v):
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD must be 0.0-1.0, got {v}")
        return float(v)
    
    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def validate_embedding_dimension(cls, v):
        if v <= 0:
            raise ValueError(f"EMBEDDING_DIMENSION must be positive, got {v}")
        return v
    
    @property
    def embedding_config_valid(self) -> bool:
        return (self.EMBEDDING_MODEL and len(self.EMBEDDING_MODEL) > 0 
                and self.EMBEDDING_DIMENSION > 0)
    
    @property
    def memory_config_valid(self) -> bool:
        return (0.0 <= self.MEMORY_SIMILARITY_THRESHOLD <= 1.0
                and 0.0 <= self.MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD <= 1.0)
    
    def validate_all(self) -> List[str]:
        errors = []
        if not self.embedding_config_valid:
            errors.append(f"❌ Invalid embedding config: {self.EMBEDDING_MODEL}={self.EMBEDDING_DIMENSION}")
        if not self.memory_config_valid:
            errors.append(f"❌ Invalid memory thresholds")
        if not self.GROQ_API_KEY:
            errors.append("❌ GROQ_API_KEY is required")
        if not self.JWT_SECRET:
            errors.append("❌ JWT_SECRET is required")
        return errors


settings = Settings()


def validate_settings_on_startup() -> None:
    errors = settings.validate_all()
    if errors:
        raise ValueError("Configuration validation failed:\n" + "\n".join(errors))