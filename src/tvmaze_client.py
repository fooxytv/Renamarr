"""TVMaze API client for TV show lookups.

TVMaze API is completely free and requires NO API key!
Documentation: https://www.tvmaze.com/api
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TVMAZE_BASE_URL = "https://api.tvmaze.com"


@dataclass
class TVShowResult:
    """TV show search result from TVMaze."""

    tvmaze_id: int
    name: str
    premiered: str | None  # YYYY-MM-DD format
    summary: str
    poster: str | None = None

    @property
    def year(self) -> int | None:
        """Extract year from premiered date."""
        if self.premiered:
            try:
                return int(self.premiered[:4])
            except (ValueError, IndexError):
                pass
        return None


@dataclass
class EpisodeResult:
    """Episode details from TVMaze."""

    episode_id: int
    show_id: int
    season_number: int
    episode_number: int
    name: str
    airdate: str | None
    summary: str


class TVMazeClient:
    """Async TVMaze API client.

    No API key required!
    """

    def __init__(self, requests_per_second: float = 2.0):
        """Initialize the TVMaze client.

        Args:
            requests_per_second: Rate limit (TVMaze allows ~20/10s but bursts trigger 429)
        """
        self.rate_limit_delay = 1.0 / requests_per_second
        self._last_request_time = 0.0
        self._rate_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, dict] = {}

    async def __aenter__(self) -> "TVMazeClient":
        """Enter async context."""
        self._client = httpx.AsyncClient(
            base_url=TVMAZE_BASE_URL,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit async context."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _rate_limit(self) -> None:
        """Enforce rate limiting with a lock to serialise concurrent requests."""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < self.rate_limit_delay:
                await asyncio.sleep(self.rate_limit_delay - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, endpoint: str, params: dict | None = None) -> dict | list | None:
        """Make a GET request to TVMaze API with retry on 429."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with.")

        # Check cache
        cache_key = f"{endpoint}:{params}"
        if cache_key in self._cache:
            logger.debug(f"Cache hit: {endpoint}")
            return self._cache[cache_key]

        max_retries = 3
        for attempt in range(max_retries + 1):
            await self._rate_limit()

            try:
                response = await self._client.get(endpoint, params=params)
                if response.status_code == 404:
                    return None
                if response.status_code == 429:
                    if attempt < max_retries:
                        wait = 2 ** attempt + 1
                        logger.warning(f"TVMaze 429 rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"TVMaze 429 rate limited after {max_retries} retries: {endpoint}")
                    return None
                response.raise_for_status()
                data = response.json()

                # Cache successful response
                self._cache[cache_key] = data
                return data
            except httpx.HTTPStatusError as e:
                logger.error(f"TVMaze API error: {e}")
                return None

        return None

    async def search_shows(self, query: str) -> list[TVShowResult]:
        """Search for TV shows by name.

        Args:
            query: Show name to search for

        Returns:
            List of matching shows
        """
        data = await self._get("/search/shows", params={"q": query})
        if not data:
            return []

        results = []
        for item in data:
            show = item.get("show", {})
            image = show.get("image") or {}
            results.append(
                TVShowResult(
                    tvmaze_id=show.get("id", 0),
                    name=show.get("name", ""),
                    premiered=show.get("premiered"),
                    summary=show.get("summary") or "",
                    poster=image.get("medium") or image.get("original"),
                )
            )

        return results

    async def get_show(self, show_id: int) -> TVShowResult | None:
        """Get show details by TVMaze ID.

        Args:
            show_id: TVMaze show ID

        Returns:
            Show details or None
        """
        data = await self._get(f"/shows/{show_id}")
        if not data:
            return None

        image = data.get("image") or {}
        return TVShowResult(
            tvmaze_id=data.get("id", 0),
            name=data.get("name", ""),
            premiered=data.get("premiered"),
            summary=data.get("summary") or "",
            poster=image.get("medium") or image.get("original"),
        )

    async def get_episode(
        self, show_id: int, season: int, episode: int
    ) -> EpisodeResult | None:
        """Get episode details.

        Args:
            show_id: TVMaze show ID
            season: Season number
            episode: Episode number

        Returns:
            Episode details or None
        """
        data = await self._get(
            f"/shows/{show_id}/episodebynumber",
            params={"season": season, "number": episode},
        )
        if not data:
            return None

        return EpisodeResult(
            episode_id=data.get("id", 0),
            show_id=show_id,
            season_number=data.get("season", season),
            episode_number=data.get("number", episode),
            name=data.get("name", ""),
            airdate=data.get("airdate"),
            summary=data.get("summary") or "",
        )

    async def get_episodes(self, show_id: int) -> list[EpisodeResult]:
        """Get all episodes for a show.

        Args:
            show_id: TVMaze show ID

        Returns:
            List of all episodes
        """
        data = await self._get(f"/shows/{show_id}/episodes")
        if not data:
            return []

        return [
            EpisodeResult(
                episode_id=ep.get("id", 0),
                show_id=show_id,
                season_number=ep.get("season", 0),
                episode_number=ep.get("number", 0),
                name=ep.get("name", ""),
                airdate=ep.get("airdate"),
                summary=ep.get("summary") or "",
            )
            for ep in data
        ]

    async def find_best_match(
        self, name: str, year: int | None = None
    ) -> TVShowResult | None:
        """Find the best matching TV show.

        Uses title similarity scoring to pick the best result rather than
        blindly trusting API ordering. Prefers exact year matches.

        Args:
            name: Show name to search for
            year: Optional premiere year to narrow results

        Returns:
            Best matching show or None
        """
        from difflib import SequenceMatcher

        results = await self.search_shows(name)
        if not results:
            return None

        query = name.lower().strip()

        def _score(result: TVShowResult) -> float:
            sim = SequenceMatcher(None, query, result.name.lower().strip()).ratio()
            score = sim * 60
            if year and result.year:
                if result.year == year:
                    score += 30
                elif abs(result.year - year) == 1:
                    score += 15
            if query == result.name.lower().strip():
                score += 10
            return score

        return max(results, key=_score)

    def clear_cache(self) -> None:
        """Clear the response cache."""
        self._cache.clear()
