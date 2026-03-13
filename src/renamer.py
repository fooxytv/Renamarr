"""File renaming service."""

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from .config import Config
from .duplicates import DuplicateHandler
from .formatter import FormattedPath, PlexFormatter
from .omdb_client import MovieResult, OMDbClient
from .parser import MediaInfo, parse_media_file
from .tvmaze_client import EpisodeResult, TVMazeClient, TVShowResult
from .utils import cleanup_empty_directories, ensure_directory, get_associated_files, get_unique_path, is_video_file

logger = logging.getLogger(__name__)


@dataclass
class RenameOperation:
    """A single rename operation."""

    source: Path
    destination: Path
    media_info: MediaInfo
    omdb_movie: MovieResult | None = None
    tvmaze_show: TVShowResult | None = None
    tvmaze_episode: EpisodeResult | None = None
    associated_files: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass
class RenameResult:
    """Result of a rename operation."""

    operation: RenameOperation
    success: bool
    error: str | None = None


class TransactionLog:
    """Log of rename operations for potential rollback."""

    def __init__(self, log_path: Path):
        """Initialize the transaction log.

        Args:
            log_path: Path to store the transaction log
        """
        self.log_path = log_path
        self.operations: list[dict] = []

    def log_operation(
        self,
        operation: RenameOperation,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Log a rename operation."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "source": str(operation.source),
            "destination": str(operation.destination),
            "success": success,
            "error": error,
            "associated_files": [
                {"source": str(src), "destination": str(dst)}
                for src, dst in operation.associated_files
            ],
        }
        self.operations.append(entry)
        self._save()

    def _save(self) -> None:
        """Save the log to disk."""
        ensure_directory(self.log_path.parent)
        with open(self.log_path, "w", encoding="utf-8") as f:
            json.dump(self.operations, f, indent=2)

    def load(self) -> list[dict]:
        """Load operations from the log file."""
        if self.log_path.exists():
            with open(self.log_path, encoding="utf-8") as f:
                self.operations = json.load(f)
        return self.operations


