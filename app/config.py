"""Application configuration, read from environment variables.

Secrets (API keys) are only ever read here, on the server. They are never
sent to the browser, written to the database or logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so local runs work without extra dependencies.

    Existing environment variables always win over values in the file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Default model per provider. These are only defaults; override with LLM_MODEL.
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "groq": "openai/gpt-oss-120b",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "openai_compatible": "gpt-4o-mini",
}

PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


@dataclass
class Settings:
    app_name: str = "PeopleGuard AI"
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "mock").strip().lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "").strip())
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "").strip())
    llm_timeout_seconds: float = field(default_factory=lambda: _float("LLM_TIMEOUT_SECONDS", 60))
    llm_max_retries: int = field(default_factory=lambda: int(_float("LLM_MAX_RETRIES", 2)))
    # Pause between agent calls; helps stay inside free-tier per-minute limits.
    llm_min_interval_seconds: float = field(default_factory=lambda: _float("LLM_MIN_INTERVAL_SECONDS", 2))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.1))

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))))
    database_path: Path = field(default_factory=lambda: Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "peopleguard.db"))))
    policy_dir: Path = field(default_factory=lambda: Path(os.getenv("POLICY_DIR", str(BASE_DIR / "data" / "policies"))))
    sample_case_dir: Path = BASE_DIR / "data" / "sample_cases"

    # Optional shared access code. When set, every page requires it first.
    access_code: str = field(default_factory=lambda: os.getenv("APP_ACCESS_CODE", "").strip())
    # Token required to upload a replacement policy handbook (affects all users).
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", "").strip())
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "dev-only-change-me"))
    max_upload_mb: float = field(default_factory=lambda: _float("MAX_UPLOAD_MB", 5))
    retrieval_top_k: int = field(default_factory=lambda: int(_float("RETRIEVAL_TOP_K", 6)))
    retrieval_min_score: float = field(default_factory=lambda: _float("RETRIEVAL_MIN_SCORE", 6.0))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", False))

    @property
    def model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")

    @property
    def api_key(self) -> str:
        env = PROVIDER_KEY_ENV.get(self.llm_provider)
        return os.getenv(env, "").strip() if env else ""

    @property
    def api_key_env_name(self) -> str:
        return PROVIDER_KEY_ENV.get(self.llm_provider, "")

    @property
    def llm_enabled(self) -> bool:
        """True only when a real provider is selected AND its key is present."""
        if self.llm_provider not in PROVIDER_KEY_ENV:
            return False
        if self.llm_provider == "openai_compatible":
            # Local servers such as Ollama may not need a key, but need a URL.
            return bool(self.llm_base_url)
        return bool(self.api_key)

    def llm_status(self) -> dict:
        """Safe-to-display description of the AI configuration (no secrets)."""
        known = self.llm_provider in PROVIDER_KEY_ENV or self.llm_provider == "mock"
        if self.llm_provider == "mock":
            state, message = "demo", "Demo mode: no language model is configured. Agent narratives are simulated and labelled as such."
        elif not known:
            state, message = "error", f"Unknown LLM_PROVIDER '{self.llm_provider}'. Use mock, gemini, groq, openrouter or openai_compatible."
        elif not self.llm_enabled:
            need = "LLM_BASE_URL" if self.llm_provider == "openai_compatible" else self.api_key_env_name
            state, message = "missing_key", f"Provider '{self.llm_provider}' is selected but {need} is not set. The app is running in demo mode until it is configured."
        else:
            state, message = "live", f"Live AI mode: {self.llm_provider} / {self.model}."
        return {
            "provider": self.llm_provider,
            "model": self.model if self.llm_enabled else None,
            "state": state,
            "message": message,
            "live": self.llm_enabled,
            "key_env": self.api_key_env_name,
        }


settings = Settings()


def reload_settings() -> Settings:
    """Re-read environment variables (used by tests)."""
    global settings
    settings = Settings()
    return settings


def get_settings() -> Settings:
    return settings
