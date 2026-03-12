"""Utility functions for Renamarr."""

import logging
import re
import time
from pathlib import Path

# Common video file extensions
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".ts",
    ".m2ts",
}

# Associated file extensions to move alongside video files
ASSOCIATED_EXTENSIONS = {
    ".srt",
    ".sub",
    ".idx",
    ".ass",
    ".ssa",
    ".nfo",
    ".jpg",
    ".jpeg",
    ".png",
    ".tbn",
}

# Characters not allowed in filenames on various operating systems
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up application logging."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("renamarr")


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename.

    Removes or replaces characters that are not allowed in filenames.
    """
    # Replace invalid characters with empty string
    sanitized = INVALID_FILENAME_CHARS.sub("", name)

    # Replace multiple spaces with single space
    sanitized = re.sub(r"\s+", " ", sanitized)

    # Strip leading/trailing whitespace and dots
    sanitized = sanitized.strip(" .")

    # Handle empty result
    if not sanitized:
        sanitized = "Unknown"

    return sanitized


def is_video_file(path: Path) -> bool:
    """Check if a path is a video file."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def get_associated_files(video_path: Path) -> list[Path]:
    """Get associated files for a video file (subtitles, nfo, etc.)."""
    associated = []
    stem = video_path.stem
    parent = video_path.parent

    for ext in ASSOCIATED_EXTENSIONS:
        # Check exact match
        potential = parent / f"{stem}{ext}"
        if potential.exists():
            associated.append(potential)

        # Check for language-specific subtitles (e.g., movie.en.srt)
        for lang_file in parent.glob(f"{stem}.*{ext}"):
            if lang_file not in associated:
                associated.append(lang_file)

    return associated


def get_file_age(path: Path) -> float:
    """Get the age of a file in seconds since last modification."""
    return time.time() - path.stat().st_mtime


def format_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def ensure_directory(path: Path) -> None:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)


def get_unique_path(path: Path) -> Path:
    """Get a unique path by appending a number if the path already exists."""
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1

    while True:
        new_path = parent / f"{stem} ({counter}){suffix}"
        if not new_path.exists():
            return new_path
        counter += 1
