"""Pydantic models for the web API."""

from pydantic import BaseModel


class FilePreview(BaseModel):
    """A single file's scan result for the UI."""

    id: str
    source_path: str
    source_filename: str
    destination_path: str
    destination_filename: str
    media_type: str  # "movie" or "episode"
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    quality_score: int = 0
    file_size: int | None = None
    status: str = "pending"  # pending | approved | rejected | completed | failed
    error: str | None = None
    already_correct: bool = False


class DuplicateGroupPreview(BaseModel):
    """A group of duplicate files."""

    id: str
    identifier: str
    files: list[FilePreview]
    best_file_id: str


class ScanResult(BaseModel):
    """Result of a full scan."""

    scan_id: str
    started_at: str
    completed_at: str | None = None
    status: str = "running"  # running | completed | failed
    files: list[FilePreview] = []
    duplicates: list[DuplicateGroupPreview] = []
    error: str | None = None


class StatusResponse(BaseModel):
    """App status response."""

    scanning: bool
    dry_run: bool
    current_scan_id: str | None = None
    total_files: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    completed: int = 0
    failed: int = 0