class RenamerService:
    """Service for renaming media files."""

    def __init__(
        self,
        config: Config,
        omdb_client: OMDbClient,
        tvmaze_client: TVMazeClient,
        formatter: PlexFormatter,
        duplicate_handler: DuplicateHandler,
    ):
        """Initialize the renamer service.

        Args:
            config: Application configuration
            omdb_client: OMDb API client for movies
            tvmaze_client: TVMaze API client for TV shows
            formatter: Plex naming formatter
            duplicate_handler: Duplicate file handler
        """
        self.config = config
        self.omdb_client = omdb_client
        self.tvmaze_client = tvmaze_client
        self.formatter = formatter
        self.duplicate_handler = duplicate_handler
        self.dry_run = config.options.dry_run

        # Transaction log (stored in web data dir or fallback to /app/data)
        log_dir = config.web.data_dir if hasattr(config, "web") else Path("/app/data")
        self.transaction_log = TransactionLog(log_dir / "transactions.json")

    async def process_file(self, file_path: Path) -> RenameResult | None:
        """Process a single media file.

        Args:
            file_path: Path to the media file

        Returns:
            Rename result or None if file was skipped
        """
        if not is_video_file(file_path):
            logger.debug(f"Skipping non-video file: {file_path}")
            return None

        # Parse the file
        media_info = parse_media_file(file_path)
        logger.info(f"Parsed: {file_path.name} -> {media_info.media_type}")

        if not media_info.media_type:
            logger.warning(f"Could not determine media type for: {file_path}")
            return None

        # Look up metadata
        omdb_movie = None
        tvmaze_show = None
        tvmaze_episode = None

        if media_info.is_movie:
            # Use OMDb for movies
            omdb_movie = await self.omdb_client.find_best_match(
                media_info.title or "", media_info.year
            )
            if omdb_movie:
                logger.info(f"OMDb match: {omdb_movie.title} ({omdb_movie.year})")
            else:
                logger.warning(f"No OMDb match for movie: {media_info.title}")

        elif media_info.is_episode:
            # Use TVMaze for TV shows (no API key required!)
            tvmaze_show = await self.tvmaze_client.find_best_match(
                media_info.show_name or "", media_info.year
            )
            if tvmaze_show:
                logger.info(f"TVMaze match: {tvmaze_show.name}")

                # Get episode details
                if media_info.season is not None and media_info.episode is not None:
                    tvmaze_episode = await self.tvmaze_client.get_episode(
                        tvmaze_show.tvmaze_id,
                        media_info.season,
                        media_info.episode,
                    )
                    if tvmaze_episode:
                        logger.info(f"Episode: {tvmaze_episode.name}")
            else:
                logger.warning(f"No TVMaze match for show: {media_info.show_name}")

        # Format the new path
        formatted = self.formatter.format(
            media_info,
            omdb_movie=omdb_movie,
            tvmaze_show=tvmaze_show,
            tvmaze_episode=tvmaze_episode,
        )

        # Determine output directory based on media type
        if media_info.is_movie:
            output_dir = self.config.directories.movies.output
        else:
            output_dir = self.config.directories.tv.output

        # Create the rename operation
        operation = self._create_operation(
            file_path, output_dir, formatted, media_info, omdb_movie, tvmaze_show, tvmaze_episode
        )

        # Execute the rename
        return await self._execute_operation(operation)

    def _create_operation(
        self,
        source: Path,
        output_dir: Path,
        formatted: FormattedPath,
        media_info: MediaInfo,
        omdb_movie: MovieResult | None,
        tvmaze_show: TVShowResult | None,
        tvmaze_episode: EpisodeResult | None,
    ) -> RenameOperation:
        """Create a rename operation."""
        destination = output_dir / formatted.relative_path / formatted.filename

        # Get associated files
        associated = get_associated_files(source)
        associated_ops = []
        for assoc_file in associated:
            # Keep the language suffix if present
            assoc_suffix = assoc_file.suffix
            assoc_stem = assoc_file.stem

            # Check for language code (e.g., movie.en.srt)
            if "." in assoc_stem:
                parts = assoc_stem.rsplit(".", 1)
                lang_code = parts[-1]
                if len(lang_code) in (2, 3):  # ISO language codes
                    new_name = f"{formatted.filename.rsplit('.', 1)[0]}.{lang_code}{assoc_suffix}"
                else:
                    new_name = f"{formatted.filename.rsplit('.', 1)[0]}{assoc_suffix}"
            else:
                new_name = f"{formatted.filename.rsplit('.', 1)[0]}{assoc_suffix}"

            assoc_dest = output_dir / formatted.relative_path / new_name
            associated_ops.append((assoc_file, assoc_dest))

        return RenameOperation(
            source=source,
            destination=destination,
            media_info=media_info,
            omdb_movie=omdb_movie,
            tvmaze_show=tvmaze_show,
            tvmaze_episode=tvmaze_episode,
            associated_files=associated_ops,
        )

    async def _execute_operation(self, operation: RenameOperation) -> RenameResult:
        """Execute a rename operation."""
        # Skip if source is already at the correct destination
        # Use normalized comparison to handle case/punctuation differences
        try:
            src_str = str(operation.source.resolve())
            dst_str = str(operation.destination.resolve())
            if src_str == dst_str:
                logger.debug(f"Already correctly named: {operation.source.name}")
                return RenameResult(operation=operation, success=True)
            if src_str.lower() == dst_str.lower():
                logger.debug(f"Already correctly named (case match): {operation.source.name}")
                return RenameResult(operation=operation, success=True)
        except OSError:
            pass

        # Skip if source file no longer exists (already moved or deleted)
        if not operation.source.exists():
            logger.warning(f"Source file no longer exists: {operation.source}")
            return RenameResult(
                operation=operation, success=False,
                error="Source file no longer exists",
            )

        logger.info(f"Rename: {operation.source.name} -> {operation.destination}")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would rename: {operation.source.name}")
            logger.info(f"[DRY RUN]           to: {operation.destination}")
            for src, dst in operation.associated_files:
                logger.info(f"[DRY RUN]   + associated: {src.name} -> {dst.name}")
            return RenameResult(operation=operation, success=True)

        try:
            # Ensure destination directory exists (case-insensitive match)
            actual_dest_dir = ensure_directory(operation.destination.parent)
            dest = actual_dest_dir / operation.destination.name

            # Handle existing file
            if dest.exists():
                dest = get_unique_path(dest)
                logger.warning(f"Destination exists, using: {dest}")

            # Remember source directory for cleanup
            source_dir = operation.source.parent

            # Determine the top-level watch directory to stop cleanup at
            if operation.media_info.is_movie:
                stop_at = self.config.directories.movies.watch
            else:
                stop_at = self.config.directories.tv.watch

            # Move the main file
            shutil.move(str(operation.source), str(dest))
            logger.info(f"Moved: {operation.source} -> {dest}")

            # Move associated files
            for src, assoc_dst in operation.associated_files:
                actual_assoc_dest = actual_dest_dir / assoc_dst.name
                if actual_assoc_dest.exists():
                    actual_assoc_dest = get_unique_path(actual_assoc_dest)
                shutil.move(str(src), str(actual_assoc_dest))
                logger.info(f"Moved associated: {src.name} -> {actual_assoc_dest.name}")

            # Clean up empty source directories
            cleanup_empty_directories(source_dir, stop_at=stop_at)

            # Log the operation
            self.transaction_log.log_operation(operation, success=True)

            return RenameResult(operation=operation, success=True)

        except Exception as e:
            logger.error(f"Rename failed: {e}")
            self.transaction_log.log_operation(
                operation, success=False, error=str(e)
            )
            return RenameResult(operation=operation, success=False, error=str(e))

    async def process_directory(
        self, directory: Path, media_type: Literal["movie", "episode"]
    ) -> list[RenameResult]:
        """Process all media files in a directory.

        Args:
            directory: Directory to process
            media_type: Type of media in the directory

        Returns:
            List of rename results
        """
        results = []
        files = list(directory.rglob("*"))

        video_files = [f for f in files if is_video_file(f)]
        logger.info(f"Found {len(video_files)} video files in {directory}")

        # First pass: parse all files and look up metadata
        media_infos: list[MediaInfo] = []
        for file_path in video_files:
            try:
                media_info = parse_media_file(file_path)
                if not media_info.media_type:
                    logger.warning(f"Could not determine media type for: {file_path}")
                    continue

                # Look up metadata to get consistent IDs for duplicate detection
                if media_info.is_movie:
                    omdb_movie = await self.omdb_client.find_best_match(
                        media_info.title or "", media_info.year
                    )
                    if omdb_movie:
                        # Use IMDb ID for duplicate detection
                        media_info.tmdb_id = hash(omdb_movie.imdb_id)  # Use hash as numeric ID
                        logger.info(f"Parsed: {file_path.name} -> movie: {omdb_movie.title} ({omdb_movie.year}) [{media_info.quality.resolution or 'unknown'}]")
                elif media_info.is_episode:
                    tvmaze_show = await self.tvmaze_client.find_best_match(
                        media_info.show_name or "", media_info.year
                    )
                    if tvmaze_show:
                        media_info.tmdb_id = tvmaze_show.tvmaze_id
                        season = media_info.season or 0
                        episode = media_info.episode or 0
                        logger.info(f"Parsed: {file_path.name} -> episode: {tvmaze_show.name} S{season:02d}E{episode:02d} [{media_info.quality.resolution or 'unknown'}]")

                media_infos.append(media_info)
            except Exception as e:
                logger.error(f"Error processing {file_path.name}: {e}")
                continue

        # Second pass: detect and resolve duplicates
        duplicate_groups = self.duplicate_handler.find_duplicates(media_infos)
        files_to_skip: set[Path] = set()

        for group in duplicate_groups:
            logger.info(f"Found {len(group.files)} duplicates for: {group.identifier}")
            resolution = self.duplicate_handler.resolve_duplicates(group)
            if resolution:
                logger.info(f"  Keeping best quality: {resolution.kept.path.name} ({resolution.kept.quality.resolution or 'unknown'})")
                for removed in resolution.removed:
                    logger.info(f"  {'[DRY RUN] Would skip' if self.dry_run else 'Skipping'} lower quality: {removed.path.name} ({removed.quality.resolution or 'unknown'})")
                    files_to_skip.add(removed.path)

        # Third pass: process files (skip duplicates)
        for media_info in media_infos:
            if media_info.path in files_to_skip:
                continue
            try:
                result = await self.process_file(media_info.path)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error renaming {media_info.path.name}: {e}")
                continue

        return results

    async def preview_file(self, file_path: Path) -> RenameOperation | None:
        """Preview a rename without executing it.

        Returns the operation that would be performed, or None if skipped.
        """
        if not is_video_file(file_path):
            return None

        media_info = parse_media_file(file_path)
        if not media_info.media_type:
            return None

        omdb_movie = None
        tvmaze_show = None
        tvmaze_episode = None

        if media_info.is_movie:
            omdb_movie = await self.omdb_client.find_best_match(
                media_info.title or "", media_info.year
            )
        elif media_info.is_episode:
            tvmaze_show = await self.tvmaze_client.find_best_match(
                media_info.show_name or "", media_info.year
            )
            if tvmaze_show and media_info.season is not None and media_info.episode is not None:
                tvmaze_episode = await self.tvmaze_client.get_episode(
                    tvmaze_show.tvmaze_id, media_info.season, media_info.episode
                )

        formatted = self.formatter.format(
            media_info,
            omdb_movie=omdb_movie,
            tvmaze_show=tvmaze_show,
            tvmaze_episode=tvmaze_episode,
        )

        if media_info.is_movie:
            output_dir = self.config.directories.movies.output
        else:
            output_dir = self.config.directories.tv.output

        return self._create_operation(
            file_path, output_dir, formatted, media_info,
            omdb_movie, tvmaze_show, tvmaze_episode,
        )

    async def preview_directory(
        self, directory: Path, media_type: Literal["movie", "episode"]
    ) -> tuple[list[RenameOperation], list]:
        """Preview all renames in a directory without executing.

        Returns (operations, duplicate_groups).
        """
        from .duplicates import DuplicateGroup as DupGroup

        files = list(directory.rglob("*"))
        video_files = [f for f in files if is_video_file(f)]
        logger.info(f"Found {len(video_files)} video files in {directory}")

        operations: list[RenameOperation] = []
        media_infos: list[MediaInfo] = []

        for file_path in video_files:
            try:
                op = await self.preview_file(file_path)
                if op:
                    operations.append(op)
                    media_infos.append(op.media_info)

                    # Set IDs for duplicate detection
                    if op.media_info.is_movie and op.omdb_movie:
                        op.media_info.tmdb_id = hash(op.omdb_movie.imdb_id)
                    elif op.media_info.is_episode and op.tvmaze_show:
                        op.media_info.tmdb_id = op.tvmaze_show.tvmaze_id
            except Exception as e:
                logger.error(f"Error previewing {file_path.name}: {e}")
                continue

        duplicate_groups = self.duplicate_handler.find_duplicates(media_infos)
        return operations, duplicate_groups

    async def execute_single(self, operation: RenameOperation) -> RenameResult:
        """Execute a single pre-built rename operation."""
        return await self._execute_operation(operation)

    async def scan_and_process(self) -> dict[str, list[RenameResult]]:
        """Scan all configured directories and process files.

        Returns:
            Dictionary of results by media type
        """
        results = {
            "movies": [],
            "tv": [],
        }

        # Process movies
        movies_dir = self.config.directories.movies.watch
        if movies_dir.exists():
            logger.info(f"Scanning movies directory: {movies_dir}")
            results["movies"] = await self.process_directory(movies_dir, "movie")

        # Process TV shows
        tv_dir = self.config.directories.tv.watch
        if tv_dir.exists():
            logger.info(f"Scanning TV directory: {tv_dir}")
            results["tv"] = await self.process_directory(tv_dir, "episode")

        return results
