"""Tests for the Plex naming formatter."""

from pathlib import Path

import pytest

from src.formatter import FormattedPath, PlexFormatter
from src.omdb_client import MovieResult
from src.parser import MediaInfo, QualityInfo
from src.tvmaze_client import EpisodeResult, TVShowResult


class TestPlexFormatterMovies:
    """Tests for movie formatting."""

    def test_format_movie_basic(self):
        """Test basic movie formatting."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("movie.mkv"),
            media_type="movie",
            title="The Matrix",
            year=1999,
        )

        result = formatter.format_movie(media_info)

        assert result.relative_path == Path("The Matrix (1999)")
        assert result.filename == "The Matrix (1999).mkv"

    def test_format_movie_with_omdb(self):
        """Test movie formatting with OMDb data."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("matrix.mkv"),
            media_type="movie",
            title="matrix",  # Lowercase from filename
            year=1999,
        )
        omdb_result = MovieResult(
            imdb_id="tt0133093",
            title="The Matrix",
            year=1999,
            plot="",
            poster=None,
        )

        result = formatter.format_movie(media_info, omdb_result)

        # Should use OMDb title
        assert result.relative_path == Path("The Matrix (1999)")
        assert result.filename == "The Matrix (1999).mkv"

    def test_format_movie_missing_year(self):
        """Test movie formatting without year."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("movie.mkv"),
            media_type="movie",
            title="Unknown Movie",
            year=None,
        )

        result = formatter.format_movie(media_info)

        assert result.relative_path == Path("Unknown Movie (Unknown)")
        assert result.filename == "Unknown Movie (Unknown).mkv"

    def test_format_movie_special_characters(self):
        """Test movie formatting with special characters."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("movie.mkv"),
            media_type="movie",
            title="What If...?",
            year=2021,
        )

        result = formatter.format_movie(media_info)

        # Special characters should be sanitized
        assert "?" not in result.filename

    def test_format_movie_custom_pattern(self):
        """Test movie formatting with custom pattern."""
        formatter = PlexFormatter(movie_pattern="{title} [{year}]/{title}{ext}")
        media_info = MediaInfo(
            path=Path("movie.mkv"),
            media_type="movie",
            title="Inception",
            year=2010,
        )

        result = formatter.format_movie(media_info)

        assert result.relative_path == Path("Inception [2010]")
        assert result.filename == "Inception.mkv"


class TestPlexFormatterTV:
    """Tests for TV episode formatting."""

    def test_format_episode_basic(self):
        """Test basic episode formatting."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="Breaking Bad",
            season=1,
            episode=1,
            episode_title="Pilot",
        )

        result = formatter.format_episode(media_info)

        assert result.relative_path == Path("Breaking Bad (Unknown)/Season 01")
        assert result.filename == "Breaking Bad (Unknown) - S01E01 - Pilot.mkv"

    def test_format_episode_with_tvmaze(self):
        """Test episode formatting with TVMaze data."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="breaking bad",
            season=1,
            episode=1,
        )
        tv_result = TVShowResult(
            tvmaze_id=169,
            name="Breaking Bad",
            premiered="2008-01-20",
            summary="",
        )
        episode_result = EpisodeResult(
            episode_id=1,
            show_id=169,
            season_number=1,
            episode_number=1,
            name="Pilot",
            airdate="2008-01-20",
            summary="",
        )

        result = formatter.format_episode(media_info, tv_result, episode_result)

        assert result.relative_path == Path("Breaking Bad (2008)/Season 01")
        assert result.filename == "Breaking Bad (2008) - S01E01 - Pilot.mkv"

    def test_format_episode_no_title(self):
        """Test episode formatting without episode title."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="Some Show",
            season=2,
            episode=5,
            episode_title=None,
        )

        result = formatter.format_episode(media_info)

        # Should use "Episode" as default
        assert result.filename == "Some Show (Unknown) - S02E05 - Episode.mkv"

    def test_format_episode_zero_padding(self):
        """Test that season and episode numbers are zero-padded."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="Test Show",
            season=1,
            episode=5,
            episode_title="Test",
        )

        result = formatter.format_episode(media_info)

        assert "S01E05" in result.filename

    def test_format_episode_custom_pattern(self):
        """Test episode formatting with custom pattern."""
        formatter = PlexFormatter(
            tv_pattern="{show}/S{season:02d}/{show}.S{season:02d}E{episode:02d}{ext}"
        )
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="Test Show",
            season=3,
            episode=12,
        )

        result = formatter.format_episode(media_info)

        assert result.relative_path == Path("Test Show/S03")
        assert result.filename == "Test Show.S03E12.mkv"


class TestPlexFormatterGeneric:
    """Tests for generic format method."""

    def test_format_detects_movie(self):
        """Test that format() correctly handles movies."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("movie.mkv"),
            media_type="movie",
            title="Test Movie",
            year=2020,
        )

        result = formatter.format(media_info)

        assert "Test Movie (2020)" in str(result.relative_path)

    def test_format_detects_episode(self):
        """Test that format() correctly handles episodes."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("episode.mkv"),
            media_type="episode",
            show_name="Test Show",
            season=1,
            episode=1,
            episode_title="Test",
        )

        result = formatter.format(media_info)

        assert "Test Show (Unknown)" in str(result.relative_path)
        assert "Season 01" in str(result.relative_path)

    def test_format_unknown_type_raises(self):
        """Test that format() raises for unknown media type."""
        formatter = PlexFormatter()
        media_info = MediaInfo(
            path=Path("unknown.mkv"),
            media_type=None,
        )

        with pytest.raises(ValueError, match="Unknown media type"):
            formatter.format(media_info)
