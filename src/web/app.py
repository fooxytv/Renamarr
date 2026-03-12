"""FastAPI web application for Renamarr."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..auth import get_passphrase, verify_code
from ..config import Config
from ..duplicates import DuplicateHandler
from ..formatter import PlexFormatter
from ..notifications import DiscordNotifier
from ..omdb_client import OMDbClient
from ..renamer import RenameOperation, RenamerService
from ..tvmaze_client import TVMazeClient
from ..utils import format_size
from .models import (
    DuplicateGroupPreview,
    FilePreview,
    ScanResult,
    StatusResponse,
)
from .scan_store import ScanStore

logger = logging.getLogger(__name__)


class RenamarrWeb:
    """Web application state and services."""

    def __init__(self, config: Config, data_dir: Path):
        self.config = config
        self.store = ScanStore(data_dir)
        self.scanning = False
        self._scan_lock = asyncio.Lock()
        self._omdb_client: OMDbClient | None = None
        self._tvmaze_client: TVMazeClient | None = None
        self._renamer: RenamerService | None = None
        self._notifier = DiscordNotifier()
        # Cache operations between preview and execute
        self._operations: dict[str, RenameOperation] = {}

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

    async def shutdown(self) -> None:
        """Clean up API clients."""
        if self._omdb_client:
            await self._omdb_client.__aexit__(None, None, None)
        if self._tvmaze_client:
            await self._tvmaze_client.__aexit__(None, None, None)

    async def run_scan(self) -> None:
        """Run a scan in the background."""
        scan_id = str(uuid.uuid4())[:8]
        scan = ScanResult(
            scan_id=scan_id,
            started_at=datetime.now().isoformat(),
            status="running",
        )
        self.store.save_scan(scan)
        self._operations.clear()
        scan_start = datetime.now()

        try:
            all_files: list[FilePreview] = []
            all_duplicates: list[DuplicateGroupPreview] = []

            # Scan movies
            movies_dir = self.config.directories.movies.watch
            if movies_dir.exists():
                logger.info(f"Scanning movies: {movies_dir}")
                ops, dups = await self._renamer.preview_directory(movies_dir, "movie")
                files, dup_previews = self._convert_results(ops, dups)
                all_files.extend(files)
                all_duplicates.extend(dup_previews)

            # Scan TV
            tv_dir = self.config.directories.tv.watch
            if tv_dir.exists():
                logger.info(f"Scanning TV: {tv_dir}")
                ops, dups = await self._renamer.preview_directory(tv_dir, "episode")
                files, dup_previews = self._convert_results(ops, dups)
                all_files.extend(files)
                all_duplicates.extend(dup_previews)

            scan.files = all_files
            scan.duplicates = all_duplicates
            scan.status = "completed"
            scan.completed_at = datetime.now().isoformat()
            logger.info(f"Scan complete: {len(all_files)} files, {len(all_duplicates)} duplicate groups")

            # Send Discord notification
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

        except Exception as e:
            logger.error(f"Scan failed: {e}")
            scan.status = "failed"
            scan.error = str(e)
            scan.completed_at = datetime.now().isoformat()
            await self._notifier.scan_failed(str(e))

        self.store.save_scan(scan)
        self.store.save_to_history(scan)
        self.scanning = False

    def _convert_results(
        self,
        operations: list[RenameOperation],
        duplicate_groups: list,
    ) -> tuple[list[FilePreview], list[DuplicateGroupPreview]]:
        """Convert internal results to API models."""
        files = []
        for op in operations:
            file_id = str(uuid.uuid4())[:8]
            # Check if already correctly named
            try:
                already_correct = op.source.resolve() == op.destination.resolve()
            except OSError:
                already_correct = False

            title = ""
            year = None
            season = None
            episode = None

            if op.media_info.is_movie:
                title = op.omdb_movie.title if op.omdb_movie else (op.media_info.title or "Unknown")
                year = op.omdb_movie.year if op.omdb_movie else op.media_info.year
            elif op.media_info.is_episode:
                title = op.tvmaze_show.name if op.tvmaze_show else (op.media_info.show_name or "Unknown")
                season = op.media_info.season
                episode = op.media_info.episode

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
                resolution=op.media_info.quality.resolution,
                quality_score=op.media_info.quality.quality_score(),
                file_size=op.media_info.quality.file_size,
                status="correct" if already_correct else "pending",
                already_correct=already_correct,
            )
            files.append(preview)
            # Cache the operation for later execution
            self._operations[file_id] = op

        # Convert duplicate groups
        dup_previews = []
        for group in duplicate_groups:
            group_id = str(uuid.uuid4())[:8]
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
                    renames.append({
                        "source": file.source_filename,
                        "destination": file.destination_filename,
                        "media_type": file.media_type,
                    })
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

        target = dup_folder / filename
        # Security: ensure the file is actually inside the duplicates folder
        try:
            target.resolve().relative_to(dup_folder.resolve())
        except ValueError:
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


def create_app(config: Config, data_dir: Path) -> FastAPI:
    """Create the FastAPI application."""
    web = RenamarrWeb(config, data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await web.startup()
        yield
        await web.shutdown()

    app = FastAPI(title="Renamarr", lifespan=lifespan)

    # Serve static files
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_file = static_dir / "index.html"
        return index_file.read_text(encoding="utf-8")

    @app.get("/api/status")
    async def status() -> StatusResponse:
        scan = web.store.load_scan()
        resp = StatusResponse(
            scanning=web.scanning,
            dry_run=config.options.dry_run,
        )
        if scan:
            resp.current_scan_id = scan.scan_id
            resp.total_files = len(scan.files)
            resp.pending = sum(1 for f in scan.files if f.status == "pending")
            resp.approved = sum(1 for f in scan.files if f.status == "approved")
            resp.rejected = sum(1 for f in scan.files if f.status == "rejected")
            resp.completed = sum(1 for f in scan.files if f.status == "completed")
            resp.failed = sum(1 for f in scan.files if f.status == "failed")
        return resp

    @app.post("/api/scan")
    async def trigger_scan():
        if web.scanning:
            raise HTTPException(409, "Scan already in progress")
        web.scanning = True
        asyncio.create_task(web.run_scan())
        return {"message": "Scan started"}

    @app.get("/api/scan/current")
    async def current_scan():
        scan = web.store.load_scan()
        if not scan:
            raise HTTPException(404, "No scan results")
        return scan

    @app.get("/api/history")
    async def scan_history():
        return web.store.load_history()

    @app.post("/api/files/{file_id}/approve")
    async def approve_file(file_id: str):
        if not web.store.update_file_status(file_id, "approved"):
            raise HTTPException(404, "File not found")
        return {"status": "approved"}

    @app.post("/api/files/{file_id}/reject")
    async def reject_file(file_id: str):
        if not web.store.update_file_status(file_id, "rejected"):
            raise HTTPException(404, "File not found")
        return {"status": "rejected"}

    @app.post("/api/files/{file_id}/pending")
    async def reset_file(file_id: str):
        if not web.store.update_file_status(file_id, "pending"):
            raise HTTPException(404, "File not found")
        return {"status": "pending"}

    @app.post("/api/files/approve-all")
    async def approve_all():
        count = web.store.update_all_pending("approved")
        return {"approved": count}

    @app.post("/api/files/reject-all")
    async def reject_all():
        count = web.store.update_all_pending("rejected")
        return {"rejected": count}

    @app.post("/api/execute")
    async def execute():
        if web.scanning:
            raise HTTPException(409, "Cannot execute while scan is running")
        results = await web.execute_approved()
        return results

    # Trash management endpoints
    @app.get("/api/trash")
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

    @app.delete("/api/trash/{filename}")
    async def delete_trash_file(filename: str, code: str = ""):
        passphrase = get_passphrase()
        if passphrase:
            if not code or not verify_code(passphrase, code):
                raise HTTPException(403, "Invalid or expired delete code")
        if not web.delete_trash_file(filename):
            raise HTTPException(404, "File not found")
        return {"deleted": filename}

    @app.delete("/api/trash")
    async def empty_trash(code: str = ""):
        passphrase = get_passphrase()
        if passphrase:
            if not code or not verify_code(passphrase, code):
                raise HTTPException(403, "Invalid or expired delete code")
        count = web.empty_trash()
        return {"deleted": count}

    return app
