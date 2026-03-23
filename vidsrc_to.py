"""
vidsrc.to stream extractor

Flow:
  Movie: /embed/movie/{tmdb_or_imdb_id}
  Series: /embed/tv/{tmdb_or_imdb_id}/{season}/{episode}

    → page contains data-id for the media entry
    → /ajax/embed/episode/{data_id}/sources   → list of providers
    → /ajax/embed/source/{source_id}          → encoded player URL
    → decode URL → provider player page (VidPlay, FileMoon, etc.)
    → extract m3u8 from provider

VidPlay provider flow:
    → https://vidplay.online/e/{hash}?...
    → /futoken endpoint provides encoding keys
    → /mediainfo/... returns the actual m3u8 playlist

Uses curl_cffi for TLS fingerprinting (same approach as vsembed.py).
Accepts both TMDB IDs (numeric) and IMDb IDs (tt...).
"""

import base64
import json
import re
import sys
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from curl_cffi import requests

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
VIDSRC_BASE = "https://vidsrc.to"
IMPERSONATE = "chrome120"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, referer: str = VIDSRC_BASE + "/", raw: bool = False):
    """Fetch a URL with browser-like TLS fingerprint."""
    resp = requests.get(
        url,
        headers={"User-Agent": UA, "Referer": referer},
        impersonate=IMPERSONATE,
        timeout=15,
    )
    resp.raise_for_status()
    return resp if raw else resp.text


def _get_json(url: str, referer: str = VIDSRC_BASE + "/") -> dict:
    """Fetch JSON from an API endpoint."""
    resp = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        impersonate=IMPERSONATE,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Embed page parsing
# ---------------------------------------------------------------------------

