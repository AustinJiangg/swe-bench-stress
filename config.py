from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


class Config(BaseSettings):
    """All configuration is read from environment variables or a .env file.

    Copy .env.example to .env and edit values there.  This class only defines
    field types and env-var names; default values live in .env.example.
    """

    # E2B self-hosted instance settings
    e2b_api_key: str = Field(alias="E2B_API_KEY")
    e2b_api_url: str = Field(alias="E2B_API_URL")
    e2b_base_image: str = Field(alias="E2B_BASE_IMAGE")
    docker_registry_url: str = Field(alias="DOCKER_REGISTRY_URL")
    docker_registry_repo: str = Field(alias="DOCKER_REGISTRY_REPO")
    e2b_registry_username: str = Field(default="", alias="E2B_REGISTRY_USERNAME")
    e2b_registry_password: str = Field(default="", alias="E2B_REGISTRY_PASSWORD")

    e2b_template_cpu_count: int = Field(alias="E2B_TEMPLATE_CPU_COUNT")
    e2b_template_memory_mb: int = Field(alias="E2B_TEMPLATE_MEMORY_MB")

    # Stress test settings
    max_concurrent_sandboxes: int = Field(alias="MAX_CONCURRENT_SANDBOXES")
    sandbox_timeout: int = Field(alias="SANDBOX_TIMEOUT")
    command_timeout: int = Field(alias="COMMAND_TIMEOUT")

    # Dataset settings
    data_dir: str = Field(alias="DATA_DIR")
    results_dir: str = Field(alias="RESULTS_DIR")
    hf_token: str = Field(default="", alias="HF_TOKEN")

    # Sampling: 0 means use all
    n_tasks: int = Field(alias="N_TASKS")
    n_trajectories: int = Field(alias="N_TRAJECTORIES")

    # Proxy settings (injected into generated Dockerfiles)
    http_proxy: str = Field(default="", alias="HTTP_PROXY")
    https_proxy: str = Field(default="", alias="HTTPS_PROXY")
    no_proxy: str = Field(default="", alias="NO_PROXY")

    model_config = {"env_file": ".env", "populate_by_name": True}

    def ensure_dirs(self):
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.results_dir).mkdir(parents=True, exist_ok=True)


class _LazyConfig:
    """Delays Config() instantiation until first attribute access."""

    def __init__(self):
        self._instance = None

    def _load(self):
        if self._instance is None:
            self._instance = Config()
        return self._instance

    def __getattr__(self, name):
        return getattr(self._load(), name)


config = _LazyConfig()
