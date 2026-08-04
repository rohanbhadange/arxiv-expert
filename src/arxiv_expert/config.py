"""Local application configuration and platform-native paths."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

APP_NAME = "arxiv-expert"
APP_AUTHOR = "arxiv-expert"


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "arxiv-expert.sqlite3"

    @property
    def sync_log(self) -> Path:
        return self.log_dir / "sync.log"

    def ensure(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                directory.chmod(stat.S_IRWXU)


def default_paths() -> AppPaths:
    override = os.environ.get("ARXIV_EXPERT_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        return AppPaths(root / "config", root / "data", root / "logs")
    return AppPaths(
        config_dir=Path(user_config_dir(APP_NAME, APP_AUTHOR)),
        data_dir=Path(user_data_dir(APP_NAME, APP_AUTHOR)),
        log_dir=Path(user_log_dir(APP_NAME, APP_AUTHOR)),
    )


@dataclass(frozen=True, slots=True)
class Settings:
    openai_api_key: str
    installation_id: str
    model: str = "gpt-5.6"
    reasoning_effort: str = "medium"

    @classmethod
    def create(cls, openai_api_key: str) -> Settings:
        key = openai_api_key.strip()
        if not key:
            raise ValueError("The OpenAI API key cannot be empty.")
        return cls(openai_api_key=key, installation_id=str(uuid.uuid4()))

    @property
    def safety_identifier(self) -> str:
        return hashlib.sha256(self.installation_id.encode("utf-8")).hexdigest()


def save_settings(settings: Settings, paths: AppPaths | None = None) -> Path:
    resolved = paths or default_paths()
    resolved.ensure()
    target = resolved.config_file
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(target)
    return target


def load_settings(paths: AppPaths | None = None) -> Settings:
    resolved = paths or default_paths()
    try:
        payload = json.loads(resolved.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("arXiv Expert is not configured. Run `arxiv-expert setup`.") from exc
    try:
        return Settings(**payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid configuration at {resolved.config_file}") from exc