def _extract_data_id(html: str) -> str | None:
    """Extract the data-id attribute from the embed page.

    The embed page contains an element like:
      <a ... data-id="XXXXX" ...>
    or it may be on a div/ul with the source list.
    """
    # Try multiple patterns the site has used
    for pattern in [
        r'data-id="([^"]+)"',
        r"data-id='([^']+)'",
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


def _get_sources(data_id: str, embed_url: str) -> list[dict]:
    """Fetch available source providers for the given media data-id.

    Returns a list of dicts like:
      [{"id": "xxx", "title": "Vidplay"}, {"id": "yyy", "title": "Filemoon"}, ...]
    """
    url = f"{VIDSRC_BASE}/ajax/embed/episode/{data_id}/sources"
    data = _get_json(url, referer=embed_url)
    if data.get("status") != 200:
        raise ValueError(f"Sources API returned status {data.get('status')}: {data}")
    return data.get("result", [])


def _get_source_url(source_id: str, embed_url: str) -> str:
    """Fetch the (possibly encoded) player URL for a source provider."""
    url = f"{VIDSRC_BASE}/ajax/embed/source/{source_id}"
    data = _get_json(url, referer=embed_url)
    if data.get("status") != 200:
        raise ValueError(f"Source URL API returned status {data.get('status')}: {data}")
    encrypted_url = data.get("result", {}).get("url", "")
    if not encrypted_url:
        raise ValueError("No URL returned from source API")
    return _decode_source_url(encrypted_url)


# ---------------------------------------------------------------------------
# URL decoding
# ---------------------------------------------------------------------------

def _decode_source_url(encoded: str) -> str:
    """Decode the source URL returned by the /ajax/embed/source/ API.

    vidsrc.to encodes the provider URL in a standardised way (base64 with
    character-rotation).  The exact scheme has changed over time – if decoding
    produces garbage, check the site's current JS for the latest variant.
    """
    # The site typically base64-encodes the URL, sometimes with a simple
    # character substitution or rotation applied first.
    # Try plain base64 first:
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass

    # Variant: standard base64 with URL-safe alphabet
    try:
        decoded = base64.urlsafe_b64decode(encoded + "==").decode("utf-8")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass

    # Variant: ROT13 + base64
    import codecs
    try:
        decoded = base64.b64decode(codecs.decode(encoded, "rot_13")).decode("utf-8")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass

    # Variant: reverse + base64
    try:
        decoded = base64.b64decode(encoded[::-1]).decode("utf-8")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass

    # Variant: the string is already a plain URL
    if encoded.startswith("http"):
        return encoded

    # If nothing worked, return as-is and let the caller handle it
    print(f"  [warn] Could not decode source URL, returning raw: {encoded[:80]}...")
    return encoded


# ---------------------------------------------------------------------------
# VidPlay provider extraction
# ---------------------------------------------------------------------------

def _extract_vidplay_streams(player_url: str, embed_url: str) -> list[str]:
    """Extract m3u8 URLs from a VidPlay player page.

    VidPlay (vidplay.online / vidplay.site) flow:
      1. GET /futoken → page with a JS array of keys
      2. Encode the video hash using those keys
      3. GET /mediainfo/{encoded}?{params} → JSON with m3u8 URLs
    """
    parsed = urlparse(player_url)
    vidplay_base = f"{parsed.scheme}://{parsed.netloc}"
    # The video hash/ID is the last path segment
    video_id = parsed.path.rstrip("/").split("/")[-1]
    query_params = parse_qs(parsed.query)

    # Step 1: Fetch futoken page to get encoding keys
    try:
        futoken_html = _get(f"{vidplay_base}/futoken",
                            referer=player_url)
    except Exception as e:
        print(f"  [warn] Failed to fetch futoken: {e}")
        # Fallback: try to get m3u8 directly from the player page
        return _extract_m3u8_from_page(player_url, embed_url)

    # Parse the key array from futoken page
    # Typically: var k='key_string'; or a JSON array
    keys = _parse_futoken_keys(futoken_html)
    if not keys:
        print("  [warn] Could not parse futoken keys, trying direct extraction")
        return _extract_m3u8_from_page(player_url, embed_url)

    # Step 2: Encode the video ID using the keys
    encoded_id = _encode_video_id(video_id, keys)

    # Step 3: Fetch mediainfo
    mediainfo_url = f"{vidplay_base}/mediainfo/{encoded_id}"
    # Preserve original query params (often includes auth tokens)
    if parsed.query:
        mediainfo_url += f"?{parsed.query}"

    try:
        mi_data = _get_json(mediainfo_url, referer=player_url)
    except Exception as e:
        print(f"  [warn] mediainfo request failed: {e}")
        return _extract_m3u8_from_page(player_url, embed_url)

    # Extract m3u8 from the response
    urls = []
    result = mi_data.get("result", {})

    # result may have "sources" array with m3u8 URLs
    if isinstance(result, dict):
        for source in result.get("sources", []):
            file_url = source.get("file", "")
            if file_url and ".m3u8" in file_url:
                urls.append(file_url)
            elif file_url:
                urls.append(file_url)
    elif isinstance(result, str) and ".m3u8" in result:
        urls.append(result)

    if not urls:
        # Fallback to page scraping
        return _extract_m3u8_from_page(player_url, embed_url)

    return urls


def _parse_futoken_keys(html: str) -> list[int] | None:
    """Extract the encoding key array from the futoken page."""
    # Pattern 1: var k='some_string'
    m = re.search(r"var\s+k\s*=\s*'([^']+)'", html)
    if m:
        key_str = m.group(1)
        return [ord(c) for c in key_str]

    # Pattern 2: JSON array of numbers
    m = re.search(r'\[(\d+(?:\s*,\s*\d+)+)\]', html)
    if m:
        return [int(x.strip()) for x in m.group(1).split(",")]

    return None


def _encode_video_id(video_id: str, keys: list[int]) -> str:
    """Encode the video ID using the futoken keys.

    The encoding XORs each character of the video ID with corresponding
    key values in a cycling pattern, then concatenates the results.
    """
    # Common encoding: futoken key + video_id chars processed
    encoded_parts = [str(keys[0])]  # First key element used as-is
    for i, char in enumerate(video_id):
        key_idx = (i % len(keys)) if len(keys) > 1 else 0
        encoded_parts.append(str(ord(char) + keys[key_idx]))
    return ",".join(encoded_parts)


# ---------------------------------------------------------------------------
# Filemoon provider extraction
# ---------------------------------------------------------------------------

def _extract_filemoon_streams(player_url: str, embed_url: str) -> list[str]:
    """Extract m3u8 URLs from a Filemoon player page."""
    html = _get(player_url, referer=embed_url)
    return _extract_m3u8_from_html(html)


# ---------------------------------------------------------------------------
# Generic m3u8 extraction helpers
# ---------------------------------------------------------------------------

def _extract_m3u8_from_page(player_url: str, embed_url: str) -> list[str]:
    """Fetch a player page and extract m3u8 URLs from it."""
    html = _get(player_url, referer=embed_url)
    return _extract_m3u8_from_html(html)


def _extract_m3u8_from_html(html: str) -> list[str]:
    """Pull all m3u8 URLs from HTML / JS source."""
    urls: list[str] = []
    seen: set[str] = set()

    # Pattern: file:"https://...m3u8..." (Playerjs style)
    for m in re.finditer(r'file:\s*"(https?://[^"]+\.m3u8[^"]*)"', html):
        parts = re.split(r"\s+or\s+", m.group(1))
        for part in parts:
            url = part.strip()
            if url not in seen:
                seen.add(url)
                urls.append(url)

    # Pattern: src:"https://...m3u8..."
    for m in re.finditer(r'src:\s*"(https?://[^"]+\.m3u8[^"]*)"', html):
        url = m.group(1).strip()
        if url not in seen:
            seen.add(url)
            urls.append(url)

    # Pattern: generic m3u8 URL in quotes
    if not urls:
        for m in re.finditer(r'"(https?://[^"]+\.m3u8[^"]*)"', html):
            url = m.group(1).strip()
            if url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

PROVIDER_EXTRACTORS = {
    "vidplay":   _extract_vidplay_streams,
    "filemoon":  _extract_filemoon_streams,
}


def _extract_streams_from_provider(
    provider_name: str, player_url: str, embed_url: str
) -> list[str]:
    """Route to the appropriate provider extractor."""
    name_lower = provider_name.lower()
    for key, extractor in PROVIDER_EXTRACTORS.items():
        if key in name_lower:
            return extractor(player_url, embed_url)
    # Unknown provider — try generic m3u8 extraction
    print(f"  [info] Unknown provider '{provider_name}', trying generic extraction")
    return _extract_m3u8_from_page(player_url, embed_url)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_movie_streams(media_id: str) -> list[str]:
    """Return m3u8 stream URLs for a movie.

    media_id can be a TMDB ID (e.g. '603') or IMDb ID (e.g. 'tt0133093').
    """
    embed_url = f"{VIDSRC_BASE}/embed/movie/{media_id}"
    return _get_streams(embed_url)


def get_episode_streams(media_id: str, season: int, episode: int) -> list[str]:
    """Return m3u8 stream URLs for a TV episode.

    media_id can be a TMDB ID or IMDb ID.
    """
    embed_url = f"{VIDSRC_BASE}/embed/tv/{media_id}/{season}/{episode}"
    return _get_streams(embed_url)


def _get_streams(embed_url: str) -> list[str]:
    """Core logic: fetch embed page → sources → provider → m3u8 URLs."""
    html = _get(embed_url)

    data_id = _extract_data_id(html)
    if not data_id:
        raise ValueError(f"No data-id found on {embed_url}")

    sources = _get_sources(data_id, embed_url)
    if not sources:
        raise ValueError(f"No sources returned for data-id {data_id}")

    all_streams: list[str] = []
    seen: set[str] = set()

    for source in sources:
        source_title = source.get("title", "unknown")
        source_id = source.get("id", "")
        if not source_id:
            continue

        print(f"  Trying source: {source_title} (id={source_id})")
        try:
            player_url = _get_source_url(source_id, embed_url)
            print(f"    Player URL: {player_url[:80]}...")
            streams = _extract_streams_from_provider(
                source_title, player_url, embed_url
            )
            for s in streams:
                if s not in seen:
                    seen.add(s)
                    all_streams.append(s)
            if streams:
                print(f"    → Found {len(streams)} stream(s)")
        except Exception as e:
            print(f"    → Failed: {e}")

    return all_streams


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) == 2:
        media_id = sys.argv[1]
        print(f"Fetching streams for movie {media_id} …")
        streams = get_movie_streams(media_id)
    elif len(sys.argv) == 4:
        media_id, s, e = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
        print(f"Fetching streams for {media_id} S{s:02d}E{e:02d} …")
        streams = get_episode_streams(media_id, s, e)
    else:
        print("Usage:")
        print("  python3 vidsrc_to.py <tmdb_or_imdb_id>                    # movie")
        print("  python3 vidsrc_to.py <tmdb_or_imdb_id> <season> <episode> # TV series")
        print()
        print("Examples:")
        print("  python3 vidsrc_to.py 603              # The Matrix (TMDB)")
        print("  python3 vidsrc_to.py tt0133093        # The Matrix (IMDb)")
        print("  python3 vidsrc_to.py 1399 1 1         # Game of Thrones S01E01")
        sys.exit(1)

    if not streams:
        print("\nNo streams found.")
        sys.exit(1)

    print(f"\nFound {len(streams)} stream URL(s):\n")
    for i, url in enumerate(streams, 1):
        print(f"  [{i}] {url}")
