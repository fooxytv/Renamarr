"""Media file parser using guessit and ffprobe."""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from guessit import guessit

logger = logging.getLogger(__name__)


@dataclass
class QualityInfo:
    """Quality information for a media file."""

    resolution: str | None = None  # e.g., "1080p", "2160p"
    resolution_height: int | None = None  # e.g., 1080, 2160
    video_codec: str | None = None  # e.g., "H.264", "HEVC"
    audio_codec: str | None = None  # e.g., "AAC", "DTS"
    bitrate: int | None = None  # Total bitrate in bits/s
    file_size: int | None = None  # File size in bytes

    def quality_score(self) -> int:
        """Calculate a quality score for comparison.

        Higher score = better quality.
        """
        score = 0

        # Resolution scoring (most important)
        if self.resolution_height:
            if self.resolution_height >= 2160:
                score += 4000
            elif self.resolution_height >= 1080:
                score += 3000
            elif self.resolution_height >= 720:
                score += 2000
            elif self.resolution_height >= 480:
                score += 1000

        # Codec scoring
        if self.video_codec:
            codec_lower = self.video_codec.lower()
            if "hevc" in codec_lower or "h.265" in codec_lower or "x265" in codec_lower:
                score += 200
            elif "h.264" in codec_lower or "x264" in codec_lower or "avc" in codec_lower:
                score += 150
            elif "vp9" in codec_lower:
                score += 100

        # Bitrate scoring (additional refinement)
        if self.bitrate:
            # Normalize bitrate to 0-100 range (assuming max useful is ~50 Mbps)
            score += min(100, self.bitrate // 500000)

        return score


@dataclass
class MediaInfo:
    """Parsed information about a media file."""

    path: Path
    media_type: Literal["movie", "episode"] | None = None
    title: str | None = None
    year: int | None = None
    # TV show specific
    show_name: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    # Quality info
    quality: QualityInfo = field(default_factory=QualityInfo)
    # TMDB IDs (populated after lookup)
    tmdb_id: int | None = None
    tmdb_episode_id: int | None = None

    @property
    def is_movie(self) -> bool:
        """Check if this is a movie."""
        return self.media_type == "movie"

    @property
    def is_episode(self) -> bool:
        """Check if this is a TV episode."""
        return self.media_type == "episode"


def parse_with_guessit(path: Path) -> dict:
    """Parse a media filename using guessit."""
    result = guessit(path.name)
    return dict(result)


def get_ffprobe_info(path: Path) -> dict | None:
    """Extract media information using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.debug(f"ffprobe failed for {path}: {e}")
    return None


def extract_quality_from_ffprobe(ffprobe_data: dict) -> QualityInfo:
    """Extract quality information from ffprobe output."""
    quality = QualityInfo()

    if not ffprobe_data:
        return quality

    # Extract from format
    if "format" in ffprobe_data:
        fmt = ffprobe_data["format"]
        if "bit_rate" in fmt:
            try:
                quality.bitrate = int(fmt["bit_rate"])
            except (ValueError, TypeError):
                pass
        if "size" in fmt:
            try:
                quality.file_size = int(fmt["size"])
            except (ValueError, TypeError):
                pass

    # Extract from streams
    for stream in ffprobe_data.get("streams", []):
        codec_type = stream.get("codec_type")

        if codec_type == "video":
            # Get resolution
            height = stream.get("height")
            if height:
                quality.resolution_height = height
                if height >= 2160:
                    quality.resolution = "2160p"
                elif height >= 1080:
                    quality.resolution = "1080p"
                elif height >= 720:
                    quality.resolution = "720p"
                elif height >= 480:
                    quality.resolution = "480p"
                else:
                    quality.resolution = f"{height}p"

            # Get video codec
            codec_name = stream.get("codec_name", "").upper()
            if codec_name:
                quality.video_codec = codec_name

        elif codec_type == "audio":
            # Get audio codec (first audio stream)
            if not quality.audio_codec:
                codec_name = stream.get("codec_name", "").upper()
                if codec_name:
                    quality.audio_codec = codec_name

    return quality


def extract_quality_from_guessit(guessit_data: dict) -> QualityInfo:
    """Extract quality information from guessit output."""
    quality = QualityInfo()

    # Resolution
    screen_size = guessit_data.get("screen_size")
    if screen_size:
        quality.resolution = screen_size
        # Parse height from screen size
        if "2160" in screen_size or "4k" in screen_size.lower():
            quality.resolution_height = 2160
        elif "1080" in screen_size:
            quality.resolution_height = 1080
        elif "720" in screen_size:
            quality.resolution_height = 720
        elif "480" in screen_size:
            quality.resolution_height = 480

    # Video codec
    video_codec = guessit_data.get("video_codec")
    if video_codec:
        quality.video_codec = video_codec

    # Audio codec
    audio_codec = guessit_data.get("audio_codec")
    if audio_codec:
        quality.audio_codec = audio_codec

    return quality


def parse_media_file(path: Path) -> MediaInfo:
    """Parse a media file and extract all relevant information.

    Uses guessit for filename parsing and ffprobe for embedded metadata.
    """
    info = MediaInfo(path=path)

    # Parse filename with guessit
    guessit_data = parse_with_guessit(path)
    logger.debug(f"Guessit result for {path.name}: {guessit_data}")

    # Determine media type
    media_type = guessit_data.get("type")
    if media_type == "movie":
        info.media_type = "movie"
        info.title = guessit_data.get("title")
        info.year = guessit_data.get("year")
    elif media_type == "episode":
        info.media_type = "episode"
        info.show_name = guessit_data.get("title")
        # guessit returns lists for multi-episode files (e.g. S01E01E02)
        season = guessit_data.get("season")
        info.season = season[0] if isinstance(season, list) else season
        episode = guessit_data.get("episode")
        info.episode = episode[0] if isinstance(episode, list) else episode
        info.episode_title = guessit_data.get("episode_title")
        info.year = guessit_data.get("year")

    # Extract quality from guessit first
    info.quality = extract_quality_from_guessit(guessit_data)

    # Try ffprobe for more accurate quality info
    ffprobe_data = get_ffprobe_info(path)
    if ffprobe_data:
        ffprobe_quality = extract_quality_from_ffprobe(ffprobe_data)
        # Merge ffprobe quality (prefer ffprobe values when available)
        if ffprobe_quality.resolution_height:
            info.quality.resolution_height = ffprobe_quality.resolution_height
            info.quality.resolution = ffprobe_quality.resolution
        if ffprobe_quality.video_codec:
            info.quality.video_codec = ffprobe_quality.video_codec
        if ffprobe_quality.audio_codec:
            info.quality.audio_codec = ffprobe_quality.audio_codec
        if ffprobe_quality.bitrate:
            info.quality.bitrate = ffprobe_quality.bitrate
        if ffprobe_quality.file_size:
            info.quality.file_size = ffprobe_quality.file_size

    # Get file size if not from ffprobe
    if not info.quality.file_size:
        try:
            info.quality.file_size = path.stat().st_size
        except OSError:
            pass

    return info
