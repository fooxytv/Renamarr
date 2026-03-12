# Renamarr

A media file renaming tool for Plex and Jellyfin. Scans your existing library, matches files against metadata APIs, and renames them to standard naming conventions — with a web UI for reviewing changes before they're applied.

## Features

- **Plex/Jellyfin standard naming** — renames movies and TV shows to the folder structures media servers expect
- **Web UI** — scan, review, approve or reject individual renames before executing
- **Metadata lookup** — OMDb API for movies, TVMaze API (free, no key needed) for TV shows
- **Duplicate detection** — finds duplicate files, compares quality (resolution, codec, bitrate), keeps the best
- **Quality analysis** — uses guessit for filename parsing and ffprobe for embedded metadata
- **Dry-run mode** — preview all changes without touching any files
- **Docker-ready** — runs as a container with Traefik integration

## Quick Start

### Docker Compose

```yaml
services:
  renamarr:
    image: ghcr.io/fooxytv/renamarr:latest
    container_name: renamarr
    restart: unless-stopped
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - renamarr-data:/app/data
      - /path/to/movies:/media/movies
      - /path/to/tv:/media/tv
      - /path/to/duplicates:/media/duplicates
    environment:
      - OMDB_API_KEY=your_key_here
      - DRY_RUN=true
    ports:
      - 8080:8080
```

### Get an OMDb API Key

Free at [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx) — only requires an email. TV shows use TVMaze which needs no key at all.

## Configuration

Create a `config.yaml`:

```yaml
omdb:
  api_key: "${OMDB_API_KEY}"

directories:
  movies:
    watch: /media/movies
    output: /media/movies
  tv:
    watch: /media/tv
    output: /media/tv

options:
  dry_run: true
  scan_interval: 21600
  min_file_age: 0

duplicates:
  action: move
  duplicates_folder: /media/duplicates

naming:
  movies: "{title} ({year})/{title} ({year}){ext}"
  tv: "{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d} - {episode_title}{ext}"
```

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `OMDB_API_KEY` | OMDb API key for movie lookups | Required |
| `DRY_RUN` | Set to `true` to preview without renaming | `false` |

## Web UI

Access the dashboard at `http://localhost:8080` (or via Traefik at `renamarr.home`).

1. Click **Scan Now** to scan your library
2. Review proposed renames — current name vs new name
3. **Approve** or **Reject** individual files (or use bulk actions)
4. Click **Execute Approved** to apply the renames

The duplicates tab shows files grouped by content with quality scores, so you can keep the best version.

## Run Modes

| Mode | Command | Description |
|---|---|---|
| Web UI | `python -m src.main --web` | Dashboard with approve/reject workflow (default in Docker) |
| One-shot | `python -m src.main --once` | Single scan, log results, exit |
| Continuous | `python -m src.main` | Watch directories + periodic rescans |
| Dry run | `python -m src.main --once --dry-run` | Preview without changes |

## Works With

Designed to complement Sonarr/Radarr — they handle new downloads, Renamarr cleans up existing files that weren't imported through the *arr pipeline.

## Naming Examples

**Movies:**
```
The.Matrix.1999.1080p.BluRay.x264.mkv
  -> The Matrix (1999)/The Matrix (1999).mkv
```

**TV Shows:**
```
Breaking.Bad.S01E01.720p.BluRay.mkv
  -> Breaking Bad/Season 01/Breaking Bad - S01E01 - Pilot.mkv
```
