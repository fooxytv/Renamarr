"""File system monitoring using watchdog."""

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Coroutine

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .utils import get_file_age, is_video_file

logger = logging.getLogger(__name__)


class MediaFileHandler(FileSystemEventHandler):
    """Handler for media file events."""

    def __init__(
        self,
        callback: Callable[[Path], Coroutine],
        min_file_age: int = 60,
        debounce_seconds: float = 5.0,
    ):
        """Initialize the handler.

        Args:
            callback: Async callback to process files
            min_file_age: Minimum file age in seconds before processing
            debounce_seconds: Debounce time for rapid changes
        """
        super().__init__()
        self.callback = callback
        self.min_file_age = min_file_age
        self.debounce_seconds = debounce_seconds

        # Track pending files and their last event time
        self._pending: dict[Path, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for scheduling callbacks."""
        self._loop = loop

    def on_created(self, event: FileCreatedEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return

        path = Path(event.src_path)
        if is_video_file(path):
            logger.debug(f"File created: {path}")
            self._schedule_processing(path)

    def on_moved(self, event: FileMovedEvent) -> None:
        """Handle file move events."""
        if event.is_directory:
            return

        path = Path(event.dest_path)
        if is_video_file(path):
            logger.debug(f"File moved to: {path}")
            self._schedule_processing(path)

    def _schedule_processing(self, path: Path) -> None:
        """Schedule a file for processing with debouncing."""
        self._pending[path] = time.time()

    def get_ready_files(self) -> list[Path]:
        """Get files that are ready for processing.

        Returns:
            List of file paths that have aged enough and debounce period passed
        """
        ready = []
        now = time.time()
        to_remove = []

        for path, event_time in self._pending.items():
            # Check debounce period
            if now - event_time < self.debounce_seconds:
                continue

            # Check if file exists and is old enough
            if not path.exists():
                to_remove.append(path)
                continue

            try:
                file_age = get_file_age(path)
                if file_age >= self.min_file_age:
                    ready.append(path)
                    to_remove.append(path)
            except OSError:
                to_remove.append(path)

        # Clean up processed/removed files
        for path in to_remove:
            del self._pending[path]

        return ready


class FileWatcher:
    """Watches directories for new media files."""

    def __init__(
        self,
        directories: list[Path],
        callback: Callable[[Path], Coroutine],
        min_file_age: int = 60,
        scan_interval: int = 300,
    ):
        """Initialize the file watcher.

        Args:
            directories: Directories to watch
            callback: Async callback to process files
            min_file_age: Minimum file age before processing
            scan_interval: Interval between periodic scans
        """
        self.directories = directories
        self.callback = callback
        self.min_file_age = min_file_age
        self.scan_interval = scan_interval

        self._observer: Observer | None = None
        self._handler: MediaFileHandler | None = None
        self._running = False

    async def start(self) -> None:
        """Start watching directories."""
        logger.info("Starting file watcher")

        # Create the event handler
        self._handler = MediaFileHandler(
            callback=self.callback,
            min_file_age=self.min_file_age,
        )
        self._handler.set_event_loop(asyncio.get_event_loop())

        # Create and start the observer
        self._observer = Observer()

        for directory in self.directories:
            if directory.exists():
                logger.info(f"Watching directory: {directory}")
                self._observer.schedule(
                    self._handler,
                    str(directory),
                    recursive=True,
                )
            else:
                logger.warning(f"Directory does not exist: {directory}")

        self._observer.start()
        self._running = True

        # Start the processing loop
        await self._process_loop()

    async def stop(self) -> None:
        """Stop watching directories."""
        logger.info("Stopping file watcher")
        self._running = False

        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    async def _process_loop(self) -> None:
        """Main processing loop."""
        last_scan = 0.0

        while self._running:
            now = time.time()

            # Process files detected by watchdog
            if self._handler:
                ready_files = self._handler.get_ready_files()
                for file_path in ready_files:
                    try:
                        await self.callback(file_path)
                    except Exception as e:
                        logger.error(f"Error processing {file_path}: {e}")

            # Periodic full scan
            if now - last_scan >= self.scan_interval:
                await self._full_scan()
                last_scan = now

            # Sleep briefly to avoid busy waiting
            await asyncio.sleep(1.0)

    async def _full_scan(self) -> None:
        """Perform a full scan of watched directories."""
        logger.info("Performing full directory scan")

        for directory in self.directories:
            if not directory.exists():
                continue

            for file_path in directory.rglob("*"):
                if not is_video_file(file_path):
                    continue

                try:
                    file_age = get_file_age(file_path)
                    if file_age >= self.min_file_age:
                        # Check if file was already processed
                        # (In a real implementation, you'd track processed files)
                        pass
                except OSError:
                    continue


class BatchProcessor:
    """Processes files in batches for efficiency."""

    def __init__(
        self,
        process_callback: Callable[[Path], Coroutine],
        batch_size: int = 10,
        batch_timeout: float = 30.0,
    ):
        """Initialize the batch processor.

        Args:
            process_callback: Async callback to process each file
            batch_size: Maximum batch size before processing
            batch_timeout: Maximum time to wait before processing partial batch
        """
        self.process_callback = process_callback
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout

        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._running = False

    async def add_file(self, file_path: Path) -> None:
        """Add a file to the processing queue."""
        await self._queue.put(file_path)

    async def start(self) -> None:
        """Start the batch processor."""
        self._running = True
        await self._process_loop()

    async def stop(self) -> None:
        """Stop the batch processor."""
        self._running = False

    async def _process_loop(self) -> None:
        """Main processing loop."""
        batch: list[Path] = []
        batch_start = time.time()

        while self._running:
            try:
                # Try to get a file with timeout
                file_path = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0,
                )
                batch.append(file_path)

                # Process if batch is full
                if len(batch) >= self.batch_size:
                    await self._process_batch(batch)
                    batch = []
                    batch_start = time.time()

            except asyncio.TimeoutError:
                # Check if we should process partial batch
                if batch and (time.time() - batch_start) >= self.batch_timeout:
                    await self._process_batch(batch)
                    batch = []
                    batch_start = time.time()

        # Process remaining files
        if batch:
            await self._process_batch(batch)

    async def _process_batch(self, batch: list[Path]) -> None:
        """Process a batch of files."""
        logger.info(f"Processing batch of {len(batch)} files")

        for file_path in batch:
            try:
                await self.process_callback(file_path)
            except Exception as e:
                logger.error(f"Error processing {file_path}: {e}")
