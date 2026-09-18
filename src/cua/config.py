from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    cua_host: str = "127.0.0.1"
    cua_port: int = 8080
    cua_operator_id: str = "teller01"
    cua_operator_password: str = "demo"
    cua_allow_risky: bool = False
    cua_headless: bool = True
    cua_max_steps: int = 20
    cua_step_timeout_ms: int = 15000
    cua_run_timeout_s: int = 180

    @property
    def base_url(self) -> str:
        return f"http://{self.cua_host}:{self.cua_port}"

    @property
    def bank_url(self) -> str:
        return f"{self.base_url}/bank"

    @property
    def artifacts_dir(self) -> Path:
        return ROOT / "artifacts"

    @property
    def runs_dir(self) -> Path:
        return ROOT / "runs"

    @property
    def evidence_dir(self) -> Path:
        return ROOT / "evidence"

    @property
    def policy_path(self) -> Path:
        return Path(__file__).parent / "policies" / "default.yaml"


settings = Settings()
