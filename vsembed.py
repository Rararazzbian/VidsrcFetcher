"""
vsembed.ru stream extractor

Flow:
  Movie: /embed/{imdbid}
    → cloudnestra.com/rcp/{hash}    (thumbnail + play button, Turnstile-gated)
    → cloudnestra.com/prorcp/{hash} (actual player, contains m3u8 URLs)

  Series: /embed/{imdbid}/{season}/{episode}
    → /embed/tv?imdb={imdbid}&season={s}&episode={e}
    → cloudnestra.com/rcp/{hash}
    → cloudnestra.com/prorcp/{hash}

Turnstile bypass:
  Cloudflare's bot-detection fingerprints the TLS handshake.  Python's
  built-in ssl module has a different JA3 signature than a real browser, so
  requests triggers the Turnstile challenge while curl (libcurl/OpenSSL) does
  not.  We use curl_cffi which ships libcurl compiled with Chrome's TLS
  fingerprint, letting us fetch every page without CAPTCHA friction.

The prorcp page embeds the m3u8 as:
  file: "https://tmstrN.{vX}/pl/{path}/master.m3u8 or ..."
{vX} placeholders are CDN shards — replace with "cloudnestra.com" directly.
"""

import re
import sys
from urllib.parse import urljoin

from curl_cffi import requests

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
VSEMBED_BASE    = "https://vsembed.ru"
CLOUDNESTRA_BASE = "https://cloudnestra.com"
IMPERSONATE     = "chrome120"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, referer: str = VSEMBED_BASE + "/") -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": UA, "Referer": referer},
        impersonate=IMPERSONATE,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.text


def _extract_rcp_hash(html: str) -> str | None:
    m = re.search(r'cloudnestra\.com/rcp/([^"\'>\s]+)', html)
    return m.group(1) if m else None


def _extract_prorcp_hash(html: str) -> str | None:
    m = re.search(r"/prorcp/([^'\">\s]+)", html)
    return m.group(1) if m else None


def _extract_m3u8_urls(html: str) -> list[str]:
    """Pull all m3u8 URLs from the Playerjs `file:` string."""
    m = re.search(r'file:\s*"(https://[^"]+\.m3u8[^"]*)"', html)
    if not m:
        return []
    parts = re.split(r"\s+or\s+", m.group(1))
    seen: set[str] = set()
    urls: list[str] = []
    for part in parts:
        # {v1}–{v4} → cloudnestra.com  |  {v5} → already app2.cloudnestra.com
        resolved = re.sub(r"\{v\d+\}", "cloudnestra.com", part).strip()
        if resolved not in seen:
            seen.add(resolved)
            urls.append(resolved)
    return urls


def _streams_for_rcp_hash(rcp_hash: str, inner_url: str) -> list[str]:
    rcp_html = _get(f"{CLOUDNESTRA_BASE}/rcp/{rcp_hash}", referer=inner_url)
    prorcp_hash = _extract_prorcp_hash(rcp_html)
    if not prorcp_hash:
        raise ValueError("Could not find prorcp hash in rcp page")
    prorcp_html = _get(f"{CLOUDNESTRA_BASE}/prorcp/{prorcp_hash}",
                       referer=CLOUDNESTRA_BASE + "/")
    return _extract_m3u8_urls(prorcp_html)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_movie_streams(imdb_id: str) -> list[str]:
    """Return m3u8 stream URLs for a movie (IMDb ID, e.g. 'tt6718170')."""
    embed_url = f"{VSEMBED_BASE}/embed/{imdb_id}"
    html      = _get(embed_url)
    rcp_hash  = _extract_rcp_hash(html)
    if not rcp_hash:
        raise ValueError(f"No rcp hash found on {embed_url}")
    return _streams_for_rcp_hash(rcp_hash, embed_url)


def get_episode_streams(imdb_id: str, season: int, episode: int) -> list[str]:
    """Return m3u8 stream URLs for a TV episode."""
    outer_url  = f"{VSEMBED_BASE}/embed/{imdb_id}/{season}/{episode}"
    outer_html = _get(outer_url)

    # Active episode's data-iframe points to the inner /embed/tv URL
    m = re.search(
        rf'data-iframe="(/embed/tv\?imdb={re.escape(imdb_id)}'
        rf'&season={season}&episode={episode}[^"]*)"',
        outer_html,
    )
    if m:
        inner_path = m.group(1)
    else:
        m2 = re.search(r'id="player_iframe"\s+src="([^"]+)"', outer_html)
        if not m2:
            raise ValueError(f"Cannot find inner embed URL in {outer_url}")
        inner_path = m2.group(1)

    inner_url  = urljoin(VSEMBED_BASE, inner_path)
    inner_html = _get(inner_url, referer=outer_url)
    rcp_hash   = _extract_rcp_hash(inner_html)
    if not rcp_hash:
        raise ValueError(f"No rcp hash found on {inner_url}")
    return _streams_for_rcp_hash(rcp_hash, inner_url)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) == 2:
        imdb = sys.argv[1]
        print(f"Fetching streams for movie {imdb} …")
        streams = get_movie_streams(imdb)
    elif len(sys.argv) == 4:
        imdb, s, e = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
        print(f"Fetching streams for {imdb} S{s:02d}E{e:02d} …")
        streams = get_episode_streams(imdb, s, e)
    else:
        print("Usage:")
        print("  python3 vsembed.py <imdb_id>                    # movie")
        print("  python3 vsembed.py <imdb_id> <season> <episode> # TV series")
        sys.exit(1)

    if not streams:
        print("No streams found.")
        sys.exit(1)

    print(f"\nFound {len(streams)} stream URL(s):\n")
    for i, url in enumerate(streams, 1):
        print(f"  [{i}] {url}")
