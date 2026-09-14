"""ESPN League client with simple in-process caching.

The League object is fetched once and reused for the life of the process so we
don't re-hit ESPN on every tool call. ``reset_league`` clears the cache; the
``refresh_league`` tool uses it to force a re-fetch.
"""

from __future__ import annotations

from typing import Optional

from . import config

try:
    from espn_api.football import League
except ImportError as exc:  # pragma: no cover - surfaced at runtime
    raise SystemExit(
        "The 'espn-api' package is required. Install it with: pip install espn-api"
    ) from exc


_league: Optional[League] = None


def get_league() -> League:
    """Return a cached League object, initializing it on first use.

    Raises RuntimeError with a helpful message if configuration is missing or
    the league cannot be loaded.
    """
    global _league
    if _league is not None:
        return _league

    if not config.LEAGUE_ID:
        raise RuntimeError(
            "ESPN_LEAGUE_ID is not set. Add it to the server's environment "
            "(see the claude_desktop_config.json snippet in the README)."
        )

    try:
        _league = League(
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

    return _league


def reset_league() -> None:
    """Clear the cached League so the next get_league() re-fetches from ESPN."""
    global _league
    _league = None
