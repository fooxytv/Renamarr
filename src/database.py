"""SQLite database for caching API results and persisting user decisions."""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Fingerprint every file discovered in watch directories
CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    file_size INTEGER,
    mtime REAL,
    media_type TEXT,
    parsed_title TEXT,
    parsed_year INTEGER,
    parsed_show_name TEXT,
    parsed_season INTEGER,
    parsed_episode INTEGER,
    resolution TEXT,
    quality_score INTEGER DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Cached API lookup results linked to a media_file
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL UNIQUE REFERENCES media_files(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    -- OMDb fields (movies)
    imdb_id TEXT,
    omdb_title TEXT,
    omdb_year INTEGER,
    omdb_plot TEXT,
    omdb_poster TEXT,
    -- TVMaze show fields
    tvmaze_show_id INTEGER,
    tvmaze_show_name TEXT,
    tvmaze_show_premiered TEXT,
    tvmaze_show_poster TEXT,
    tvmaze_show_summary TEXT,
    -- TVMaze episode fields
    tvmaze_episode_id INTEGER,
    tvmaze_episode_name TEXT,
    tvmaze_episode_airdate TEXT,
    tvmaze_episode_summary TEXT,
    tvmaze_season INTEGER,
    tvmaze_episode_number INTEGER,
    -- Computed destination
    destination_path TEXT,
    -- Lookup metadata
    lookup_title TEXT,
    lookup_year INTEGER,
    is_manual_override INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Persistent user decisions that survive re-scans
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_size INTEGER,
    filename TEXT NOT NULL,
    media_type TEXT,
    status TEXT NOT NULL,
    chosen_destination TEXT,
    decided_at TEXT NOT NULL
);

-- Inventory of output directory contents
CREATE TABLE IF NOT EXISTS library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    file_size INTEGER,
    mtime REAL,
    media_type TEXT,
    normalized_path TEXT,
    last_scanned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_filename_size ON decisions(filename, file_size);
