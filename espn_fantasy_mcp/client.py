"""ESPN League client with time-bounded in-process caching.

The League object is fetched from ESPN and reused across tool calls so we don't
re-hit the API on every request. Unlike a fetch-once cache, the object is
considered *stale* after ``config.CACHE_TTL_SECONDS`` and the next tool call
transparently re-fetches it. This is what keeps rosters, free agents, and waiver
results from drifting out of sync with reality between calls.

Key functions:
- ``get_league`` returns a fresh-enough League, re-fetching if the cache is stale.
- ``reset_league`` / ``refresh_league`` tool force an immediate re-fetch.
- ``data_age_seconds`` / ``freshness_line`` let tools surface how old the data is.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from . import config

try:
    from espn_api.football import League
except ImportError as exc:  # pragma: no cover - surfaced at runtime
    raise SystemExit(
        "The 'espn-api' package is required. Install it with: pip install espn-api"
    ) from exc


_league: Optional[League] = None
_fetched_at: Optional[float] = None  # epoch seconds of the last successful fetch


def _build_league() -> League:
    """Construct a League from config, raising RuntimeError on any failure."""
    if not config.LEAGUE_ID:
        raise RuntimeError(
            "ESPN_LEAGUE_ID is not set. Add it to the server's environment "
            "(see the claude_desktop_config.json snippet in the README)."
        )
    try:
        return League(
            league_id=int(config.LEAGUE_ID),
            year=config.YEAR,
            espn_s2=config.ESPN_S2,
            swid=config.SWID,
        )
    except Exception as exc:  # noqa: BLE001 - report any init failure clearly
        raise RuntimeError(
            f"Could not load league {config.LEAGUE_ID} for {config.YEAR}: {exc}. "
            "For private leagues, verify ESPN_S2 and ESPN_SWID are set correctly."
        ) from exc


def _reload() -> None:
    """Fetch a fresh League and record the fetch time. Raises on failure."""
    global _league, _fetched_at
    league = _build_league()
    _league = league
    _fetched_at = time.time()


def get_league(max_age: Optional[float] = None) -> League:
    """Return a fresh-enough cached League, re-fetching from ESPN when stale.

    Args:
        max_age: Override the staleness threshold in seconds for this call. Use
            0 to accept the cached copy regardless of age; omit to use
            ``config.CACHE_TTL_SECONDS``.

    On first use this fetches from ESPN. On later calls it returns the cached
    League if it is younger than the TTL, otherwise it re-fetches. If a
    re-fetch fails but a previous copy exists, the (stale) copy is returned
    rather than raising — a transient network blip should not break every tool.

    Raises RuntimeError (with a helpful message) only when there is no usable
    League at all.
    """
    global _league, _fetched_at
    ttl = config.CACHE_TTL_SECONDS if max_age is None else max_age

    if _league is None or _fetched_at is None:
        _reload()  # nothing cached yet — let failures surface
        return _league  # type: ignore[return-value]

    if ttl and ttl > 0 and (time.time() - _fetched_at) > ttl:
        try:
            _reload()
        except RuntimeError:
            # Keep serving the stale copy; freshness_line() will show its age.
            pass

    return _league


def reset_league() -> None:
    """Clear the cache so the next get_league() re-fetches from ESPN."""
    global _league, _fetched_at
    _league = None
    _fetched_at = None


def fetched_at() -> Optional[datetime]:
    """When the current cached League was fetched, or None if never."""
    return datetime.fromtimestamp(_fetched_at) if _fetched_at else None


def data_age_seconds() -> Optional[float]:
    """Age of the cached League in seconds, or None if nothing is cached."""
    return (time.time() - _fetched_at) if _fetched_at else None


def _age_phrase(age: float) -> str:
    mins = int(age // 60)
    if mins < 1:
        return "just now"
    if mins == 1:
        return "1 min ago"
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    return f"{hours}h {mins % 60}m ago"


def freshness_line() -> str:
    """A one-line note tools can append so data age is always visible.

    Returns an empty string if nothing has been fetched yet.
    """
    age = data_age_seconds()
    stamp = fetched_at()
    if age is None or stamp is None:
        return ""
    return (
        f"_Data as of {stamp.strftime('%H:%M')} ({_age_phrase(age)}). "
        "Roster/waiver changes can take a few minutes to appear on ESPN; "
        "call refresh_league to force an update._"
    )
