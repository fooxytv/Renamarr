"""Tests for duplicate detection and handling."""

from pathlib import Path

import pytest

from src.duplicates import (
    DuplicateGroup,
    DuplicateHandler,
    compare_quality,
)
from src.parser import MediaInfo, QualityInfo


class TestDuplicateGroup:
    """Tests for DuplicateGroup class."""

    def test_best_quality(self):
        """Test finding best quality file in group."""
        files = [
            MediaInfo(
                path=Path("movie_720p.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=720),
            ),
            MediaInfo(
                path=Path("movie_1080p.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=1080),
            ),
            MediaInfo(
                path=Path("movie_480p.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=480),
            ),
        ]

        group = DuplicateGroup(identifier="test:movie", files=files)
        best = group.best_quality

        assert best.path.name == "movie_1080p.mkv"

    def test_duplicates(self):
        """Test getting duplicate files (excluding best)."""
        files = [
            MediaInfo(
                path=Path("movie_720p.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=720),
            ),
            MediaInfo(
                path=Path("movie_1080p.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=1080),
            ),
        ]

        group = DuplicateGroup(identifier="test:movie", files=files)
        duplicates = group.duplicates

        assert len(duplicates) == 1
        assert duplicates[0].path.name == "movie_720p.mkv"

    def test_best_quality_with_codec(self):
        """Test best quality considers codec."""
        files = [
            MediaInfo(
                path=Path("movie_h264.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=1080, video_codec="H.264"),
            ),
            MediaInfo(
                path=Path("movie_hevc.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=1080, video_codec="HEVC"),
            ),
        ]

        group = DuplicateGroup(identifier="test:movie", files=files)
        best = group.best_quality

        assert best.path.name == "movie_hevc.mkv"


class TestDuplicateHandler:
    """Tests for DuplicateHandler class."""

    def test_find_duplicates_by_tmdb_id(self):
        """Test finding duplicates by TMDB ID."""
        handler = DuplicateHandler(action="report_only")

        files = [
            MediaInfo(
                path=Path("movie1.mkv"),
                media_type="movie",
                title="Test Movie",
                tmdb_id=12345,
                quality=QualityInfo(resolution_height=1080),
            ),
            MediaInfo(
                path=Path("movie2.mkv"),
                media_type="movie",
                title="Test Movie",
                tmdb_id=12345,
                quality=QualityInfo(resolution_height=720),
            ),
            MediaInfo(
                path=Path("other.mkv"),
                media_type="movie",
                title="Other Movie",
                tmdb_id=99999,
                quality=QualityInfo(resolution_height=1080),
            ),
        ]

        groups = handler.find_duplicates(files)

        assert len(groups) == 1
        assert len(groups[0].files) == 2

    def test_find_duplicates_by_title_year(self):
        """Test finding duplicates by title and year."""
        handler = DuplicateHandler(action="report_only")

        files = [
            MediaInfo(
                path=Path("movie1.mkv"),
                media_type="movie",
                title="Test Movie",
                year=2020,
                quality=QualityInfo(resolution_height=1080),
            ),
            MediaInfo(
                path=Path("movie2.mkv"),
                media_type="movie",
                title="Test Movie",
                year=2020,
                quality=QualityInfo(resolution_height=720),
            ),
        ]

        groups = handler.find_duplicates(files)

        assert len(groups) == 1

    def test_find_duplicates_tv_episodes(self):
        """Test finding duplicate TV episodes."""
        handler = DuplicateHandler(action="report_only")

        files = [
            MediaInfo(
                path=Path("episode1.mkv"),
                media_type="episode",
                show_name="Test Show",
                season=1,
                episode=1,
                tmdb_id=1000,
                quality=QualityInfo(resolution_height=1080),
            ),
            MediaInfo(
                path=Path("episode2.mkv"),
                media_type="episode",
                show_name="Test Show",
                season=1,
                episode=1,
                tmdb_id=1000,
                quality=QualityInfo(resolution_height=720),
            ),
        ]

        groups = handler.find_duplicates(files)

        assert len(groups) == 1

    def test_no_duplicates(self):
        """Test no duplicates found."""
        handler = DuplicateHandler(action="report_only")

        files = [
            MediaInfo(
                path=Path("movie1.mkv"),
                media_type="movie",
                title="Movie 1",
                tmdb_id=111,
            ),
            MediaInfo(
                path=Path("movie2.mkv"),
                media_type="movie",
                title="Movie 2",
                tmdb_id=222,
            ),
        ]

        groups = handler.find_duplicates(files)

        assert len(groups) == 0

    def test_resolve_duplicates_report_only(self):
        """Test resolving duplicates with report_only action."""
        handler = DuplicateHandler(action="report_only")

        files = [
            MediaInfo(
                path=Path("movie1.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=1080),
            ),
            MediaInfo(
                path=Path("movie2.mkv"),
                media_type="movie",
                title="Test",
                quality=QualityInfo(resolution_height=720),
            ),
        ]
        group = DuplicateGroup(identifier="test:movie", files=files)

        result = handler.resolve_duplicates(group)

        assert result is not None
        assert result.action_taken == "report_only"
        assert len(result.removed) == 0

    def test_move_action_requires_folder(self):
        """Test that 'move' action requires duplicates_folder."""
        with pytest.raises(ValueError, match="duplicates_folder required"):
            DuplicateHandler(action="move", duplicates_folder=None)


class TestCompareQuality:
    """Tests for compare_quality function."""

    def test_higher_resolution_wins(self):
        """Test that higher resolution is better."""
        file1 = MediaInfo(
            path=Path("1080p.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=1080),
        )
        file2 = MediaInfo(
            path=Path("720p.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=720),
        )

        assert compare_quality(file1, file2) > 0

    def test_same_resolution_codec_wins(self):
        """Test that better codec wins when resolution is same."""
        file1 = MediaInfo(
            path=Path("hevc.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=1080, video_codec="HEVC"),
        )
        file2 = MediaInfo(
            path=Path("h264.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=1080, video_codec="H.264"),
        )

        assert compare_quality(file1, file2) > 0

    def test_equal_quality(self):
        """Test equal quality comparison."""
        file1 = MediaInfo(
            path=Path("file1.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=1080, video_codec="H.264"),
        )
        file2 = MediaInfo(
            path=Path("file2.mkv"),
            media_type="movie",
            quality=QualityInfo(resolution_height=1080, video_codec="H.264"),
        )

        assert compare_quality(file1, file2) == 0