CREATE INDEX IF NOT EXISTS idx_library_normalized_path ON library(normalized_path);
"""


def _normalize_path(path: str) -> str:
    """Normalize a path for comparison: lowercase, strip punctuation."""
    return re.sub(r'[^a-z0-9/\\]', '', path.lower())


class RenamarrDB:
    """SQLite database for caching API results and persisting decisions."""

    def __init__(self, data_dir: Path):
        db_path = data_dir / "renamarr.db"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        logger.info(f"Database opened: {db_path}")

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        self._conn.executescript(SCHEMA_SQL)
        row = self._conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not row:
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Media files ---

    def upsert_media_file(
        self,
        path: str,
        filename: str,
        file_size: int,
        mtime: float,
        media_type: str | None,
        parsed_title: str | None = None,
        parsed_year: int | None = None,
        parsed_show_name: str | None = None,
        parsed_season: int | None = None,
        parsed_episode: int | None = None,
        resolution: str | None = None,
        quality_score: int = 0,
    ) -> int:
        """Insert or update a media file record. Returns the row id."""
        now = datetime.now().isoformat()
        row = self._conn.execute(
            "SELECT id FROM media_files WHERE path = ?", (path,)
        ).fetchone()
        if row:
            self._conn.execute(
                """UPDATE media_files SET filename=?, file_size=?, mtime=?,
                   media_type=?, parsed_title=?, parsed_year=?,
                   parsed_show_name=?, parsed_season=?, parsed_episode=?,
                   resolution=?, quality_score=?, last_seen_at=?
                   WHERE id=?""",
                (filename, file_size, mtime, media_type, parsed_title,
                 parsed_year, parsed_show_name, parsed_season, parsed_episode,
                 resolution, quality_score, now, row["id"]),
            )
            self._conn.commit()
            return row["id"]
        else:
            cur = self._conn.execute(
                """INSERT INTO media_files
                   (path, filename, file_size, mtime, media_type,
                    parsed_title, parsed_year, parsed_show_name,
                    parsed_season, parsed_episode, resolution,
                    quality_score, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (path, filename, file_size, mtime, media_type, parsed_title,
                 parsed_year, parsed_show_name, parsed_season, parsed_episode,
                 resolution, quality_score, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_media_file(self, path: str) -> dict | None:
        """Get a media file by path."""
        row = self._conn.execute(
            "SELECT * FROM media_files WHERE path = ?", (path,)
        ).fetchone()
        return dict(row) if row else None

    def file_changed(self, path: str, file_size: int, mtime: float) -> bool:
        """Check if a file has changed since last scan (size/mtime differ).
        Returns True if file is new or changed, False if unchanged."""
        row = self._conn.execute(
            "SELECT file_size, mtime FROM media_files WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return True  # New file
        return row["file_size"] != file_size or abs(row["mtime"] - mtime) > 0.01

    # --- Matches (cached API results) ---

    def get_match(self, media_file_id: int) -> dict | None:
        """Get the cached match for a media file."""
        row = self._conn.execute(
            "SELECT * FROM matches WHERE media_file_id = ?", (media_file_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_match_by_path(self, file_path: str) -> dict | None:
        """Get cached match by file path (joins media_files + matches)."""
        row = self._conn.execute(
            """SELECT m.* FROM matches m
               JOIN media_files mf ON m.media_file_id = mf.id
               WHERE mf.path = ?""",
            (file_path,),
        ).fetchone()
        return dict(row) if row else None

    def save_movie_match(
        self,
        media_file_id: int,
        imdb_id: str,
        title: str,
        year: int | None,
        plot: str,
        poster: str | None,
        destination_path: str,
        lookup_title: str,
        lookup_year: int | None,
        is_manual: bool = False,
    ) -> None:
        """Save/update an OMDb match for a media file."""
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO matches
               (media_file_id, source, imdb_id, omdb_title, omdb_year,
                omdb_plot, omdb_poster, destination_path, lookup_title,
                lookup_year, is_manual_override, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_file_id) DO UPDATE SET
                source=excluded.source, imdb_id=excluded.imdb_id,
                omdb_title=excluded.omdb_title, omdb_year=excluded.omdb_year,
                omdb_plot=excluded.omdb_plot, omdb_poster=excluded.omdb_poster,
                destination_path=excluded.destination_path,
                lookup_title=excluded.lookup_title,
                lookup_year=excluded.lookup_year,
                is_manual_override=excluded.is_manual_override,
                updated_at=excluded.updated_at""",
            (media_file_id, "omdb", imdb_id, title, year, plot, poster,
             destination_path, lookup_title, lookup_year,
             1 if is_manual else 0, now, now),
        )
        self._conn.commit()

    def save_episode_match(
        self,
        media_file_id: int,
        show_id: int,
        show_name: str,
        premiered: str | None,
        show_poster: str | None,
        show_summary: str | None,
        episode_id: int | None,
        episode_name: str | None,
        airdate: str | None,
        episode_summary: str | None,
        season: int | None,
        episode_number: int | None,
        destination_path: str,
        lookup_title: str,
        lookup_year: int | None,
        is_manual: bool = False,
    ) -> None:
        """Save/update a TVMaze match for a media file."""
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO matches
               (media_file_id, source, tvmaze_show_id, tvmaze_show_name,
                tvmaze_show_premiered, tvmaze_show_poster, tvmaze_show_summary,
                tvmaze_episode_id, tvmaze_episode_name,
                tvmaze_episode_airdate, tvmaze_episode_summary,
                tvmaze_season, tvmaze_episode_number,
                destination_path, lookup_title, lookup_year,
                is_manual_override, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_file_id) DO UPDATE SET
                source=excluded.source,
                tvmaze_show_id=excluded.tvmaze_show_id,
                tvmaze_show_name=excluded.tvmaze_show_name,
                tvmaze_show_premiered=excluded.tvmaze_show_premiered,
                tvmaze_show_poster=excluded.tvmaze_show_poster,
                tvmaze_show_summary=excluded.tvmaze_show_summary,
                tvmaze_episode_id=excluded.tvmaze_episode_id,
                tvmaze_episode_name=excluded.tvmaze_episode_name,
                tvmaze_episode_airdate=excluded.tvmaze_episode_airdate,
                tvmaze_episode_summary=excluded.tvmaze_episode_summary,
                tvmaze_season=excluded.tvmaze_season,
                tvmaze_episode_number=excluded.tvmaze_episode_number,
                destination_path=excluded.destination_path,
                lookup_title=excluded.lookup_title,
                lookup_year=excluded.lookup_year,
                is_manual_override=excluded.is_manual_override,
                updated_at=excluded.updated_at""",
            (media_file_id, "tvmaze", show_id, show_name, premiered,
             show_poster, show_summary, episode_id, episode_name,
             airdate, episode_summary, season, episode_number,
             destination_path, lookup_title, lookup_year,
             1 if is_manual else 0, now, now),
        )
        self._conn.commit()

    def clear_match(self, media_file_id: int) -> None:
        """Remove cached match (forces re-lookup on next scan)."""
        self._conn.execute(
            "DELETE FROM matches WHERE media_file_id = ?", (media_file_id,)
        )
        self._conn.commit()

    # --- Decisions ---

    def get_decision(self, file_path: str) -> dict | None:
        """Get a decision by exact file path."""
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def find_decision(self, filename: str, file_size: int) -> dict | None:
        """Fuzzy-find a decision by filename + size (for moved files)."""
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE filename = ? AND file_size = ?",
            (filename, file_size),
        ).fetchone()
        return dict(row) if row else None

    def save_decision(
        self,
        file_path: str,
        file_size: int | None,
        filename: str,
        media_type: str | None,
        status: str,
        chosen_destination: str | None = None,
    ) -> None:
        """Save or update a user decision."""
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO decisions
               (file_path, file_size, filename, media_type, status,
                chosen_destination, decided_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(file_path) DO UPDATE SET
                file_size=excluded.file_size, filename=excluded.filename,
                media_type=excluded.media_type, status=excluded.status,
                chosen_destination=excluded.chosen_destination,
                decided_at=excluded.decided_at""",
            (file_path, file_size, filename, media_type, status,
             chosen_destination, now),
        )
        self._conn.commit()

    def remove_decision(self, file_path: str) -> None:
        """Remove a decision (e.g. when resetting to pending)."""
        self._conn.execute(
            "DELETE FROM decisions WHERE file_path = ?", (file_path,)
        )
        self._conn.commit()

    def get_all_decisions(self, status: str | None = None) -> list[dict]:
        """Get all decisions, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE status = ?", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM decisions").fetchall()
        return [dict(r) for r in rows]

    # --- Library inventory ---

    def rebuild_library(self, output_dirs: list[tuple[Path, str]]) -> int:
        """Scan output directories and rebuild the library table.

        Args:
            output_dirs: list of (directory_path, media_type) tuples.

        Returns:
            Number of files indexed.
        """
        from .utils import is_video_file

        now = datetime.now().isoformat()
        count = 0

        # Clear stale entries
        self._conn.execute("DELETE FROM library")

        for directory, media_type in output_dirs:
            if not directory.exists():
                continue
            for f in directory.rglob("*"):
                if not is_video_file(f):
                    continue
                try:
                    stat = f.stat()
                    path_str = str(f)
                    self._conn.execute(
                        """INSERT OR REPLACE INTO library
                           (path, filename, file_size, mtime, media_type,
                            normalized_path, last_scanned_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (path_str, f.name, stat.st_size, stat.st_mtime,
                         media_type, _normalize_path(path_str), now),
                    )
                    count += 1
                except OSError:
                    continue

        self._conn.commit()
        logger.info(f"Library index rebuilt: {count} files")
        return count

    def is_in_library(self, destination_path: str) -> bool:
        """Check if a destination path exists in the library (normalized comparison)."""
        norm = _normalize_path(destination_path)
        row = self._conn.execute(
            "SELECT 1 FROM library WHERE normalized_path = ?", (norm,)
        ).fetchone()
        return row is not None

    # --- Cleanup ---

    def remove_stale_files(self, current_paths: set[str]) -> int:
        """Remove media_files for paths no longer on disk. Cascades to matches."""
        if not current_paths:
            return 0
        rows = self._conn.execute("SELECT id, path FROM media_files").fetchall()
        stale = [r["id"] for r in rows if r["path"] not in current_paths]
        if stale:
            placeholders = ",".join("?" * len(stale))
            self._conn.execute(
                f"DELETE FROM media_files WHERE id IN ({placeholders})", stale
            )
            self._conn.commit()
            logger.info(f"Removed {len(stale)} stale media files from DB")
        return len(stale)
