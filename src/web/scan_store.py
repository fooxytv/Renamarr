"""JSON-file-backed storage for scan results."""

import json
import logging
from pathlib import Path

from .models import ScanResult

logger = logging.getLogger(__name__)


class ScanStore:
    """Persists scan results to a JSON file."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._scan_file = self.data_dir / "current_scan.json"
        self._history_file = self.data_dir / "scan_history.json"

    def save_scan(self, scan: ScanResult) -> None:
        """Save the current scan result."""
        with open(self._scan_file, "w", encoding="utf-8") as f:
            f.write(scan.model_dump_json(indent=2))

    def load_scan(self) -> ScanResult | None:
        """Load the current scan result."""
        if not self._scan_file.exists():
            return None
        try:
            with open(self._scan_file, encoding="utf-8") as f:
                data = json.load(f)
            return ScanResult(**data)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Failed to load scan: {e}")
            return None

    def update_file_status(self, file_id: str, status: str) -> bool:
        """Update the status of a single file in the current scan."""
        scan = self.load_scan()
        if not scan:
            return False

        for file in scan.files:
            if file.id == file_id:
                file.status = status
                self.save_scan(scan)
                return True
        return False

    def update_all_pending(self, status: str) -> int:
        """Update all pending files to a new status. Returns count updated."""
        scan = self.load_scan()
        if not scan:
            return 0

        count = 0
        for file in scan.files:
            if file.status == "pending" and not file.already_correct:
                file.status = status
                count += 1
        self.save_scan(scan)
        return count

    def save_to_history(self, scan: ScanResult) -> None:
        """Append a scan summary to history."""
        history = self._load_history()
        summary = {
            "scan_id": scan.scan_id,
            "started_at": scan.started_at,
            "completed_at": scan.completed_at,
            "status": scan.status,
            "total_files": len(scan.files),
            "duplicates": len(scan.duplicates),
        }
        history.insert(0, summary)
        # Keep last 50
        history = history[:50]
        with open(self._history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    def _load_history(self) -> list[dict]:
        """Load scan history."""
        if not self._history_file.exists():
            return []
        try:
            with open(self._history_file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return []

    def load_history(self) -> list[dict]:
        """Load scan history."""
        return self._load_history()
