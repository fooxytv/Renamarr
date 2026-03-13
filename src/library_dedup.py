"""Library folder deduplication and cleanup.

Scans output/library directories for:
- Case-insensitive duplicate folders (merge them)
- Incorrectly named folders (scene names, missing formatting)
"""

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from guessit import guessit

from .utils import cleanup_empty_directories, get_unique_path, sanitize_filename

logger = logging.getLogger(__name__)


@dataclass
class FolderMergeGroup:
    """A group of folders that are case-insensitive duplicates."""

    canonical: Path
    duplicates: list[Path]
    canonical_file_count: int = 0
    canonical_size: int = 0
    duplicate_file_count: int = 0
    duplicate_size: int = 0
    conflicts: int = 0


@dataclass
class MergeResult:
    """Result of merging duplicate folders."""

    moved: int = 0
    conflicts: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class LibraryDeduplicator:
    """Scans library directories for case-insensitive duplicate folders."""

    def scan_directory(self, root: Path, recursive: bool = True) -> list[FolderMergeGroup]:
        """Scan a directory for case-insensitive duplicate folders.

        Args:
            root: The library root to scan (e.g. /media/movies)
            recursive: If True, also scan subdirectories (for TV season folders)

        Returns:
            List of folder merge groups
        """
        if not root.exists() or not root.is_dir():
            return []

        groups = self._find_duplicate_groups(root)

        # For recursive mode (TV shows), also check subdirectories
        if recursive:
            # Check inside each top-level folder for sub-folder duplicates
            try:
                for child in sorted(root.iterdir()):
                    if child.is_dir() and not child.is_symlink():
                        sub_groups = self._find_duplicate_groups(child)
                        groups.extend(sub_groups)
            except (PermissionError, OSError) as e:
                logger.error(f"Error scanning subdirectories of {root}: {e}")

        return groups

    def _find_duplicate_groups(self, parent: Path) -> list[FolderMergeGroup]:
        """Find groups of case-insensitive duplicate folders within a parent."""
        # Group folders by lowercase name
        folder_map: dict[str, list[Path]] = {}
        try:
            for child in sorted(parent.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    key = child.name.lower()
                    folder_map.setdefault(key, []).append(child)
        except (PermissionError, OSError) as e:
            logger.error(f"Error scanning {parent}: {e}")
            return []

        groups = []
        for folders in folder_map.values():
            if len(folders) < 2:
                continue

            canonical = self._pick_canonical(folders)
            duplicates = [f for f in folders if f != canonical]

            group = FolderMergeGroup(
                canonical=canonical,
                duplicates=duplicates,
            )

            # Gather stats
            group.canonical_file_count, group.canonical_size = self._count_files(canonical)
            for dup in duplicates:
                count, size = self._count_files(dup)
                group.duplicate_file_count += count
                group.duplicate_size += size

            # Count conflicts
            group.conflicts = self._count_conflicts(canonical, duplicates)

            groups.append(group)

        return groups

    def _pick_canonical(self, folders: list[Path]) -> Path:
        """Pick the best folder to keep as canonical.

        Prefers: most files > largest size > title-case-like naming.
        """
        scored = []
        for folder in folders:
            file_count, total_size = self._count_files(folder)
            # Simple heuristic: prefer names with uppercase letters (title case)
            has_upper = sum(1 for c in folder.name if c.isupper())
            scored.append((file_count, total_size, has_upper, folder))

        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return scored[0][3]

    def _count_files(self, folder: Path) -> tuple[int, int]:
        """Count files and total size in a folder (recursive)."""
        count = 0
        total_size = 0
        try:
            for item in folder.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    count += 1
                    try:
                        total_size += item.stat().st_size
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
        return count, total_size

    def _count_conflicts(self, canonical: Path, duplicates: list[Path]) -> int:
        """Count how many files would conflict when merging."""
        # Build set of relative paths in canonical (lowercase for comparison)
        canonical_files: set[str] = set()
        try:
            for item in canonical.rglob("*"):
                if item.is_file():
                    rel = str(item.relative_to(canonical)).lower()
                    canonical_files.add(rel)
        except (PermissionError, OSError):
            pass

        conflicts = 0
        for dup in duplicates:
            try:
                for item in dup.rglob("*"):
                    if item.is_file():
                        rel = str(item.relative_to(dup)).lower()
                        if rel in canonical_files:
                            conflicts += 1
            except (PermissionError, OSError):
                pass

        return conflicts

    def execute_merge(self, canonical: Path, duplicate: Path) -> MergeResult:
        """Merge a duplicate folder into the canonical folder.

        Moves all files from duplicate into canonical, creating
        subdirectories as needed. Uses get_unique_path for conflicts.
        Removes the duplicate folder if empty after merging.
        """
        result = MergeResult()

        if not duplicate.exists():
            return result

        try:
            for item in list(duplicate.rglob("*")):
                if not item.is_file() or item.is_symlink():
                    continue

                # Compute destination preserving relative path
                rel = item.relative_to(duplicate)
                dest = canonical / rel

                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if dest.exists():
                        dest = get_unique_path(dest)
                        result.conflicts += 1

                    shutil.move(str(item), str(dest))
                    result.moved += 1
                    logger.info(f"Merged: {item} -> {dest}")
                except (OSError, shutil.Error) as e:
                    result.failed += 1
                    result.errors.append(f"{item.name}: {e}")
                    logger.error(f"Failed to merge {item}: {e}")

        except (PermissionError, OSError) as e:
            result.failed += 1
            result.errors.append(str(e))
            logger.error(f"Error walking {duplicate}: {e}")

        # Clean up empty directories
        cleanup_empty_directories(duplicate, stop_at=duplicate.parent)
        # Also try to remove the duplicate folder itself if empty
        try:
            if duplicate.exists() and not any(duplicate.rglob("*")):
                shutil.rmtree(str(duplicate))
                logger.info(f"Removed empty duplicate folder: {duplicate}")
        except (PermissionError, OSError) as e:
            logger.warning(f"Could not remove {duplicate}: {e}")

        return result


# Plex naming pattern: "Title (Year)" for movies, "Show Name" for TV
PLEX_MOVIE_PATTERN = re.compile(r"^.+ \(\d{4}\)$")

# Indicators of a scene/unformatted folder name
SCENE_INDICATORS = re.compile(
    r"\b(1080[pi]|2160p|720p|480p|WEB[-.]?DL|WEB[-.]?Rip|BluRay|BDRip|HDRip|"
    r"DVDRip|HDTV|x264|x265|H\.?264|H\.?265|HEVC|AAC|DD[+P]?5\.1|DTS|FLAC|"
    r"AMZN|NF|DSNP|HMAX|ATVP|PMTP|WEB|Remux|PROPER|REPACK)\b",
    re.IGNORECASE,
)


@dataclass
class FolderRenameProposal:
    """A proposal to rename a misnamed library folder."""

    current_path: Path
    current_name: str
    proposed_name: str | None = None
    title: str | None = None
    year: int | None = None
    media_type: str = "movie"  # "movie" or "tv"
    file_count: int = 0
    total_size: int = 0


class LibraryFolderScanner:
    """Scans library folders for incorrectly named directories."""

    def find_misnamed_folders(
        self, root: Path, media_type: str
    ) -> list[FolderRenameProposal]:
        """Find folders that don't match Plex naming conventions.

        Args:
            root: Library root (e.g. /media/movies)
            media_type: "movie" or "tv"

        Returns:
            List of folders that need renaming
        """
        if not root.exists() or not root.is_dir():
            return []

        proposals = []
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue

                if self._is_misnamed(child.name, media_type):
                    proposal = self._create_proposal(child, media_type)
                    if proposal:
                        proposals.append(proposal)
        except (PermissionError, OSError) as e:
            logger.error(f"Error scanning {root}: {e}")

        return proposals

    def _is_misnamed(self, folder_name: str, media_type: str) -> bool:
        """Check if a folder name doesn't match Plex conventions."""
        if media_type == "movie":
            # Good: "War of the Worlds (2025)"
            # Bad: "war.of.the.worlds.2025.1080p.WEB-DL..."
            if PLEX_MOVIE_PATTERN.match(folder_name):
                return False
            # Has scene indicators = definitely misnamed
            if SCENE_INDICATORS.search(folder_name):
                return True
            # Contains dots as separators (scene naming)
            if "." in folder_name and not folder_name.endswith(")"):
                return True
            return False
        else:
            # TV: check for scene indicators or dot-separated names
            if SCENE_INDICATORS.search(folder_name):
                return True
            # Dot-separated scene style
            dots = folder_name.count(".")
            if dots >= 3:
                return True
            return False

    def _create_proposal(
        self, folder: Path, media_type: str
    ) -> FolderRenameProposal | None:
        """Create a rename proposal by parsing the folder name with guessit."""
        try:
            parsed = guessit(folder.name)
        except Exception as e:
            logger.warning(f"Could not parse folder name '{folder.name}': {e}")
            return None

        title = parsed.get("title")
        year = parsed.get("year")

        if not title:
            return None

        # Count files
        file_count = 0
        total_size = 0
        try:
            for item in folder.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    file_count += 1
                    try:
                        total_size += item.stat().st_size
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass

        proposal = FolderRenameProposal(
            current_path=folder,
            current_name=folder.name,
            title=title,
            year=year,
            media_type=media_type,
            file_count=file_count,
            total_size=total_size,
        )

        # Generate proposed name from guessit (will be refined by API lookup)
        if media_type == "movie":
            clean_title = sanitize_filename(
                title.title() if title == title.lower() else title
            )
            if year:
                proposal.proposed_name = f"{clean_title} ({year})"
            else:
                proposal.proposed_name = clean_title
        else:
            clean_title = sanitize_filename(
                title.title() if title == title.lower() else title
            )
            proposal.proposed_name = clean_title

        return proposal

    @staticmethod
    def execute_folder_rename(current: Path, new_name: str) -> bool:
        """Rename a folder in place.

        Args:
            current: Current folder path
            new_name: New folder name (not full path)

        Returns:
            True if successful
        """
        if not current.exists():
            logger.error(f"Folder not found: {current}")
            return False

        destination = current.parent / new_name

        # Check for case-insensitive collision — merge into existing
        if destination.exists() and destination != current:
            logger.info(f"Merging '{current.name}' into existing '{new_name}'")
            dedup = LibraryDeduplicator()
            result = dedup.execute_merge(destination, current)
            return result.failed == 0

        try:
            current.rename(destination)
            logger.info(f"Renamed folder: {current.name} -> {new_name}")
            return True
        except OSError as e:
            logger.error(f"Failed to rename folder {current}: {e}")
            return False
