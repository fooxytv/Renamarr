"""Duplicate detection and resolution."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .parser import MediaInfo
from .utils import ensure_directory, get_unique_path

logger = logging.getLogger(__name__)

DuplicateAction = Literal["keep_best", "move", "report_only"]


@dataclass
class DuplicateGroup:
    """A group of duplicate media files."""

    # Unique identifier for this group (TMDB ID or formatted path)
    identifier: str
    files: list[MediaInfo]

    @property
    def best_quality(self) -> MediaInfo:
        """Get the best quality file in this group."""
        return max(self.files, key=lambda f: f.quality.quality_score())

    @property
    def duplicates(self) -> list[MediaInfo]:
        """Get all files except the best quality one."""
        best = self.best_quality
        return [f for f in self.files if f.path != best.path]


@dataclass
class DuplicateResolution:
    """Result of duplicate resolution."""

    kept: MediaInfo
    removed: list[MediaInfo]
    action_taken: DuplicateAction


class DuplicateHandler:
    """Handles detection and resolution of duplicate media files."""

    def __init__(
        self,
        action: DuplicateAction = "keep_best",
        duplicates_folder: Path | None = None,
        dry_run: bool = False,
    ):
        """Initialize the duplicate handler.

        Args:
            action: Action to take on duplicates
            duplicates_folder: Folder to move duplicates to (required for 'move' action)
            dry_run: If True, don't actually delete/move files
        """
        self.action = action
        self.duplicates_folder = duplicates_folder
        self.dry_run = dry_run

        if action == "move" and not duplicates_folder:
            raise ValueError("duplicates_folder required for 'move' action")

    def find_duplicates(self, media_files: list[MediaInfo]) -> list[DuplicateGroup]:
        """Find groups of duplicate media files.

        Duplicates are identified by matching TMDB ID, or if not available,
        by matching title/show name and year/season/episode.

        Args:
            media_files: List of parsed media files

        Returns:
            List of duplicate groups (only groups with 2+ files)
        """
        groups: dict[str, list[MediaInfo]] = {}

        for media in media_files:
            key = self._get_duplicate_key(media)
            if key:
                if key not in groups:
                    groups[key] = []
                groups[key].append(media)

        # Return only groups with duplicates
        return [
            DuplicateGroup(identifier=key, files=files)
            for key, files in groups.items()
            if len(files) > 1
        ]

    def _get_duplicate_key(self, media: MediaInfo) -> str | None:
        """Generate a key for duplicate detection.

        Args:
            media: Parsed media information

        Returns:
            Unique key for duplicate grouping, or None if can't be determined
        """
        if media.is_movie:
            # Use TMDB ID if available
            if media.tmdb_id:
                return f"movie:{media.tmdb_id}"
            # Fall back to title + year
            if media.title:
                year = media.year or "unknown"
                return f"movie:{media.title.lower()}:{year}"
        elif media.is_episode:
            # Use TMDB IDs if available
            if media.tmdb_id and media.season is not None and media.episode is not None:
                return f"episode:{media.tmdb_id}:s{media.season}e{media.episode}"
            # Fall back to show name + season + episode
            if media.show_name and media.season is not None and media.episode is not None:
                return f"episode:{media.show_name.lower()}:s{media.season}e{media.episode}"

        return None

    def resolve_duplicates(
        self, group: DuplicateGroup
    ) -> DuplicateResolution | None:
        """Resolve a group of duplicates according to the configured action.

        Args:
            group: Group of duplicate files

        Returns:
            Resolution result, or None if no action taken
        """
        if len(group.files) < 2:
            return None

        best = group.best_quality
        duplicates = group.duplicates

        logger.info(f"Resolving duplicates for {group.identifier}")
        logger.info(f"  Best quality: {best.path.name} (score: {best.quality.quality_score()})")

        for dup in duplicates:
            logger.info(f"  Duplicate: {dup.path.name} (score: {dup.quality.quality_score()})")

        if self.action == "report_only":
            return DuplicateResolution(
                kept=best,
                removed=[],
                action_taken="report_only",
            )

        removed = []
        for dup in duplicates:
            if self.action == "keep_best":
                if not self.dry_run:
                    self._delete_file(dup.path)
                removed.append(dup)
            elif self.action == "move":
                if not self.dry_run:
                    self._move_file(dup.path)
                removed.append(dup)

        return DuplicateResolution(
            kept=best,
            removed=removed,
            action_taken=self.action,
        )

    def _delete_file(self, path: Path) -> None:
        """Delete a file."""
        logger.info(f"Deleting duplicate: {path}")
        try:
            path.unlink()
        except OSError as e:
            logger.error(f"Failed to delete {path}: {e}")

    def _move_file(self, path: Path) -> None:
        """Move a file to the duplicates folder."""
        if not self.duplicates_folder:
            raise ValueError("Duplicates folder not configured")

        ensure_directory(self.duplicates_folder)
        dest = self.duplicates_folder / path.name
        dest = get_unique_path(dest)

        logger.info(f"Moving duplicate: {path} -> {dest}")
        try:
            shutil.move(str(path), str(dest))
        except OSError as e:
            logger.error(f"Failed to move {path}: {e}")

    def check_existing_duplicate(
        self, media: MediaInfo, output_dir: Path
    ) -> MediaInfo | None:
        """Check if a file already exists in the output directory.

        Args:
            media: New media file to check
            output_dir: Output directory to check against

        Returns:
            Existing MediaInfo if duplicate found, None otherwise
        """
        # This would need to be implemented based on how you track existing files
        # For now, return None (no duplicate detection against existing files)
        return None


def compare_quality(file1: MediaInfo, file2: MediaInfo) -> int:
    """Compare quality of two media files.

    Args:
        file1: First file
        file2: Second file

    Returns:
        Positive if file1 is better, negative if file2 is better, 0 if equal
    """
    return file1.quality.quality_score() - file2.quality.quality_score()
