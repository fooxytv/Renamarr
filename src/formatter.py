"""Plex naming convention formatter."""

import re
from dataclasses import dataclass
from pathlib import Path

from .omdb_client import MovieResult
from .parser import MediaInfo
from .tvmaze_client import EpisodeResult, TVShowResult
from .utils import sanitize_filename


@dataclass
class FormattedPath:
    """Formatted path for a media file."""

    relative_path: Path  # Relative path from output directory
    filename: str  # Just the filename


class PlexFormatter:
    """Formatter for Plex naming conventions."""

    # Default naming patterns
    DEFAULT_MOVIE_PATTERN = "{title} ({year})/{title} ({year}){ext}"
    DEFAULT_TV_PATTERN = "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}{ext}"

    def __init__(
        self,
        movie_pattern: str | None = None,
        tv_pattern: str | None = None,
    ):
        """Initialize the formatter.

        Args:
            movie_pattern: Custom movie naming pattern
            tv_pattern: Custom TV show naming pattern
        """
        self.movie_pattern = movie_pattern or self.DEFAULT_MOVIE_PATTERN
        self.tv_pattern = tv_pattern or self.DEFAULT_TV_PATTERN

    def format_movie(
        self,
        media_info: MediaInfo,
        omdb_result: MovieResult | None = None,
    ) -> FormattedPath:
        """Format a movie file path according to Plex conventions.

        Pattern variables:
        - {title}: Movie title
        - {year}: Release year
        - {ext}: File extension (including dot)

        Args:
            media_info: Parsed media information
            omdb_result: Optional OMDb lookup result

        Returns:
            Formatted path
        """
        # Use OMDb data if available, fall back to parsed info
        if omdb_result:
            title = omdb_result.title
            year = omdb_result.year
        else:
            title = (media_info.title or "Unknown Movie").title()
            year = media_info.year

        # Handle missing year
        if not year:
            year = "Unknown"

        # Sanitize title for filesystem
        title = sanitize_filename(title)

        # Get file extension
        ext = media_info.path.suffix

        # Format the pattern
        try:
            formatted = self.movie_pattern.format(
                title=title,
                year=year,
                ext=ext,
            )
        except KeyError as e:
            raise ValueError(f"Invalid pattern variable: {e}")

        # Split into directory and filename
        path = Path(formatted)
        return FormattedPath(
            relative_path=path.parent,
            filename=path.name,
        )

    def format_episode(
        self,
        media_info: MediaInfo,
        tv_result: TVShowResult | None = None,
        episode_result: EpisodeResult | None = None,
    ) -> FormattedPath:
        """Format a TV episode file path according to Plex conventions.

        Pattern variables:
        - {show}: TV show name
        - {season}: Season number (supports format spec like :02d)
        - {episode}: Episode number (supports format spec like :02d)
        - {episode_title}: Episode title
        - {ext}: File extension (including dot)

        Args:
            media_info: Parsed media information
            tv_result: Optional TVMaze show result
            episode_result: Optional TVMaze episode result

        Returns:
            Formatted path
        """
        # Use TVMaze data if available, fall back to parsed info
        if tv_result:
            show = tv_result.name
        else:
            show = (media_info.show_name or "Unknown Show").title()

        if episode_result:
            season = episode_result.season_number
            episode = episode_result.episode_number
            episode_title = episode_result.name
        else:
            season = media_info.season or 1
            episode = media_info.episode or 1
            episode_title = media_info.episode_title or ""

        # Sanitize names for filesystem
        show = sanitize_filename(show)
        if episode_title:
            episode_title = sanitize_filename(episode_title)
        else:
            episode_title = "Episode"

        # Get file extension
        ext = media_info.path.suffix

        # Format the pattern using custom formatter for format specs
        formatted = self._format_tv_pattern(
            pattern=self.tv_pattern,
            show=show,
            season=season,
            episode=episode,
            episode_title=episode_title,
            ext=ext,
        )

        # Split into directory and filename
        path = Path(formatted)
        return FormattedPath(
            relative_path=path.parent,
            filename=path.name,
        )

    def _format_tv_pattern(
        self,
        pattern: str,
        show: str,
        season: int,
        episode: int,
        episode_title: str,
        ext: str,
    ) -> str:
        """Format TV pattern with support for format specs on numeric values."""
        # Replace simple placeholders first
        result = pattern.replace("{show}", show)
        result = result.replace("{episode_title}", episode_title)
        result = result.replace("{ext}", ext)

        # Handle season with format spec
        result = re.sub(
            r"\{season(?::([^}]+))?\}",
            lambda m: format(season, m.group(1) or ""),
            result,
        )

        # Handle episode with format spec
        result = re.sub(
            r"\{episode(?::([^}]+))?\}",
            lambda m: format(episode, m.group(1) or ""),
            result,
        )

        return result

    def format(
        self,
        media_info: MediaInfo,
        omdb_movie: MovieResult | None = None,
        tvmaze_show: TVShowResult | None = None,
        tvmaze_episode: EpisodeResult | None = None,
    ) -> FormattedPath:
        """Format a media file path based on its type.

        Args:
            media_info: Parsed media information
            omdb_movie: Optional OMDb movie result (for movies)
            tvmaze_show: Optional TVMaze show result (for episodes)
            tvmaze_episode: Optional TVMaze episode result (for episodes)

        Returns:
            Formatted path
        """
        if media_info.is_movie:
            return self.format_movie(media_info, omdb_movie)
        elif media_info.is_episode:
            return self.format_episode(media_info, tvmaze_show, tvmaze_episode)
        else:
            raise ValueError(f"Unknown media type: {media_info.media_type}")


def create_formatter(
    movie_pattern: str | None = None,
    tv_pattern: str | None = None,
) -> PlexFormatter:
    """Create a PlexFormatter with optional custom patterns.

    Args:
        movie_pattern: Custom movie naming pattern
        tv_pattern: Custom TV show naming pattern

    Returns:
        Configured PlexFormatter
    """
    return PlexFormatter(movie_pattern=movie_pattern, tv_pattern=tv_pattern)
