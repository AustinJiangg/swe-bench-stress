from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


class Config(BaseSettings):
    # E2B self-hosted instance settings
    e2b_api_key: str = Field(default="test-key", alias="E2B_API_KEY")
    e2b_api_url: str = Field(default="http://localhost:3000", alias="E2B_API_URL")
    e2b_base_image: str = Field(
        default="61.47.17.182:89/e2b/ubuntu:22.04-custom",
        alias="E2B_BASE_IMAGE",
    )

    e2b_template_cpu_count: int = Field(default=1, alias="E2B_TEMPLATE_CPU_COUNT")
    e2b_template_memory_mb: int = Field(default=1024, alias="E2B_TEMPLATE_MEMORY_MB")
    docker_registry_username: str = Field(default="", alias="DOCKER_REGISTRY_USERNAME")
    docker_registry_password: str = Field(default="", alias="DOCKER_REGISTRY_PASSWORD")

    # Stress test settings
    max_concurrent_sandboxes: int = Field(default=10, alias="MAX_CONCURRENT_SANDBOXES")
    sandbox_timeout: int = Field(default=300, alias="SANDBOX_TIMEOUT")
    command_timeout: int = Field(default=60, alias="COMMAND_TIMEOUT")

    # Dataset settings
    data_dir: str = Field(default="./data", alias="DATA_DIR")
    results_dir: str = Field(default="./results", alias="RESULTS_DIR")
    hf_token: str = Field(default="", alias="HF_TOKEN")

    # Sampling: 0 means use all
    n_tasks: int = Field(default=100, alias="N_TASKS")
    n_trajectories: int = Field(default=50, alias="N_TRAJECTORIES")

    model_config = {"env_file": ".env", "populate_by_name": True}

    def ensure_dirs(self):
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        Path(self.results_dir).mkdir(parents=True, exist_ok=True)


config = Config()
