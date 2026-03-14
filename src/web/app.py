"""FastAPI web application for Renamarr."""

import asyncio
import hmac
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .. import __version__
from ..auth import get_api_key, get_passphrase, verify_code


class LogBuffer(logging.Handler):
    """Ring buffer log handler that keeps recent log entries for the UI."""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=maxlen)
        self._counter = 0

    # Skip noisy loggers that aren't useful in the activity panel
    SKIP_LOGGERS = {"uvicorn.access", "uvicorn.error", "httpcore", "hpack"}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in self.SKIP_LOGGERS:
            return
        self._counter += 1
        self.records.append({
            "id": self._counter,
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })

    def get_logs(self, after: int = 0) -> list[dict]:
        """Return log entries after the given ID."""
        if after == 0:
            return list(self.records)
        return [r for r in self.records if r["id"] > after]


log_buffer = LogBuffer()
from ..confidence import score_movie_match, score_episode_match
from ..config import Config
from ..duplicates import DuplicateHandler
from ..library_dedup import LibraryDeduplicator, LibraryFolderScanner
from ..formatter import PlexFormatter
from ..notifications import DiscordNotifier
from ..omdb_client import OMDbClient
from ..database import RenamarrDB
from ..renamer import RenameOperation, RenamerService
from ..tvmaze_client import TVMazeClient, TVShowResult, EpisodeResult
from ..utils import format_size, is_video_file, sanitize_filename
from .models import (
    DuplicateGroupPreview,
    FilePreview,
    FolderMergePreview,
    FolderRenamePreview,
    LibraryScanResult,
    ScanResult,
    StatusResponse,
)
from .scan_store import ScanStore

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


