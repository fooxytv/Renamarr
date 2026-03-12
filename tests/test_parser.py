"""Tests for the media file parser."""

from pathlib import Path

import pytest

from src.parser import (
    MediaInfo,
    QualityInfo,
    extract_quality_from_guessit,
    parse_with_guessit,
)


class TestQualityInfo:
    """Tests for QualityInfo class."""

    def test_quality_score_4k(self):
        """Test quality score for 4K content."""
        quality = QualityInfo(
            resolution="2160p",
            resolution_height=2160,
            video_codec="HEVC",
        )
        score = quality.quality_score()
        assert score >= 4000  # 4K base score

    def test_quality_score_1080p(self):
        """Test quality score for 1080p content."""
        quality = QualityInfo(
            resolution="1080p",
            resolution_height=1080,
            video_codec="H.264",
        )
        score = quality.quality_score()
        assert 3000 <= score < 4000  # 1080p range

    def test_quality_score_720p(self):
        """Test quality score for 720p content."""
        quality = QualityInfo(
            resolution="720p",
            resolution_height=720,
            video_codec="H.264",
        )
        score = quality.quality_score()
        assert 2000 <= score < 3000  # 720p range

    def test_quality_score_comparison(self):
        """Test that higher quality scores higher."""
        low_quality = QualityInfo(resolution_height=720, video_codec="H.264")
        high_quality = QualityInfo(resolution_height=1080, video_codec="HEVC")

        assert high_quality.quality_score() > low_quality.quality_score()

    def test_codec_scoring(self):
        """Test codec contribution to quality score."""
        hevc = QualityInfo(resolution_height=1080, video_codec="HEVC")
        h264 = QualityInfo(resolution_height=1080, video_codec="H.264")

        assert hevc.quality_score() > h264.quality_score()


class TestParseWithGuessit:
    """Tests for guessit parsing."""

    def test_parse_movie_with_year(self):
        """Test parsing movie filename with year."""
        path = Path("The Matrix (1999).mkv")
        result = parse_with_guessit(path)

        assert result.get("type") == "movie"
        assert result.get("title") == "The Matrix"
        assert result.get("year") == 1999

    def test_parse_movie_with_quality(self):
        """Test parsing movie filename with quality info."""
        path = Path("Inception.2010.1080p.BluRay.x264.mkv")
        result = parse_with_guessit(path)

        assert result.get("type") == "movie"
        assert result.get("title") == "Inception"
        assert result.get("year") == 2010
        assert result.get("screen_size") == "1080p"

    def test_parse_tv_episode(self):
        """Test parsing TV episode filename."""
        path = Path("Breaking Bad S01E01 Pilot.mkv")
        result = parse_with_guessit(path)

        assert result.get("type") == "episode"
        assert result.get("title") == "Breaking Bad"
        assert result.get("season") == 1
        assert result.get("episode") == 1

    def test_parse_tv_episode_with_title(self):
        """Test parsing TV episode with episode title."""
        path = Path("Game.of.Thrones.S08E06.The.Iron.Throne.720p.mkv")
        result = parse_with_guessit(path)

        assert result.get("type") == "episode"
        assert result.get("title") == "Game of Thrones"
        assert result.get("season") == 8
        assert result.get("episode") == 6
        assert result.get("screen_size") == "720p"


class TestExtractQualityFromGuessit:
    """Tests for quality extraction from guessit results."""

    def test_extract_resolution(self):
        """Test extracting resolution from guessit data."""
        guessit_data = {"screen_size": "1080p"}
        quality = extract_quality_from_guessit(guessit_data)

        assert quality.resolution == "1080p"
        assert quality.resolution_height == 1080

    def test_extract_4k_resolution(self):
        """Test extracting 4K resolution."""
        guessit_data = {"screen_size": "2160p"}
        quality = extract_quality_from_guessit(guessit_data)

        assert quality.resolution == "2160p"
        assert quality.resolution_height == 2160

    def test_extract_video_codec(self):
        """Test extracting video codec."""
        guessit_data = {"video_codec": "H.265"}
        quality = extract_quality_from_guessit(guessit_data)

        assert quality.video_codec == "H.265"

    def test_extract_audio_codec(self):
        """Test extracting audio codec."""
        guessit_data = {"audio_codec": "DTS-HD"}
        quality = extract_quality_from_guessit(guessit_data)

        assert quality.audio_codec == "DTS-HD"


class TestMediaInfo:
    """Tests for MediaInfo class."""

    def test_is_movie(self):
        """Test is_movie property."""
        info = MediaInfo(path=Path("movie.mkv"), media_type="movie")
        assert info.is_movie is True
        assert info.is_episode is False

    def test_is_episode(self):
        """Test is_episode property."""
        info = MediaInfo(path=Path("episode.mkv"), media_type="episode")
        assert info.is_movie is False
        assert info.is_episode is True

    def test_unknown_type(self):
        """Test unknown media type."""
        info = MediaInfo(path=Path("unknown.mkv"), media_type=None)
        assert info.is_movie is False
        assert info.is_episode is False
