"""Renamarr - Media File Renamer Application entry point."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .config import Config, load_config
from .duplicates import DuplicateHandler
from .formatter import PlexFormatter
from .omdb_client import OMDbClient
from .renamer import RenamerService
from .tvmaze_client import TVMazeClient
from .utils import setup_logging
from .watcher import FileWatcher

logger = logging.getLogger(__name__)


class Renamarr:
    """Main application class."""

    def __init__(self, config: Config):
        """Initialize the application.

        Args:
            config: Application configuration
        """
        self.config = config
        self._running = False
        self._watcher: FileWatcher | None = None
        self._omdb_client: OMDbClient | None = None
        self._tvmaze_client: TVMazeClient | None = None

    async def start(self) -> None:
        """Start the application."""
        logger.info("Starting Renamarr")
        logger.info(f"Dry run mode: {self.config.options.dry_run}")

        self._running = True

        # Initialize API clients
        # OMDb for movies (requires API key - just email to get one)
        self._omdb_client = OMDbClient(self.config.omdb.api_key)
        # TVMaze for TV shows (no API key required!)
        self._tvmaze_client = TVMazeClient()

        async with self._omdb_client, self._tvmaze_client:
            formatter = PlexFormatter(
                movie_pattern=self.config.naming.movies,
                tv_pattern=self.config.naming.tv,
            )

            duplicate_handler = DuplicateHandler(
                action=self.config.duplicates.action,
                duplicates_folder=self.config.duplicates.duplicates_folder,
                dry_run=self.config.options.dry_run,
            )

            renamer = RenamerService(
                config=self.config,
                omdb_client=self._omdb_client,
                tvmaze_client=self._tvmaze_client,
                formatter=formatter,
                duplicate_handler=duplicate_handler,
            )

            # Define the callback for processing files
            async def process_file(file_path: Path) -> None:
                await renamer.process_file(file_path)

            # Get directories to watch
            watch_dirs = [
                self.config.directories.movies.watch,
                self.config.directories.tv.watch,
            ]

            # Initial scan
            logger.info("Performing initial scan")
            results = await renamer.scan_and_process()

            movies_processed = len(results["movies"])
            tv_processed = len(results["tv"])
            logger.info(f"Initial scan complete: {movies_processed} movies, {tv_processed} TV episodes")

            # Start file watcher
            self._watcher = FileWatcher(
                directories=watch_dirs,
                callback=process_file,
                min_file_age=self.config.options.min_file_age,
                scan_interval=self.config.options.scan_interval,
            )

            await self._watcher.start()

    async def stop(self) -> None:
        """Stop the application."""
        logger.info("Stopping Renamarr")
        self._running = False

        if self._watcher:
            await self._watcher.stop()

    async def run_once(self) -> None:
        """Run a single scan without watching."""
        logger.info("Running single scan")

        # Initialize API clients
        self._omdb_client = OMDbClient(self.config.omdb.api_key)
        self._tvmaze_client = TVMazeClient()

        async with self._omdb_client, self._tvmaze_client:
            formatter = PlexFormatter(
                movie_pattern=self.config.naming.movies,
                tv_pattern=self.config.naming.tv,
            )

            duplicate_handler = DuplicateHandler(
                action=self.config.duplicates.action,
                duplicates_folder=self.config.duplicates.duplicates_folder,
                dry_run=self.config.options.dry_run,
            )

            renamer = RenamerService(
                config=self.config,
                omdb_client=self._omdb_client,
                tvmaze_client=self._tvmaze_client,
                formatter=formatter,
                duplicate_handler=duplicate_handler,
            )

            results = await renamer.scan_and_process()

            # Print summary
            movies_success = sum(1 for r in results["movies"] if r.success)
            movies_failed = sum(1 for r in results["movies"] if not r.success)
            tv_success = sum(1 for r in results["tv"] if r.success)
            tv_failed = sum(1 for r in results["tv"] if not r.success)

            mode = "[DRY RUN] " if self.config.options.dry_run else ""
            logger.info(f"{mode}Scan complete:")
            logger.info(f"  Movies: {movies_success} renamed, {movies_failed} failed")
            logger.info(f"  TV: {tv_success} renamed, {tv_failed} failed")
            if self._omdb_client._rate_limited:
                logger.warning("OMDb API daily limit was reached. Some movies were skipped. Try again tomorrow or upgrade your API key.")
            if self.config.options.dry_run:
                logger.info("No files were changed. Set DRY_RUN=false to apply.")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Renamarr - Media File Renamer for Plex"
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to configuration file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit (don't watch)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start web UI mode",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--delete-code",
        type=str,
        metavar="PASSPHRASE",
        help="Generate a time-limited delete code from your passphrase",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    """Async main function."""
    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return 1

    # Override dry run from command line or DRY_RUN env var
    dry_run_env = os.environ.get("DRY_RUN", "").lower()
    if args.dry_run or dry_run_env in ("true", "1", "yes"):
        config.options.dry_run = True
    elif dry_run_env in ("false", "0", "no"):
        config.options.dry_run = False

    # Create application
    app = Renamarr(config)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def handle_signal() -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(app.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Run the application
    try:
        if args.once:
            await app.run_once()
        else:
            await app.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        return 1

    return 0


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Generate delete code and exit
    if args.delete_code:
        from .auth import generate_code
        code = generate_code(args.delete_code)
        print(f"Delete code: {code}")
        print("Valid for 2 minutes.")
        return 0

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(log_level)

    # Web mode runs uvicorn directly (it manages its own event loop)
    if args.web:
        import uvicorn
        from .web.app import create_app

        try:
            config = load_config(args.config)
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to load configuration: {e}")
            return 1

        dry_run_env = os.environ.get("DRY_RUN", "").lower()
        if args.dry_run or dry_run_env in ("true", "1", "yes"):
            config.options.dry_run = True
        elif dry_run_env in ("false", "0", "no"):
            config.options.dry_run = False

        web_app = create_app(config, config.web.data_dir)
        logging.getLogger(__name__).info(f"Starting web UI on {config.web.host}:{config.web.port}")
        uvicorn.run(web_app, host=config.web.host, port=config.web.port, log_level="info")
        return 0

    # Run async main
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
