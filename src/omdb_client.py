"""OMDb API client for movie lookups.

OMDb (Open Movie Database) requires only an email to get an API key.
Get your free key at: https://www.omdbapi.com/apikey.aspx
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OMDB_BASE_URL = "https://www.omdbapi.com"


@dataclass
class MovieResult:
    """Movie search result from OMDb."""

    imdb_id: str
    title: str
    year: int | None
    plot: str
    poster: str | None


class OMDbClient:
    """Async OMDb API client."""

    def __init__(self, api_key: str, requests_per_second: float = 10.0):
        """Initialize the OMDb client.

        Args:
            api_key: OMDb API key (get free at omdbapi.com)
            requests_per_second: Rate limit
        """
        self.api_key = api_key
        self.rate_limit_delay = 1.0 / requests_per_second
        self._last_request_time = 0.0
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, dict] = {}
        self._rate_limited = False

    def _fix_poster_url(self, url: str | None) -> str | None:
        """Ensure Patron poster URLs include the API key."""
        if not url:
            return None
        if "img.omdbapi.com" in url and "apikey=" not in url:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}apikey={self.api_key}"
        return url

    async def __aenter__(self) -> "OMDbClient":
        """Enter async context."""
        self._client = httpx.AsyncClient(
            base_url=OMDB_BASE_URL,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit async context."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        if elapsed < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, params: dict) -> dict | None:
        """Make a GET request to OMDb API."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with.")

        # Add API key to params
        params["apikey"] = self.api_key

        # Check cache
        cache_key = str(sorted(params.items()))
        if cache_key in self._cache:
            logger.debug(f"Cache hit: {params}")
            return self._cache[cache_key]

        await self._rate_limit()

        response = await self._client.get("/", params=params)

        if response.status_code == 401:
            if not self._rate_limited:
                self._rate_limited = True
                logger.warning("OMDb API daily limit reached. Remaining movies will be skipped.")
            return None

        response.raise_for_status()
        data = response.json()

        # Check for API error
        if data.get("Response") == "False":
            logger.debug(f"OMDb error: {data.get('Error')}")
            return None

        # Cache successful response
        self._cache[cache_key] = data
        return data

    async def search_movie(self, title: str, year: int | None = None) -> list[MovieResult]:
        """Search for movies by title.

        Args:
            title: Movie title to search for
            year: Optional release year

        Returns:
            List of matching movies
        """
        params = {"s": title, "type": "movie"}
        if year:
            params["y"] = str(year)

        data = await self._get(params)
        if not data or "Search" not in data:
            return []

        results = []
        for item in data["Search"]:
            year_str = item.get("Year", "")
            # Handle year ranges like "2019-2022"
            try:
                movie_year = int(year_str[:4]) if year_str else None
            except ValueError:
                movie_year = None

            results.append(
                MovieResult(
                    imdb_id=item.get("imdbID", ""),
                    title=item.get("Title", ""),
                    year=movie_year,
                    plot="",
                    poster=self._fix_poster_url(item.get("Poster") if item.get("Poster") != "N/A" else None),
                )
            )

        return results

    async def get_movie(self, imdb_id: str) -> MovieResult | None:
        """Get movie details by IMDb ID.

        Args:
            imdb_id: IMDb ID (e.g., "tt0133093")

        Returns:
            Movie details or None
        """
        params = {"i": imdb_id, "plot": "short"}
        data = await self._get(params)

        if not data:
            return None

        year_str = data.get("Year", "")
        try:
            movie_year = int(year_str[:4]) if year_str else None
        except ValueError:
            movie_year = None

        return MovieResult(
            imdb_id=data.get("imdbID", ""),
            title=data.get("Title", ""),
            year=movie_year,
            plot=data.get("Plot", ""),
            poster=self._fix_poster_url(data.get("Poster") if data.get("Poster") != "N/A" else None),
        )

    async def get_movie_by_title(
        self, title: str, year: int | None = None
    ) -> MovieResult | None:
        """Get movie details by title (exact match).

        Args:
            title: Movie title
            year: Optional release year

        Returns:
            Movie details or None
        """
        params = {"t": title, "type": "movie", "plot": "short"}
        if year:
            params["y"] = str(year)

        data = await self._get(params)

        if not data:
            return None

        year_str = data.get("Year", "")
        try:
            movie_year = int(year_str[:4]) if year_str else None
        except ValueError:
            movie_year = None

        return MovieResult(
            imdb_id=data.get("imdbID", ""),
            title=data.get("Title", ""),
            year=movie_year,
            plot=data.get("Plot", ""),
            poster=self._fix_poster_url(data.get("Poster") if data.get("Poster") != "N/A" else None),
        )

    async def find_best_match(
        self, title: str, year: int | None = None
    ) -> MovieResult | None:
        """Find the best matching movie.

        First tries exact title match, then falls back to search.

        Args:
            title: Movie title
            year: Optional release year

        Returns:
            Best matching movie or None
        """
        # Try exact match first
        result = await self.get_movie_by_title(title, year)
        if result:
            return result

        # Try without year
        if year:
            result = await self.get_movie_by_title(title)
            if result:
                return result

        # Fall back to search
        results = await self.search_movie(title, year)
        if results:
            # If year provided, prefer exact year match
            if year:
                for r in results:
                    if r.year == year:
                        return r
            return results[0]

        # Try search without year
        if year:
            results = await self.search_movie(title)
            if results:
                return results[0]

        return None

    def clear_cache(self) -> None:
        """Clear the response cache."""
        self._cache.clear()
