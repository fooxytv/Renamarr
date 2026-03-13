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
    poster_url: str | None = None
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


class FolderMergePreview(BaseModel):
    """A group of duplicate folders to merge."""

    id: str
    canonical_path: str
    canonical_name: str
    duplicate_paths: list[str]
    duplicate_names: list[str]
    canonical_file_count: int = 0
    canonical_size: int = 0
    canonical_size_human: str = ""
    duplicate_file_count: int = 0
    duplicate_size: int = 0
    duplicate_size_human: str = ""
    conflicts: int = 0
    media_type: str = "movie"
    status: str = "pending"  # pending | approved | skipped | completed | failed
    error: str | None = None


class LibraryScanResult(BaseModel):
    """Result of a library dedup scan."""

    scan_id: str
    started_at: str
    completed_at: str | None = None
    status: str = "running"  # running | completed | failed
    groups: list[FolderMergePreview] = []
    error: str | None = None


class StatusResponse(BaseModel):
    """App status response."""

    version: str = "dev"
    scanning: bool
    dry_run: bool
    current_scan_id: str | None = None
    total_files: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    completed: int = 0
    failed: int = 0
