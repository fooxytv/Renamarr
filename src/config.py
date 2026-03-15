"""Configuration management with Pydantic."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class OMDbConfig(BaseModel):
    """OMDb API configuration.

    Get a free API key at: https://www.omdbapi.com/apikey.aspx
    Only requires an email address!
    """

    api_key: str = Field(..., description="OMDb API key")


class DirectoryConfig(BaseModel):
    """Directory configuration for a media type."""

    watch: Path = Field(..., description="Directory to watch for new files")
    output: Path = Field(..., description="Output directory for renamed files")

    @field_validator("watch", "output", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path) -> Path:
        """Expand environment variables and convert to Path."""
        if isinstance(v, str):
            v = os.path.expandvars(v)
        return Path(v)


class DirectoriesConfig(BaseModel):
    """Directories configuration for movies and TV shows."""

    movies: DirectoryConfig
    tv: DirectoryConfig


class OptionsConfig(BaseModel):
    """General options configuration."""

    dry_run: bool = Field(default=False, description="Preview changes without applying")
    scan_interval: int = Field(default=300, description="Interval between scans in seconds")
    scheduled_scan: bool = Field(default=False, description="Enable scheduled background scans")
    min_file_age: int = Field(default=60, description="Minimum file age before processing")
    auto_approve_threshold: int = Field(
        default=0,
        description="Auto-approve files with confidence >= this (0 = disabled, recommended: 90)",
    )


class DuplicatesConfig(BaseModel):
    """Duplicate handling configuration."""

    action: Literal["keep_best", "move", "report_only"] = Field(
        default="keep_best", description="Action to take on duplicates"
    )
    duplicates_folder: Path | None = Field(
        default=None, description="Folder to move duplicates to"
    )

    @field_validator("duplicates_folder", mode="before")
    @classmethod
    def expand_duplicates_path(cls, v: str | Path | None) -> Path | None:
        """Expand environment variables and convert to Path."""
        if v is None:
            return None
        if isinstance(v, str):
            v = os.path.expandvars(v)
        return Path(v)


class NamingConfig(BaseModel):
    """Naming convention configuration."""

    movies: str = Field(
        default="{title} ({year})/{title} ({year}){ext}",
        description="Movie naming pattern",
    )
    tv: str = Field(
        default="{show} ({show_year})/Season {season:02d}/{show} ({show_year}) - S{season:02d}E{episode:02d} - {episode_title}{ext}",
        description="TV show naming pattern",
    )


class WebConfig(BaseModel):
    """Web UI configuration."""

    host: str = Field(default="0.0.0.0", description="Web server host")
    port: int = Field(default=8080, description="Web server port")
    data_dir: Path = Field(default=Path("/app/data"), description="Data directory for scan results")


class DiscordConfig(BaseModel):
    """Discord bot configuration for reaction-based approve/reject/ignore."""

    bot_token: str = Field(default="", description="Discord bot token")
    channel_id: int = Field(default=0, description="Discord channel ID for notifications")

    @field_validator("bot_token", mode="before")
    @classmethod
    def resolve_bot_token(cls, v: str) -> str:
        if isinstance(v, str) and v.startswith("${"):
            env_var = v[2:-1]
            return os.environ.get(env_var, "")
        return v


class Config(BaseModel):
    """Main application configuration."""

    omdb: OMDbConfig
    directories: DirectoriesConfig
    options: OptionsConfig = Field(default_factory=OptionsConfig)
    duplicates: DuplicatesConfig = Field(default_factory=DuplicatesConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load configuration from YAML file."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Configuration file is empty: {path}")

        # Ensure omdb section exists
        if "omdb" not in data:
            data["omdb"] = {}

        # Resolve OMDb API key: env var > ${VAR} syntax in yaml > empty
        env_api_key = os.environ.get("OMDB_API_KEY")
        if env_api_key:
            data["omdb"]["api_key"] = env_api_key
        elif "api_key" in data["omdb"]:
            api_key = data["omdb"]["api_key"]
            if isinstance(api_key, str) and api_key.startswith("${"):
                env_var = api_key[2:-1]
                data["omdb"]["api_key"] = os.environ.get(env_var, "")

        # Resolve Discord bot token from env var
        env_bot_token = os.environ.get("RENAMARR_DISCORD_BOT_TOKEN")
        if env_bot_token:
            data.setdefault("discord", {})["bot_token"] = env_bot_token

        # Resolve Discord channel ID from env var
        env_channel_id = os.environ.get("RENAMARR_DISCORD_CHANNEL_ID")
        if env_channel_id:
            data.setdefault("discord", {})["channel_id"] = int(env_channel_id)

        return cls(**data)


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from file or environment."""
    if config_path is None:
        config_path = Path("config.yaml")

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    return Config.from_yaml(config_path)