class RateLimiter:
    """Simple in-memory rate limiter per IP."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        attempts = self._attempts[key]
        # Remove expired attempts
        self._attempts[key] = [t for t in attempts if now - t < self.window]
        if len(self._attempts[key]) >= self.max_attempts:
            return False
        self._attempts[key].append(now)
        return True


# Rate limiter for delete auth attempts (5 attempts per minute per IP)
delete_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)


class RenamarrWeb:
    """Web application state and services."""

    def __init__(self, config: Config, data_dir: Path):
        self.config = config
        self.store = ScanStore(data_dir)
        self.db = RenamarrDB(data_dir)
        self.scanning = False
        self._shutting_down = False
        self._scan_task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._omdb_client: OMDbClient | None = None
        self._tvmaze_client: TVMazeClient | None = None
        self._renamer: RenamerService | None = None
        self._notifier = DiscordNotifier()
        # Cache operations between preview and execute (in-memory + persisted)
        self._operations: dict[str, RenameOperation] = {}
        # Scheduled scan state
        self._scheduler_task: asyncio.Task | None = None
        self._next_scan_at: datetime | None = None
        # Load persisted operations on startup
        self._load_persisted_operations()

    def _serialize_operations(self) -> None:
        """Save operations to disk so they survive restarts."""
        serialized = {}
        for file_id, op in self._operations.items():
            serialized[file_id] = {
                "source": str(op.source),
                "destination": str(op.destination),
                "media_type": op.media_info.media_type,
                "associated_files": [
                    [str(src), str(dst)] for src, dst in op.associated_files
                ],
            }
        self.store.save_operations(serialized)

    def _load_persisted_operations(self) -> None:
        """Load operations from disk into memory."""
        from ..parser import MediaInfo, QualityInfo

        serialized = self.store.load_operations()
        for file_id, data in serialized.items():
            source = Path(data["source"])
            # Create a minimal MediaInfo for the execute path
            media_info = MediaInfo(
                path=source,
                media_type=data.get("media_type", "movie"),
                quality=QualityInfo(),
            )
            associated = [
                (Path(src), Path(dst)) for src, dst in data.get("associated_files", [])
            ]
            self._operations[file_id] = RenameOperation(
                source=source,
                destination=Path(data["destination"]),
                media_info=media_info,
                associated_files=associated,
            )
        if serialized:
            logger.info(f"Loaded {len(serialized)} persisted operations")

    async def _scan_directory_cached(
        self, directory: Path, media_type: str
    ) -> tuple[list[RenameOperation], list]:
        """Scan a directory using DB cache to skip API calls for unchanged files."""
        from ..parser import parse_media_file

        video_files = [f for f in directory.rglob("*") if is_video_file(f)]
        logger.info(f"Found {len(video_files)} video files in {directory}")

        operations: list[RenameOperation] = []
        uncached_files: list[Path] = []
        current_paths: set[str] = set()

        for file_path in video_files:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            path_str = str(file_path)
            current_paths.add(path_str)

            # Check if file has a cached match we can reuse
            media_file = self.db.get_media_file(path_str)
            if media_file:
                match = self.db.get_match(media_file["id"])
                if match:
                    # Always use manual overrides; use cache if file unchanged
                    if match["is_manual_override"] or not self.db.file_changed(
                        path_str, stat.st_size, stat.st_mtime
                    ):
                        op = self._rebuild_operation_from_cache(
                            file_path, media_file, match
                        )
                        if op:
                            operations.append(op)
                            # Update mtime/size in DB for changed files
                            self.db.upsert_media_file(
                                path=path_str,
                                filename=file_path.name,
                                file_size=stat.st_size,
                                mtime=stat.st_mtime,
                                media_type=media_file["media_type"],
                            )
                            continue

            uncached_files.append(file_path)

        if uncached_files:
            logger.info(
                f"Cache hit: {len(operations)}, need API lookup: {len(uncached_files)}"
            )
        else:
            logger.info(f"All {len(operations)} files served from cache")

        # Do API lookups only for uncached/changed files
        for file_path in uncached_files:
            try:
                op = await self._renamer.preview_file(file_path)
                if op:
                    operations.append(op)
                    self._cache_operation_to_db(file_path, op)
            except Exception as e:
                logger.error(f"Error previewing {file_path.name}: {e}")

        # Clean up stale DB entries for files no longer on disk
        self.db.remove_stale_files(current_paths)

        # Duplicate detection on all operations
        media_infos = [op.media_info for op in operations]
        duplicate_groups = self._renamer.duplicate_handler.find_duplicates(media_infos)
        return operations, duplicate_groups

    def _rebuild_operation_from_cache(
        self, file_path: Path, media_file: dict, match: dict
    ) -> RenameOperation | None:
        """Reconstruct a RenameOperation from cached DB data."""
        from ..omdb_client import MovieResult
        from ..parser import MediaInfo, QualityInfo
        from ..utils import get_associated_files

        try:
            media_info = MediaInfo(
                path=file_path,
                media_type=media_file["media_type"],
                title=media_file["parsed_title"],
                year=media_file["parsed_year"],
                show_name=media_file["parsed_show_name"],
                season=media_file["parsed_season"],
                episode=media_file["parsed_episode"],
                quality=QualityInfo(
                    resolution=media_file["resolution"],
                    file_size=media_file["file_size"],
                ),
            )

            omdb_movie = None
            tvmaze_show = None
            tvmaze_episode = None

            if match["source"] == "omdb":
                omdb_movie = MovieResult(
                    imdb_id=match["imdb_id"] or "",
                    title=match["omdb_title"] or "",
                    year=match["omdb_year"],
                    plot=match["omdb_plot"] or "",
                    poster=match["omdb_poster"],
                )
                media_info.tmdb_id = hash(match["imdb_id"])
            elif match["source"] == "tvmaze":
                tvmaze_show = TVShowResult(
                    tvmaze_id=match["tvmaze_show_id"],
                    name=match["tvmaze_show_name"] or "",
                    premiered=match["tvmaze_show_premiered"],
                    summary=match["tvmaze_show_summary"] or "",
                    poster=match["tvmaze_show_poster"],
                )
                media_info.tmdb_id = match["tvmaze_show_id"]
                if match["tvmaze_episode_id"]:
                    tvmaze_episode = EpisodeResult(
                        episode_id=match["tvmaze_episode_id"],
                        show_id=match["tvmaze_show_id"],
                        season_number=match["tvmaze_season"] or 1,
                        episode_number=match["tvmaze_episode_number"] or 1,
                        name=match["tvmaze_episode_name"] or "",
                        airdate=match["tvmaze_episode_airdate"],
                        summary=match["tvmaze_episode_summary"] or "",
                    )

            destination = Path(match["destination_path"])

            # Rebuild associated files from disk (they may have changed)
            associated = get_associated_files(file_path)
            associated_ops = []
            for assoc_file in associated:
                assoc_suffix = assoc_file.suffix
                assoc_stem = assoc_file.stem
                dest_stem = destination.stem
                if "." in assoc_stem:
                    parts = assoc_stem.rsplit(".", 1)
                    lang_code = parts[-1]
                    if len(lang_code) in (2, 3):
                        new_name = f"{dest_stem}.{lang_code}{assoc_suffix}"
                    else:
                        new_name = f"{dest_stem}{assoc_suffix}"
                else:
                    new_name = f"{dest_stem}{assoc_suffix}"
                associated_ops.append((assoc_file, destination.parent / new_name))

            return RenameOperation(
                source=file_path,
                destination=destination,
                media_info=media_info,
                omdb_movie=omdb_movie,
                tvmaze_show=tvmaze_show,
                tvmaze_episode=tvmaze_episode,
                associated_files=associated_ops,
            )
        except Exception as e:
            logger.warning(f"Failed to rebuild from cache: {file_path.name}: {e}")
            return None

    def _cache_operation_to_db(self, file_path: Path, op: RenameOperation) -> None:
        """Save a fresh API lookup result to the DB cache."""
        try:
            stat = file_path.stat()
            media_file_id = self.db.upsert_media_file(
                path=str(file_path),
                filename=file_path.name,
                file_size=stat.st_size,
                mtime=stat.st_mtime,
                media_type=op.media_info.media_type,
                parsed_title=op.media_info.title,
                parsed_year=op.media_info.year,
                parsed_show_name=op.media_info.show_name,
                parsed_season=op.media_info.season,
                parsed_episode=op.media_info.episode,
                resolution=op.media_info.quality.resolution,
                quality_score=op.media_info.quality.quality_score(),
            )

            dest_path = str(op.destination)
            if op.omdb_movie:
                self.db.save_movie_match(
                    media_file_id=media_file_id,
                    imdb_id=op.omdb_movie.imdb_id,
                    title=op.omdb_movie.title,
                    year=op.omdb_movie.year,
                    plot=op.omdb_movie.plot,
                    poster=op.omdb_movie.poster,
                    destination_path=dest_path,
                    lookup_title=op.media_info.title or "",
                    lookup_year=op.media_info.year,
                )
            elif op.tvmaze_show:
                self.db.save_episode_match(
                    media_file_id=media_file_id,
                    show_id=op.tvmaze_show.tvmaze_id,
                    show_name=op.tvmaze_show.name,
                    premiered=op.tvmaze_show.premiered,
                    show_poster=op.tvmaze_show.poster,
                    show_summary=op.tvmaze_show.summary,
                    episode_id=op.tvmaze_episode.episode_id if op.tvmaze_episode else None,
                    episode_name=op.tvmaze_episode.name if op.tvmaze_episode else None,
                    airdate=op.tvmaze_episode.airdate if op.tvmaze_episode else None,
                    episode_summary=op.tvmaze_episode.summary if op.tvmaze_episode else None,
                    season=op.tvmaze_episode.season_number if op.tvmaze_episode else None,
                    episode_number=op.tvmaze_episode.episode_number if op.tvmaze_episode else None,
                    destination_path=dest_path,
                    lookup_title=op.media_info.show_name or "",
                    lookup_year=op.media_info.year,
                )
        except Exception as e:
            logger.warning(f"Failed to cache to DB: {file_path.name}: {e}")

    async def startup(self) -> None:
        """Initialize API clients."""
        self._omdb_client = OMDbClient(self.config.omdb.api_key)
        self._tvmaze_client = TVMazeClient()
        await self._omdb_client.__aenter__()
        await self._tvmaze_client.__aenter__()

        formatter = PlexFormatter(
            movie_pattern=self.config.naming.movies,
            tv_pattern=self.config.naming.tv,
        )
        duplicate_handler = DuplicateHandler(
            action=self.config.duplicates.action,
            duplicates_folder=self.config.duplicates.duplicates_folder,
            dry_run=self.config.options.dry_run,
        )
        self._renamer = RenamerService(
            config=self.config,
            omdb_client=self._omdb_client,
            tvmaze_client=self._tvmaze_client,
            formatter=formatter,
            duplicate_handler=duplicate_handler,
        )

        # Start scheduled scan if enabled
        if self.config.options.scheduled_scan and self.config.options.scan_interval > 0:
            self._scheduler_task = asyncio.create_task(self._scan_scheduler())
            logger.info(
                f"Scheduled scans enabled: every {self.config.options.scan_interval}s"
            )

    async def shutdown(self) -> None:
        """Clean up API clients. Waits for any running scan to finish first."""
        self._shutting_down = True

        # Stop scheduler
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        # Wait for scan to complete before closing clients
        if self._scan_task and not self._scan_task.done():
            logger.info("Waiting for scan to complete before shutdown...")
            try:
                await asyncio.wait_for(self._scan_task, timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Scan did not complete in 30s, cancelling")
                self._scan_task.cancel()
                try:
                    await self._scan_task
                except asyncio.CancelledError:
                    pass

        if self._omdb_client:
            await self._omdb_client.__aexit__(None, None, None)
        if self._tvmaze_client:
            await self._tvmaze_client.__aexit__(None, None, None)
        if self.db:
            self.db.close()

    def _compute_confidence(self, op: RenameOperation) -> int:
        """Compute confidence score for a rename operation."""
        if op.media_info.is_movie and op.omdb_movie:
            return score_movie_match(
                parsed_title=op.media_info.title or "",
                parsed_year=op.media_info.year,
                api_title=op.omdb_movie.title,
                api_year=op.omdb_movie.year,
            )
        elif op.media_info.is_episode and op.tvmaze_show:
            return score_episode_match(
                parsed_show=op.media_info.show_name or "",
                parsed_year=op.media_info.year,
                api_show=op.tvmaze_show.name,
                api_year=op.tvmaze_show.year,
                has_episode_match=op.tvmaze_episode is not None,
            )
        return 0

    async def _scan_scheduler(self) -> None:
        """Background task that triggers scans on a schedule."""
        interval = self.config.options.scan_interval
        logger.info(f"Scan scheduler started (interval: {interval}s)")
        try:
            while not self._shutting_down:
                from datetime import timedelta
                self._next_scan_at = datetime.now() + timedelta(seconds=interval)
                logger.debug(f"Next scheduled scan at {self._next_scan_at.isoformat()}")
                await asyncio.sleep(interval)
                if self._shutting_down:
                    break
                if self.scanning:
                    logger.info("Scheduled scan skipped: scan already in progress")
                    continue
                logger.info("Starting scheduled scan")
                self.scanning = True
                self._scan_task = asyncio.create_task(self.run_scan("all"))
                await self._scan_task
        except asyncio.CancelledError:
            logger.info("Scan scheduler stopped")
        except Exception as e:
            logger.error(f"Scan scheduler error: {e}", exc_info=True)

    async def run_scan(self, media_type: str = "all") -> None:
        """Run a scan in the background.

        Args:
            media_type: "all", "movies", or "tv"
        """
        scan_id = str(uuid.uuid4())
        scan = ScanResult(
            scan_id=scan_id,
            started_at=datetime.now().isoformat(),
            status="running",
        )
        self.store.save_scan(scan)
        scan_start = datetime.now()

        # For partial scans, preserve existing data from the other type
        existing_scan = None
        if media_type != "all":
            existing_scan = self.store.load_scan()

        # Clear operations for the types being scanned
        if media_type == "all":
            self._operations.clear()
        else:
            remove_type = "movie" if media_type == "movies" else "episode"
            self._operations = {
                fid: op for fid, op in self._operations.items()
                if not (hasattr(op, 'media_info') and op.media_info.media_type == remove_type)
            }

        try:
            all_files: list[FilePreview] = []
            all_duplicates: list[DuplicateGroupPreview] = []

            # Rebuild library index for "already in library" detection
            output_dirs = []
            if media_type in ("all", "movies"):
                output_dirs.append((self.config.directories.movies.output, "movie"))
            if media_type in ("all", "tv"):
                output_dirs.append((self.config.directories.tv.output, "episode"))
            self.db.rebuild_library(output_dirs)

            # Carry over data from the type NOT being scanned
            if existing_scan and media_type == "movies":
                all_files.extend(f for f in existing_scan.files if f.media_type != "movie")
                all_duplicates.extend(
                    d for d in existing_scan.duplicates
                    if d.files and d.files[0].media_type != "movie"
                )
            elif existing_scan and media_type == "tv":
                all_files.extend(f for f in existing_scan.files if f.media_type != "episode")
                all_duplicates.extend(
                    d for d in existing_scan.duplicates
                    if d.files and d.files[0].media_type != "episode"
                )

            # Scan movies
            if media_type in ("all", "movies"):
                movies_dir = self.config.directories.movies.watch
                if movies_dir.exists():
                    logger.info(f"Scanning movies: {movies_dir}")
                    ops, dups = await self._scan_directory_cached(movies_dir, "movie")
                    files, dup_previews = self._convert_results(ops, dups)
                    all_files.extend(files)
                    all_duplicates.extend(dup_previews)

            # Scan TV
            if media_type in ("all", "tv"):
                tv_dir = self.config.directories.tv.watch
                if tv_dir.exists():
                    logger.info(f"Scanning TV: {tv_dir}")
                    ops, dups = await self._scan_directory_cached(tv_dir, "episode")
                    files, dup_previews = self._convert_results(ops, dups)
                    all_files.extend(files)
                    all_duplicates.extend(dup_previews)

            # Auto-approve high-confidence files
            threshold = self.config.options.auto_approve_threshold
            auto_approved_files = []
            review_files = []

            for f in all_files:
                if f.status != "pending":
                    continue

                # Build rich notification data from the operation
                op = self._operations.get(f.id)
                plot = ""
                if op:
                    if op.omdb_movie:
                        plot = op.omdb_movie.plot or ""
                    elif op.tvmaze_show:
                        plot = op.tvmaze_show.summary or ""

                file_info = {
                    "filename": f.source_filename,
                    "title": f.title,
                    "year": f.year,
                    "confidence": f.confidence,
                    "file_id": f.id,
                    "media_type": f.media_type,
                    "poster": f.poster_url,
                    "plot": plot,
                    "destination": f.destination_filename,
                }

                if threshold > 0 and f.confidence >= threshold:
                    f.status = "approved"
                    auto_approved_files.append(file_info)
                    self.db.save_decision(
                        file_path=f.source_path,
                        file_size=f.file_size,
                        filename=f.source_filename,
                        media_type=f.media_type,
                        status="approved",
                        chosen_destination=f.destination_path,
                    )
                elif f.confidence > 0:
                    review_files.append(file_info)

            if auto_approved_files:
                logger.info(
                    f"Auto-approved {len(auto_approved_files)} files "
                    f"(confidence >= {threshold}%)"
                )

            scan.files = all_files
            scan.duplicates = all_duplicates
            scan.status = "completed"
            scan.completed_at = datetime.now().isoformat()
            logger.info(f"Scan complete: {len(all_files)} files, {len(all_duplicates)} duplicate groups")

            # Send Discord notifications
            pending = sum(1 for f in all_files if f.status == "pending")
            correct = sum(1 for f in all_files if f.already_correct)
            movies = sum(1 for f in all_files if f.media_type == "movie")
            tv = sum(1 for f in all_files if f.media_type == "episode")
            pending_movies = sum(1 for f in all_files if f.status == "pending" and f.media_type == "movie")
            pending_tv = sum(1 for f in all_files if f.status == "pending" and f.media_type == "episode")
            duration = (datetime.now() - scan_start).total_seconds()
            await self._notifier.scan_completed(
                total_files=len(all_files),
                movies=movies,
                tv=tv,
                duplicates=len(all_duplicates),
                pending=pending,
                already_correct=correct,
                pending_movies=pending_movies,
                pending_tv=pending_tv,
                duration_seconds=duration,
            )

            # Notify about auto-approved files
            if auto_approved_files:
                await self._notifier.auto_approved(
                    count=len(auto_approved_files),
                    files=auto_approved_files,
                )

            # Notify about files needing review (low confidence)
            if review_files:
                await self._notifier.review_needed(files=review_files)

        except Exception as e:
            logger.error(f"Scan failed: {e}", exc_info=True)
            scan.status = "failed"
            scan.error = "Scan failed. Check server logs for details."
            scan.completed_at = datetime.now().isoformat()
            await self._notifier.scan_failed(str(e))

        self.store.save_scan(scan)
        self.store.save_to_history(scan)
        self._serialize_operations()
        self.scanning = False

    def _convert_results(
        self,
        operations: list[RenameOperation],
        duplicate_groups: list,
    ) -> tuple[list[FilePreview], list[DuplicateGroupPreview]]:
        """Convert internal results to API models."""
        files = []
        for op in operations:
            file_id = str(uuid.uuid4())
            # Check if already correctly named using multiple methods
            already_correct = False
            try:
                src_resolved = str(op.source.resolve())
                dst_resolved = str(op.destination.resolve())
                if src_resolved == dst_resolved:
                    already_correct = True
                elif src_resolved.lower() == dst_resolved.lower():
                    already_correct = True
                else:
                    import re as _re
                    norm_src = _re.sub(r'[^a-z0-9/\\]', '', src_resolved.lower())
                    norm_dst = _re.sub(r'[^a-z0-9/\\]', '', dst_resolved.lower())
                    if norm_src == norm_dst:
                        already_correct = True
            except OSError:
                pass

            # Check DB library index (normalized comparison)
            if not already_correct:
                if self.db.is_in_library(str(op.destination)):
                    already_correct = True
                    logger.debug(f"Already in library (DB): {op.destination.name}")

            # Restore persisted decision from DB
            decision = self.db.get_decision(str(op.source))
            if not decision and op.media_info.quality.file_size:
                decision = self.db.find_decision(
                    op.source.name, op.media_info.quality.file_size
                )
            restored_status = None
            if decision:
                ds = decision["status"]
                if ds == "ignored":
                    restored_status = "ignored"
                elif ds == "completed":
                    already_correct = True

            title = ""
            year = None
            season = None
            episode = None
            poster_url = None

            if op.media_info.is_movie:
                title = op.omdb_movie.title if op.omdb_movie else (op.media_info.title or "Unknown")
                year = op.omdb_movie.year if op.omdb_movie else op.media_info.year
                if op.omdb_movie and op.omdb_movie.poster:
                    poster_url = op.omdb_movie.poster
            elif op.media_info.is_episode:
                title = op.tvmaze_show.name if op.tvmaze_show else (op.media_info.show_name or "Unknown")
                season = op.media_info.season
                episode = op.media_info.episode
                if op.tvmaze_show and op.tvmaze_show.poster:
                    poster_url = op.tvmaze_show.poster

            confidence = self._compute_confidence(op)

            preview = FilePreview(
                id=file_id,
                source_path=str(op.source),
                source_filename=op.source.name,
                destination_path=str(op.destination),
                destination_filename=op.destination.name,
                media_type=op.media_info.media_type or "unknown",
                title=title,
                year=year,
                season=season,
                episode=episode,
                poster_url=poster_url,
                resolution=op.media_info.quality.resolution,
                quality_score=op.media_info.quality.quality_score(),
                file_size=op.media_info.quality.file_size,
                confidence=confidence,
                status=restored_status or ("correct" if already_correct else "pending"),
                already_correct=already_correct,
            )
            files.append(preview)
            # Cache the operation for later execution
            self._operations[file_id] = op

        # Convert duplicate groups
        dup_previews = []
        for group in duplicate_groups:
            group_id = str(uuid.uuid4())
            group_files = []
            best = group.best_quality
            best_file_id = ""

            for media in group.files:
                # Find matching file preview
                for f in files:
                    if f.source_path == str(media.path):
                        group_files.append(f)
                        if media.path == best.path:
                            best_file_id = f.id
                        break

            if group_files:
                dup_previews.append(DuplicateGroupPreview(
                    id=group_id,
                    identifier=group.identifier,
                    files=group_files,
                    best_file_id=best_file_id,
                ))

        return files, dup_previews

    async def execute_approved(self) -> dict:
        """Execute all approved renames and move rejected duplicates."""
        import shutil
        from .models import FilePreview

        scan = self.store.load_scan()
        if not scan:
            return {"error": "No scan results"}

        results = {"completed": 0, "failed": 0, "moved_to_trash": 0, "errors": []}
        renames: list[dict] = []

        # Execute approved renames
        for file in scan.files:
            if file.status != "approved":
                continue

            op = self._operations.get(file.id)
            if not op:
                file.status = "failed"
                file.error = "Operation not found (scan may be stale)"
                results["failed"] += 1
                results["errors"].append(f"{file.source_filename}: operation not found")
                continue

            try:
                result = await self._renamer.execute_single(op)
                if result.success:
                    file.status = "completed"
                    results["completed"] += 1
                    plot = ""
                    poster = file.poster_url
                    if op.omdb_movie:
                        plot = op.omdb_movie.plot or ""
                    elif op.tvmaze_show:
                        plot = op.tvmaze_show.summary or ""
                    renames.append({
                        "source": file.source_filename,
                        "destination": file.destination_filename,
                        "media_type": file.media_type,
                        "title": file.title,
                        "year": file.year,
                        "poster": poster,
                        "plot": plot,
                        "confidence": file.confidence,
                    })
                    # Mark as completed in DB so it never reappears
                    self.db.save_decision(
                        file_path=file.source_path,
                        file_size=file.file_size,
                        filename=file.source_filename,
                        media_type=file.media_type,
                        status="completed",
                        chosen_destination=file.destination_path,
                    )
                else:
                    file.status = "failed"
                    file.error = result.error
                    results["failed"] += 1
                    results["errors"].append(f"{file.source_filename}: {result.error}")
            except Exception as e:
                file.status = "failed"
                file.error = str(e)
                results["failed"] += 1
                results["errors"].append(f"{file.source_filename}: {e}")

        # Move rejected files to duplicates folder (if configured)
        dup_folder = self.config.duplicates.duplicates_folder
        if dup_folder:
            for file in scan.files:
                if file.status != "rejected":
                    continue

                source = Path(file.source_path)
                if not source.exists():
                    continue

                try:
                    dup_folder.mkdir(parents=True, exist_ok=True)
                    dest = dup_folder / source.name
                    # Avoid overwriting
                    if dest.exists():
                        stem = dest.stem
                        suffix = dest.suffix
                        counter = 1
                        while dest.exists():
                            dest = dup_folder / f"{stem} ({counter}){suffix}"
                            counter += 1
                    shutil.move(str(source), str(dest))
                    file.status = "moved_to_trash"
                    results["moved_to_trash"] += 1
                    logger.info(f"Moved to duplicates: {source.name} -> {dest}")
                except Exception as e:
                    logger.error(f"Failed to move {source.name} to duplicates: {e}")
                    results["errors"].append(f"{file.source_filename}: move to duplicates failed: {e}")

        self.store.save_scan(scan)

        # Send Discord notification
        await self._notifier.execute_completed(
            renamed=results["completed"],
            failed=results["failed"],
            errors=results.get("errors"),
            renames=renames,
            moved_to_trash=results["moved_to_trash"],
        )

        return results

    async def retry_file_lookup(
        self, file_id: str, title: str | None = None, year: int | None = None
    ) -> dict | None:
        """Re-run API lookup for a single file, optionally with overridden title/year.

        Updates the scan result and operations cache with new metadata.
        Returns the updated file preview dict, or None if not found.
        """
        scan = self.store.load_scan()
        if not scan:
            return None

        # Find the file
        file_preview = None
        for f in scan.files:
            if f.id == file_id:
                file_preview = f
                break
        if not file_preview:
            return None

        op = self._operations.get(file_id)

        # Use provided title/year or fall back to existing
        lookup_title = title or file_preview.title or ""
        lookup_year = year or file_preview.year

        poster_url = None
        new_title = file_preview.title
        new_year = file_preview.year
        new_dest_filename = file_preview.destination_filename
        new_dest_path = file_preview.destination_path

        if file_preview.media_type == "movie" and self._omdb_client:
            try:
                result = await self._omdb_client.find_best_match(lookup_title, lookup_year)
                if result:
                    new_title = result.title
                    new_year = result.year
                    poster_url = result.poster
                    # Re-format destination
                    if op and self._renamer:
                        formatted = self._renamer.formatter.format_movie(
                            op.media_info, result
                        )
                        output_dir = self.config.directories.movies.output
                        new_dest = output_dir / formatted.relative_path / formatted.filename
                        new_dest_path = str(new_dest)
                        new_dest_filename = new_dest.name
                        # Update the cached operation
                        op.destination = new_dest
                        op.omdb_movie = result
                        self._serialize_operations()
            except Exception as e:
                logger.error(f"OMDb retry failed for '{lookup_title}': {e}")

        elif file_preview.media_type == "episode" and self._tvmaze_client:
            try:
                result = await self._tvmaze_client.find_best_match(lookup_title, lookup_year)
                if result:
                    new_title = result.name
                    poster_url = result.poster
                    # Re-format destination
                    if op and self._renamer:
                        ep_result = None
                        if file_preview.season is not None and file_preview.episode is not None:
                            ep_result = await self._tvmaze_client.get_episode(
                                result.tvmaze_id, file_preview.season, file_preview.episode
                            )
                        formatted = self._renamer.formatter.format_episode(
                            op.media_info, result, ep_result
                        )
                        output_dir = self.config.directories.tv.output
                        new_dest = output_dir / formatted.relative_path / formatted.filename
                        new_dest_path = str(new_dest)
                        new_dest_filename = new_dest.name
                        op.destination = new_dest
                        op.tvmaze_show = result
                        op.tvmaze_episode = ep_result
                        self._serialize_operations()
            except Exception as e:
                logger.error(f"TVMaze retry failed for '{lookup_title}': {e}")

        # Only update if we got a result from the API
        if new_title != file_preview.title or new_year != file_preview.year or poster_url:
            file_preview.title = new_title
            file_preview.year = new_year
            if poster_url:
                file_preview.poster_url = poster_url
            file_preview.destination_filename = new_dest_filename
            file_preview.destination_path = new_dest_path

            # Check if now correctly named
            try:
                source = Path(file_preview.source_path)
                dest = Path(new_dest_path)
                file_preview.already_correct = source.resolve() == dest.resolve()
                if file_preview.already_correct:
                    file_preview.status = "correct"
            except OSError:
                pass

            self.store.save_scan(scan)
            logger.info(f"Updated metadata for '{file_preview.source_filename}': {new_title} ({new_year})")

            # Update DB cache with manual override
            if op:
                self._cache_operation_to_db(Path(file_preview.source_path), op)
                # Mark as manual override in DB
                media_file = self.db.get_media_file(file_preview.source_path)
                if media_file:
                    match = self.db.get_match(media_file["id"])
                    if match:
                        self.db._conn.execute(
                            "UPDATE matches SET is_manual_override=1 WHERE id=?",
                            (match["id"],),
                        )
                        self.db._conn.commit()
        else:
            logger.warning(f"No API result for '{lookup_title}' — metadata unchanged")

        return file_preview.model_dump()

    def edit_file_destination(
        self, file_id: str, folder_name: str | None = None, filename: str | None = None
    ) -> dict | None:
        """Manually edit a file's destination folder and/or filename.

        Returns the updated file preview dict, or None if not found.
        """
        scan = self.store.load_scan()
        if not scan:
            return None

        file_preview = None
        for f in scan.files:
            if f.id == file_id:
                file_preview = f
                break
        if not file_preview:
            return None

        op = self._operations.get(file_id)
        current_dest = Path(file_preview.destination_path)

        # Determine the output root (strip the relative folder/filename)
        # e.g. /media/movies/Title (2020)/Title (2020).mkv -> /media/movies
        output_root = current_dest.parent.parent

        new_folder = sanitize_filename(folder_name) if folder_name else current_dest.parent.name
        new_file = sanitize_filename(filename) if filename else current_dest.name

        # Ensure the filename keeps its extension
        if not Path(new_file).suffix and current_dest.suffix:
            new_file += current_dest.suffix

        new_dest = output_root / new_folder / new_file

        file_preview.destination_path = str(new_dest)
        file_preview.destination_filename = new_dest.name

        # Update the operation cache
        if op:
            op.destination = new_dest
            self._serialize_operations()

        # Check if now correctly named
        try:
            source = Path(file_preview.source_path)
            file_preview.already_correct = source.resolve() == new_dest.resolve()
            if file_preview.already_correct:
                file_preview.status = "correct"
        except OSError:
            pass

        self.store.save_scan(scan)
        logger.info(f"Manual edit: {file_preview.source_filename} -> {new_folder}/{new_file}")
        return file_preview.model_dump()

    def list_trash(self) -> list[dict]:
        """List files in the duplicates/trash folder."""
        dup_folder = self.config.duplicates.duplicates_folder
        if not dup_folder or not dup_folder.exists():
            return []

        files = []
        for f in sorted(dup_folder.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "size_human": format_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        return files

    def delete_trash_file(self, filename: str) -> bool:
        """Delete a single file from the duplicates/trash folder."""
        dup_folder = self.config.duplicates.duplicates_folder
        if not dup_folder:
            return False

        # Reject path traversal attempts
        if "/" in filename or "\\" in filename or ".." in filename:
            logger.warning(f"Path traversal attempt in trash delete: {filename}")
            return False

        target = dup_folder / filename
        # Security: ensure the resolved path is inside the duplicates folder
        try:
            target.resolve().relative_to(dup_folder.resolve())
        except ValueError:
            logger.warning(f"Path traversal attempt in trash delete: {filename}")
            return False

        if target.exists() and target.is_file():
            target.unlink()
            logger.info(f"Deleted from trash: {filename}")
            return True
        return False

    def empty_trash(self) -> int:
        """Delete all files in the duplicates/trash folder. Returns count deleted."""
        dup_folder = self.config.duplicates.duplicates_folder
        if not dup_folder or not dup_folder.exists():
            return 0

        count = 0
        for f in dup_folder.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except OSError as e:
                    logger.error(f"Failed to delete {f.name}: {e}")
        logger.info(f"Emptied trash: {count} files deleted")
        return count

    # Library folder deduplication
    async def run_library_scan(self) -> None:
        """Scan library directories for case-insensitive duplicate folders."""
        scan_id = str(uuid.uuid4())
        scan = LibraryScanResult(
            scan_id=scan_id,
            started_at=datetime.now().isoformat(),
            status="running",
        )
        self.store.save_library_scan(scan)

        try:
            dedup = LibraryDeduplicator()
            all_groups: list[FolderMergePreview] = []

            # Scan movies output directory
            movies_output = self.config.directories.movies.output
            if movies_output.exists():
                logger.info(f"Library scan: movies at {movies_output}")
                groups = dedup.scan_directory(movies_output, recursive=False)
                for g in groups:
                    all_groups.append(FolderMergePreview(
                        id=str(uuid.uuid4()),
                        canonical_path=str(g.canonical),
                        canonical_name=g.canonical.name,
                        duplicate_paths=[str(d) for d in g.duplicates],
                        duplicate_names=[d.name for d in g.duplicates],
                        canonical_file_count=g.canonical_file_count,
                        canonical_size=g.canonical_size,
                        canonical_size_human=format_size(g.canonical_size),
                        duplicate_file_count=g.duplicate_file_count,
                        duplicate_size=g.duplicate_size,
                        duplicate_size_human=format_size(g.duplicate_size),
                        conflicts=g.conflicts,
                        media_type="movie",
                    ))

            # Scan TV output directory (recursive for season folders)
            tv_output = self.config.directories.tv.output
            if tv_output.exists():
                logger.info(f"Library scan: TV at {tv_output}")
                groups = dedup.scan_directory(tv_output, recursive=True)
                for g in groups:
                    all_groups.append(FolderMergePreview(
                        id=str(uuid.uuid4()),
                        canonical_path=str(g.canonical),
                        canonical_name=g.canonical.name,
                        duplicate_paths=[str(d) for d in g.duplicates],
                        duplicate_names=[d.name for d in g.duplicates],
                        canonical_file_count=g.canonical_file_count,
                        canonical_size=g.canonical_size,
                        canonical_size_human=format_size(g.canonical_size),
                        duplicate_file_count=g.duplicate_file_count,
                        duplicate_size=g.duplicate_size,
                        duplicate_size_human=format_size(g.duplicate_size),
                        conflicts=g.conflicts,
                        media_type="tv",
                    ))

            # Scan for misnamed folders
            folder_scanner = LibraryFolderScanner()
            all_renames: list[FolderRenamePreview] = []

            if movies_output.exists():
                logger.info(f"Library scan: checking movie folder names at {movies_output}")
                proposals = folder_scanner.find_misnamed_folders(movies_output, "movie")
                for p in proposals:
                    # Look up correct title via OMDb
                    if self._omdb_client and p.title:
                        try:
                            result = await self._omdb_client.find_best_match(p.title, p.year)
                            if result:
                                from ..utils import sanitize_filename as _sanitize
                                clean_title = _sanitize(result.title)
                                year = result.year or p.year
                                p.proposed_name = f"{clean_title} ({year})" if year else clean_title
                                p.title = result.title
                                p.year = year
                        except Exception as e:
                            logger.debug(f"OMDb lookup failed for '{p.title}': {e}")

                    # Skip if proposed name matches current name
                    if p.proposed_name and p.proposed_name != p.current_name:
                        all_renames.append(FolderRenamePreview(
                            id=str(uuid.uuid4()),
                            current_path=str(p.current_path),
                            current_name=p.current_name,
                            proposed_name=p.proposed_name,
                            title=p.title,
                            year=p.year,
                            media_type="movie",
                            file_count=p.file_count,
                            total_size=p.total_size,
                            total_size_human=format_size(p.total_size),
                        ))

            if tv_output.exists():
                logger.info(f"Library scan: checking TV folder names at {tv_output}")
                proposals = folder_scanner.find_misnamed_folders(tv_output, "tv")
                for p in proposals:
                    # Look up correct title via TVMaze
                    if self._tvmaze_client and p.title:
                        try:
                            result = await self._tvmaze_client.find_best_match(p.title, p.year)
                            if result:
                                from ..utils import sanitize_filename as _sanitize
                                p.proposed_name = _sanitize(result.name)
                                p.title = result.name
                        except Exception as e:
                            logger.debug(f"TVMaze lookup failed for '{p.title}': {e}")

                    if p.proposed_name and p.proposed_name != p.current_name:
                        all_renames.append(FolderRenamePreview(
                            id=str(uuid.uuid4()),
                            current_path=str(p.current_path),
                            current_name=p.current_name,
                            proposed_name=p.proposed_name,
                            title=p.title,
                            year=p.year,
                            media_type="tv",
                            file_count=p.file_count,
                            total_size=p.total_size,
                            total_size_human=format_size(p.total_size),
                        ))

            scan.groups = all_groups
            scan.folder_renames = all_renames
            scan.status = "completed"
            scan.completed_at = datetime.now().isoformat()
            logger.info(
                f"Library scan complete: {len(all_groups)} duplicate folder groups, "
                f"{len(all_renames)} misnamed folders"
            )

        except Exception as e:
            logger.error(f"Library scan failed: {e}", exc_info=True)
            scan.status = "failed"
            scan.error = "Library scan failed. Check server logs."
            scan.completed_at = datetime.now().isoformat()

        self.store.save_library_scan(scan)
        self.scanning = False

    async def execute_library_merges(self) -> dict:
        """Execute all approved folder merges and folder renames."""
        scan = self.store.load_library_scan()
        if not scan:
            return {"error": "No library scan results"}

        dedup = LibraryDeduplicator()
        results = {"merged": 0, "moved_files": 0, "renamed": 0, "failed": 0, "errors": []}

        # Execute folder merges
        for group in scan.groups:
            if group.status != "approved":
                continue

            canonical = Path(group.canonical_path)
            group_moved = 0
            group_failed = False

            for dup_path_str in group.duplicate_paths:
                dup_path = Path(dup_path_str)
                result = dedup.execute_merge(canonical, dup_path)
                group_moved += result.moved
                if result.failed > 0:
                    group_failed = True
                    results["errors"].extend(result.errors)

            if group_failed:
                group.status = "failed"
                results["failed"] += 1
            else:
                group.status = "completed"
                results["merged"] += 1

            results["moved_files"] += group_moved

        # Execute folder renames
        for rename in scan.folder_renames:
            if rename.status != "approved":
                continue

            current = Path(rename.current_path)
            success = LibraryFolderScanner.execute_folder_rename(
                current, rename.proposed_name
            )
            if success:
                rename.status = "completed"
                results["renamed"] += 1
            else:
                rename.status = "failed"
                results["failed"] += 1
                results["errors"].append(f"Failed to rename: {rename.current_name}")

        self.store.save_library_scan(scan)

        # Send Discord notification
        await self._notifier.library_cleanup_completed(
            merged=results["merged"],
            moved_files=results["moved_files"],
            failed=results["failed"],
            errors=results.get("errors"),
            renamed=results["renamed"],
        )

        return results


def _verify_api_key(x_api_key: str = Header(default="")):
    """Dependency to verify API key if configured."""
    required_key = get_api_key()
    if not required_key:
        return  # No API key configured, allow access
    if not x_api_key or not hmac.compare_digest(x_api_key, required_key):
        raise HTTPException(401, "Invalid or missing API key")


def _verify_delete_code(request: Request, x_delete_code: str = Header(default="")):
    """Dependency to verify delete auth code via header."""
    passphrase = get_passphrase()
    if not passphrase:
        return  # No passphrase configured, allow delete

    client_ip = request.client.host if request.client else "unknown"
    if not delete_rate_limiter.check(client_ip):
        raise HTTPException(429, "Too many attempts. Try again later.")

    if not x_delete_code or not verify_code(passphrase, x_delete_code):
        raise HTTPException(403, "Invalid or expired delete code")


def create_app(config: Config, data_dir: Path) -> FastAPI:
    """Create the FastAPI application."""
    # Attach log buffer to root logger to capture all app logs for the activity panel
    log_buffer.setLevel(logging.INFO)
    log_buffer.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(log_buffer)

    web = RenamarrWeb(config, data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await web.startup()
        yield
        await web.shutdown()

    app = FastAPI(title="Renamarr", lifespan=lifespan)

    # Security headers middleware
    app.add_middleware(SecurityHeadersMiddleware)

    # Serve static files
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_file = static_dir / "index.html"
        return index_file.read_text(encoding="utf-8")

    @app.get("/api/status", dependencies=[Depends(_verify_api_key)])
    async def status() -> StatusResponse:
        scan = web.store.load_scan()
        resp = StatusResponse(
            version=__version__,
            scanning=web.scanning,
            dry_run=config.options.dry_run,
            auto_approve_threshold=config.options.auto_approve_threshold,
            scheduled_scan=config.options.scheduled_scan,
        )
        if web._next_scan_at:
            resp.next_scan_at = web._next_scan_at.isoformat()
        if scan:
            resp.current_scan_id = scan.scan_id
            resp.total_files = len(scan.files)
            resp.pending = sum(1 for f in scan.files if f.status == "pending")
            resp.approved = sum(1 for f in scan.files if f.status == "approved")
            resp.rejected = sum(1 for f in scan.files if f.status == "rejected")
            resp.ignored = sum(1 for f in scan.files if f.status == "ignored")
            resp.auto_approved = sum(
                1 for f in scan.files
                if f.status == "approved" and f.confidence >= config.options.auto_approve_threshold > 0
            )
            resp.completed = sum(1 for f in scan.files if f.status == "completed")
            resp.failed = sum(1 for f in scan.files if f.status == "failed")
        return resp

    @app.post("/api/scan", dependencies=[Depends(_verify_api_key)])
    async def trigger_scan(request: Request):
        if web.scanning:
            raise HTTPException(409, "Scan already in progress")
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        media_type = body.get("media_type", "all")
        if media_type not in ("all", "movies", "tv"):
            raise HTTPException(400, "media_type must be 'all', 'movies', or 'tv'")
        web.scanning = True
        web._scan_task = asyncio.create_task(web.run_scan(media_type))
        return {"message": f"Scan started ({media_type})"}

    @app.post("/api/scan/cancel", dependencies=[Depends(_verify_api_key)])
    async def cancel_scan():
        if not web.scanning:
            raise HTTPException(409, "No scan in progress")
        if web._scan_task and not web._scan_task.done():
            web._scan_task.cancel()
            try:
                await web._scan_task
            except asyncio.CancelledError:
                pass
        web.scanning = False
        # Update persisted scan status
        scan = web.store.load_scan()
        if scan and scan.status == "running":
            scan.status = "cancelled"
            scan.completed_at = datetime.now().isoformat()
            web.store.save_scan(scan)
        logger.info("Scan cancelled by user")
        return {"message": "Scan cancelled"}

    @app.get("/api/scan/current", dependencies=[Depends(_verify_api_key)])
    async def current_scan():
        scan = web.store.load_scan()
        if not scan:
            raise HTTPException(404, "No scan results")
        return scan

    @app.get("/api/logs", dependencies=[Depends(_verify_api_key)])
    async def get_logs(after: int = 0):
        return {"logs": log_buffer.get_logs(after)}

    @app.get("/api/history", dependencies=[Depends(_verify_api_key)])
    async def scan_history():
        return web.store.load_history()

    @app.get("/api/history/{scan_id}", dependencies=[Depends(_verify_api_key)])
    async def get_archived_scan(scan_id: str):
        scan = web.store.load_archive(scan_id)
        if not scan:
            raise HTTPException(404, "Archived scan not found")
        return scan

    @app.get("/api/history/{scan_id}/download")
    async def download_archived_scan(scan_id: str, api_key: str = ""):
        # Accept API key as query param for browser downloads
        required_key = get_api_key()
        if required_key:
            if not api_key or not hmac.compare_digest(api_key, required_key):
                raise HTTPException(401, "Invalid or missing API key")
        scan = web.store.load_archive(scan_id)
        if not scan:
            raise HTTPException(404, "Archived scan not found")
        from fastapi.responses import Response
        date_str = scan.completed_at[:10] if scan.completed_at else scan.started_at[:10]
        filename = f"renamarr-scan-{date_str}.json"
        return Response(
            content=scan.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _persist_decision(file_id: str, status: str) -> None:
        """Save a user decision to the DB for the given file."""
        scan = web.store.load_scan()
        if not scan:
            return
        fp = next((f for f in scan.files if f.id == file_id), None)
        if fp:
            web.db.save_decision(
                file_path=fp.source_path,
                file_size=fp.file_size,
                filename=fp.source_filename,
                media_type=fp.media_type,
                status=status,
                chosen_destination=fp.destination_path,
            )

    @app.post("/api/files/{file_id}/approve", dependencies=[Depends(_verify_api_key)])
    async def approve_file(file_id: str):
        if not web.store.update_file_status(file_id, "approved"):
            raise HTTPException(404, "File not found")
        _persist_decision(file_id, "approved")
        return {"status": "approved"}

    @app.post("/api/files/{file_id}/reject", dependencies=[Depends(_verify_api_key)])
    async def reject_file(file_id: str):
        if not web.store.update_file_status(file_id, "rejected"):
            raise HTTPException(404, "File not found")
        _persist_decision(file_id, "rejected")
        return {"status": "rejected"}

    @app.post("/api/files/{file_id}/pending", dependencies=[Depends(_verify_api_key)])
    async def reset_file(file_id: str):
        if not web.store.update_file_status(file_id, "pending"):
            raise HTTPException(404, "File not found")
        # Remove decision from DB when resetting to pending
        scan = web.store.load_scan()
        if scan:
            fp = next((f for f in scan.files if f.id == file_id), None)
            if fp:
                web.db.remove_decision(fp.source_path)
        return {"status": "pending"}

    @app.post("/api/files/{file_id}/ignore", dependencies=[Depends(_verify_api_key)])
    async def ignore_file(file_id: str):
        if not web.store.update_file_status(file_id, "ignored"):
            raise HTTPException(404, "File not found")
        _persist_decision(file_id, "ignored")
        return {"status": "ignored"}

    @app.post("/api/files/{file_id}/retry", dependencies=[Depends(_verify_api_key)])
    async def retry_file_lookup(file_id: str, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        title = body.get("title")
        year = body.get("year")
        if year is not None:
            try:
                year = int(year)
            except (ValueError, TypeError):
                year = None
        result = await web.retry_file_lookup(file_id, title=title, year=year)
        if not result:
            raise HTTPException(404, "File not found")
        return result

    @app.post("/api/files/{file_id}/edit-destination", dependencies=[Depends(_verify_api_key)])
    async def edit_file_destination(file_id: str, request: Request):
        body = await request.json()
        folder_name = body.get("folder_name")
        filename = body.get("filename")
        if not folder_name and not filename:
            raise HTTPException(400, "Provide folder_name and/or filename")
        result = web.edit_file_destination(file_id, folder_name=folder_name, filename=filename)
        if not result:
            raise HTTPException(404, "File not found")
        return result

    @app.post("/api/files/approve-all", dependencies=[Depends(_verify_api_key)])
    async def approve_all():
        count = web.store.update_all_pending("approved")
        return {"approved": count}

    @app.post("/api/files/reject-all", dependencies=[Depends(_verify_api_key)])
    async def reject_all():
        count = web.store.update_all_pending("rejected")
        return {"rejected": count}

    @app.post("/api/execute", dependencies=[Depends(_verify_api_key)])
    async def execute():
        if web.scanning:
            raise HTTPException(409, "Cannot execute while scan is running")
        results = await web.execute_approved()
        return results

    # Trash management endpoints
    @app.get("/api/trash", dependencies=[Depends(_verify_api_key)])
    async def list_trash():
        files = web.list_trash()
        total_size = sum(f["size"] for f in files)
        return {
            "files": files,
            "count": len(files),
            "total_size": total_size,
            "total_size_human": format_size(total_size),
            "delete_auth_required": bool(get_passphrase()),
        }

    @app.delete("/api/trash/{filename}", dependencies=[Depends(_verify_api_key), Depends(_verify_delete_code)])
    async def delete_trash_file(filename: str):
        if not web.delete_trash_file(filename):
            raise HTTPException(404, "File not found")
        return {"deleted": filename}

    @app.delete("/api/trash", dependencies=[Depends(_verify_api_key), Depends(_verify_delete_code)])
    async def empty_trash():
        count = web.empty_trash()
        return {"deleted": count}

    # Library dedup endpoints
    @app.post("/api/library/scan", dependencies=[Depends(_verify_api_key)])
    async def trigger_library_scan():
        if web.scanning:
            raise HTTPException(409, "Scan already in progress")
        web.scanning = True
        web._scan_task = asyncio.create_task(web.run_library_scan())
        return {"message": "Library scan started"}

    @app.get("/api/library/scan/current", dependencies=[Depends(_verify_api_key)])
    async def current_library_scan():
        scan = web.store.load_library_scan()
        if not scan:
            raise HTTPException(404, "No library scan results")
        return scan

    @app.post("/api/library/groups/{group_id}/approve", dependencies=[Depends(_verify_api_key)])
    async def approve_merge_group(group_id: str):
        if not web.store.update_merge_group_status(group_id, "approved"):
            raise HTTPException(404, "Group not found")
        return {"status": "approved"}

    @app.post("/api/library/groups/{group_id}/skip", dependencies=[Depends(_verify_api_key)])
    async def skip_merge_group(group_id: str):
        if not web.store.update_merge_group_status(group_id, "skipped"):
            raise HTTPException(404, "Group not found")
        return {"status": "skipped"}

    @app.post("/api/library/groups/{group_id}/pending", dependencies=[Depends(_verify_api_key)])
    async def reset_merge_group(group_id: str):
        if not web.store.update_merge_group_status(group_id, "pending"):
            raise HTTPException(404, "Group not found")
        return {"status": "pending"}

    @app.post("/api/library/groups/approve-all", dependencies=[Depends(_verify_api_key)])
    async def approve_all_merge_groups():
        count = web.store.update_all_merge_groups("pending", "approved")
        return {"approved": count}

    @app.post("/api/library/renames/{rename_id}/approve", dependencies=[Depends(_verify_api_key)])
    async def approve_folder_rename(rename_id: str):
        if not web.store.update_folder_rename_status(rename_id, "approved"):
            raise HTTPException(404, "Rename not found")
        return {"status": "approved"}

    @app.post("/api/library/renames/{rename_id}/skip", dependencies=[Depends(_verify_api_key)])
    async def skip_folder_rename(rename_id: str):
        if not web.store.update_folder_rename_status(rename_id, "skipped"):
            raise HTTPException(404, "Rename not found")
        return {"status": "skipped"}

    @app.post("/api/library/renames/{rename_id}/pending", dependencies=[Depends(_verify_api_key)])
    async def reset_folder_rename(rename_id: str):
        if not web.store.update_folder_rename_status(rename_id, "pending"):
            raise HTTPException(404, "Rename not found")
        return {"status": "pending"}

    @app.post("/api/library/renames/approve-all", dependencies=[Depends(_verify_api_key)])
    async def approve_all_folder_renames():
        scan = web.store.load_library_scan()
        if not scan:
            return {"approved": 0}
        count = 0
        for rename in scan.folder_renames:
            if rename.status == "pending":
                rename.status = "approved"
                count += 1
        web.store.save_library_scan(scan)
        return {"approved": count}

    @app.post("/api/library/renames/{rename_id}/edit", dependencies=[Depends(_verify_api_key)])
    async def edit_folder_rename(rename_id: str, request: Request):
        body = await request.json()
        new_name = body.get("proposed_name", "").strip()
        if not new_name:
            raise HTTPException(400, "proposed_name is required")
        if "/" in new_name or "\\" in new_name or ".." in new_name:
            raise HTTPException(400, "Invalid folder name")
        if not web.store.update_folder_rename_proposed_name(rename_id, new_name):
            raise HTTPException(404, "Rename not found")
        return {"proposed_name": new_name}

    @app.post("/api/library/execute", dependencies=[Depends(_verify_api_key)])
    async def execute_library_merges():
        if web.scanning:
            raise HTTPException(409, "Cannot execute while scan is running")
        results = await web.execute_library_merges()
        return results

    return app
