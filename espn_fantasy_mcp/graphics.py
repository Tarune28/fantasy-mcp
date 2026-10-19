"""ESPN-style card rendering for richer tool output.

This module composes PNG "cards" — circular player headshots pulled from ESPN's
image CDN, team colors, projected points, and swap/add-drop arrows — that Claude
Desktop renders inline as MCP image content, so a roster move *looks* like a move
instead of reading like a log line.

Everything here is best-effort and degrades gracefully. If Pillow isn't
installed, the MCP ``Image`` helper is unavailable, visuals are disabled via
``ESPN_MCP_VISUALS=0``, or a headshot simply can't be fetched, the caller still
gets its normal text back — a rendered card is *never* required for a tool to
work. Callers wrap their text with :func:`visual`, which attaches a card when it
can and returns the plain string when it can't.

Rendering notes:
- This is a stdio MCP server, so nothing here may write to stdout; failures are
  swallowed silently (the text output already tells the user what happened).
- Cards are drawn at 2x and downscaled (LANCZOS) so text and circles are crisp.
- Fetched assets are cached in-process (successes forever — they're immutable;
  failures briefly, so one CDN blip doesn't wedge every later card).
"""

from __future__ import annotations

import io
import time
import threading
from typing import Any, Optional

from . import config

# --- Optional dependencies, resolved once at import ------------------------
# The MCP Image helper (mcp 2.x renamed the module). ``_ImageHelper`` is what we
# actually build content with; ``Image`` is the symbol tools use in their return
# annotation to switch MCPServer into content mode (it must be a recognized
# content type). When the helper is missing we fall back the annotation to
# ImageContent purely so the annotation stays valid; visual() then returns text.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image as _ImageHelper
except ImportError:  # pragma: no cover - mcp 1.x
    try:
        from mcp.server.fastmcp import Image as _ImageHelper
    except ImportError:  # pragma: no cover - no image helper at all
        _ImageHelper = None  # type: ignore[assignment]

try:
    from mcp.types import ImageContent as _ImageContent
except ImportError:  # pragma: no cover
    _ImageContent = None  # type: ignore[assignment]

# Exported for tool return annotations: `-> list[str | Image]`.
Image = _ImageHelper if _ImageHelper is not None else _ImageContent

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image as PILImage, ImageDraw, ImageFont

    _PIL_OK = True
except ImportError:  # pragma: no cover
    _PIL_OK = False


def _can_render() -> bool:
    """True only if we can both build a card and return it as image content."""
    return bool(config.VISUALS_ENABLED and _PIL_OK and _ImageHelper is not None
                and requests is not None)


# --- Palette ---------------------------------------------------------------
_BG = (11, 14, 19)          # near-black card background
_PANEL = (22, 27, 34)       # slightly lifted panel
_PANEL_ALT = (28, 34, 43)   # alternating roster row
_INK = (240, 243, 247)      # primary text
_MUTED = (140, 148, 160)    # secondary text
_HAIR = (46, 53, 63)        # hairline / divider
_ESPN_RED = (213, 10, 10)
_GREEN = (47, 180, 87)
_AMBER = (245, 166, 35)
_BLUE = (54, 132, 214)

# NFL primary colors keyed by the abbreviations espn-api's proTeam produces.
_TEAM_COLORS: dict[str, tuple[int, int, int]] = {
    "ARI": (0x97, 0x23, 0x3F), "ATL": (0xA7, 0x19, 0x30), "BAL": (0x24, 0x17, 0x73),
    "BUF": (0x00, 0x33, 0x8D), "CAR": (0x00, 0x85, 0xCA), "CHI": (0x0B, 0x16, 0x2A),
    "CIN": (0xFB, 0x4F, 0x14), "CLE": (0x31, 0x1D, 0x00), "DAL": (0x04, 0x1E, 0x42),
    "DEN": (0xFB, 0x4F, 0x14), "DET": (0x00, 0x76, 0xB6), "GB": (0x20, 0x37, 0x31),
    "HOU": (0x03, 0x20, 0x2F), "IND": (0x00, 0x2C, 0x5F), "JAX": (0x00, 0x67, 0x78),
    "KC": (0xE3, 0x18, 0x37), "LAC": (0x00, 0x80, 0xC6), "LAR": (0x00, 0x35, 0x94),
    "LV": (0x16, 0x16, 0x16), "MIA": (0x00, 0x8E, 0x97), "MIN": (0x4F, 0x26, 0x83),
    "NE": (0x00, 0x22, 0x44), "NO": (0xD3, 0xBC, 0x8D), "NYG": (0x0B, 0x22, 0x65),
    "NYJ": (0x12, 0x57, 0x40), "PHI": (0x00, 0x4C, 0x54), "PIT": (0xFF, 0xB6, 0x12),
    "SEA": (0x00, 0x22, 0x44), "SF": (0xAA, 0x00, 0x00), "TB": (0xD5, 0x0A, 0x0A),
    "TEN": (0x0C, 0x23, 0x40), "WSH": (0x5A, 0x14, 0x14),
}


def _team_color(abbr: Optional[str]) -> tuple[int, int, int]:
    return _TEAM_COLORS.get(str(abbr or "").upper(), (60, 68, 80))


# --- Asset fetching (headshots / logos) ------------------------------------
_ASSET_CACHE: dict[str, Optional[bytes]] = {}
_ASSET_FAIL_AT: dict[str, float] = {}
_ASSET_LOCK = threading.Lock()
_FAIL_TTL = 120.0  # re-try a failed asset after this many seconds

_HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nfl/players/full/{pid}.png"
_LOGO_URL = "https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png"


def _fetch(url: str) -> Optional[bytes]:
    """Fetch image bytes with in-process caching and a short failure TTL."""
    if requests is None:
        return None
    now = time.time()
    with _ASSET_LOCK:
        if url in _ASSET_CACHE:
            return _ASSET_CACHE[url]
        failed_at = _ASSET_FAIL_AT.get(url)
        if failed_at is not None and (now - failed_at) < _FAIL_TTL:
            return None
    data: Optional[bytes] = None
    try:
        r = requests.get(url, timeout=config.VISUALS_TIMEOUT)
        if r.status_code == 200 and r.content:
            data = r.content
    except Exception:  # noqa: BLE001 - never let a card break a tool
        data = None
    with _ASSET_LOCK:
        if data is None:
            _ASSET_FAIL_AT[url] = now
        else:
            _ASSET_CACHE[url] = data
    return data


def _player_image_bytes(player: Any) -> Optional[bytes]:
    """Headshot for a skill player, or the team logo for a D/ST."""
    pos = str(getattr(player, "position", "") or "").upper()
    abbr = str(getattr(player, "proTeam", "") or "").upper()
    if pos in ("D/ST", "DST", "DEF"):
        if abbr and abbr != "NONE":
            return _fetch(_LOGO_URL.format(abbr=abbr.lower()))
        return None
    pid = getattr(player, "playerId", None)
    if isinstance(pid, int) and pid > 0:
        raw = _fetch(_HEADSHOT_URL.format(pid=pid))
        if raw:
            return raw
    # Fall back to the team logo so the coin is never empty.
    if abbr and abbr != "NONE":
        return _fetch(_LOGO_URL.format(abbr=abbr.lower()))
    return None


# --- Fonts -----------------------------------------------------------------
_FONT_CACHE: dict[tuple[int, bool], Any] = {}
_REGULAR_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "DejaVuSans.ttf",
]
_BOLD_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


def _font(size: int, bold: bool = False) -> Any:
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    candidates = _BOLD_FONTS if bold else _REGULAR_FONTS
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:  # noqa: BLE001
            continue
    if font is None:  # pragma: no cover - last resort, tiny bitmap font
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# --- Drawing primitives (all coordinates in device px; S applied by caller) -
_S = 2  # supersample factor


def _P(v: float) -> int:
    """Scale a logical coordinate to device px."""
    return int(round(v * _S))


def _text_w(draw: Any, text: str, font: Any) -> float:
    return draw.textlength(text, font=font)


def _fit(draw: Any, text: str, font: Any, max_w: float) -> str:
    """Truncate ``text`` with an ellipsis so it fits within ``max_w`` device px."""
    if _text_w(draw, text, font) <= max_w:
        return text
    ell = "…"
    out = text
    while out and _text_w(draw, out + ell, font) > max_w:
        out = out[:-1]
    return (out + ell) if out else ell


def _draw_text(draw, xy, text, font, fill, anchor="la"):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _chip(draw, box, text, font, *, fill, text_fill):
    """A rounded 'pill' with centered text. box = (x0, y0, x1, y1) device px."""
    x0, y0, x1, y1 = box
    radius = (y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=fill)
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    _draw_text(draw, (cx, cy - _P(0.5)), text, font, text_fill, anchor="mm")


def _avatar(player: Any, diameter_px: int, ring: tuple[int, int, int]) -> Any:
    """A circular team-color 'coin' with the player's headshot/logo on top."""
    d = diameter_px
    team = _team_color(getattr(player, "proTeam", None))
    coin = PILImage.new("RGBA", (d, d), (0, 0, 0, 0))
    cd = ImageDraw.Draw(coin)
    # Team-color disc, subtly darker at the rim for depth.
    cd.ellipse([0, 0, d - 1, d - 1], fill=team + (255,))

    raw = _player_image_bytes(player)
    if raw is not None:
        try:
            im = PILImage.open(io.BytesIO(raw)).convert("RGBA")
            w, h = im.size
            side = min(w, h)
            left = (w - side) // 2
            # Portrait headshots put the head up top; bias the square upward.
            top = 0 if h >= w else (h - side) // 2
            im = im.crop((left, top, left + side, top + side)).resize(
                (d, d), PILImage.LANCZOS
            )
            coin.alpha_composite(im)
        except Exception:  # noqa: BLE001 - keep the plain coin
            pass
    else:
        # No asset: put the position on the coin so it still reads.
        pos = str(getattr(player, "position", "") or "?")[:3]
        f = _font(int(d * 0.34), bold=True)
        cd.text((d / 2, d / 2), pos, font=f, fill=(255, 255, 255, 235), anchor="mm")

    # Clip to a circle and add a ring (white underlay so dark rings still read).
    mask = PILImage.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    out = PILImage.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(coin, (0, 0), mask)
    od = ImageDraw.Draw(out)
    rw = max(2, int(d * 0.035))
    od.ellipse([1, 1, d - 2, d - 2], outline=(255, 255, 255, 60), width=rw + _S)
    od.ellipse([1, 1, d - 2, d - 2], outline=ring + (255,), width=rw)
    return out


def _finish(img: Any) -> bytes:
    """Downscale the 2x canvas and return PNG bytes."""
    w, h = img.size
    img = img.convert("RGB").resize((w // _S, h // _S), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _header(draw, w, *, kicker, title, badge_text, badge_color):
    """Shared top strip: red tab + kicker, title, and a status badge."""
    # ESPN-red accent tab.
    draw.rectangle([0, 0, _P(6), _P(96)], fill=_ESPN_RED)
    _draw_text(draw, (_P(28), _P(22)), kicker.upper(), _font(_P(13), bold=True),
               _ESPN_RED, anchor="la")
    _draw_text(draw, (_P(28), _P(40)), _fit(draw, title, _font(_P(26), bold=True),
               w - _P(230)), _font(_P(26), bold=True), _INK, anchor="la")
    # Status badge, right-aligned.
    bf = _font(_P(13), bold=True)
    bw = _text_w(draw, badge_text.upper(), bf) + _P(28)
    x1 = w - _P(28)
    _chip(draw, (x1 - bw, _P(26), x1, _P(26) + _P(30)), badge_text.upper(), bf,
          fill=badge_color, text_fill=(255, 255, 255))
    draw.line([(_P(28), _P(96)), (w - _P(28), _P(96))], fill=_HAIR, width=_S)


# --- Public card builders --------------------------------------------------

def render_swap_card(
    team_name: str,
    week: Optional[int],
    moves: list[dict[str, Any]],
    *,
    submitted: bool,
) -> Optional[bytes]:
    """A lineup-change card. ``moves`` is 1-2 dicts:

    {"player": PlayerObj, "from": "Bench", "to": "FLEX", "proj": 12.3}
    """
    if not _can_render() or not moves:
        return None
    try:
        w = _P(920)
        h = _P(480)
        img = PILImage.new("RGBA", (w, h), _BG + (255,))
        draw = ImageDraw.Draw(img)
        badge = ("Submitted", _GREEN) if submitted else ("Preview", _AMBER)
        wk = f"  ·  Week {week}" if week else ""
        _header(draw, w, kicker="Lineup Change", title=f"{team_name}{wk}",
                badge_text=badge[0], badge_color=badge[1])

        two = len(moves) >= 2
        centers = [w * 0.28, w * 0.72] if two else [w * 0.30]
        for c, mv in zip(centers, moves):
            _player_tile(draw, img, c, _P(150), mv)

        # Center connector between the two players (or player -> slot).
        cy = _P(230)
        if two:
            _connector(draw, w * 0.28 + _P(95), w * 0.72 - _P(95), cy, "SWAP", _ESPN_RED)
        else:
            mv = moves[0]
            _connector(draw, w * 0.30 + _P(95), w * 0.70, cy,
                       str(mv.get("to", "")).upper() or "MOVE", _BLUE)

        _footer(draw, w, h, "Preview only — nothing submitted yet."
                if not submitted else "Applied to ESPN. Locked players can't move.")
        return _finish(img)
    except Exception:  # noqa: BLE001
        return None


def render_add_drop_card(
    team_name: str,
    add_player: Any,
    drop_player: Any,
    *,
    kind: str,
    bid: Optional[int],
    submitted: bool,
    add_proj: float = 0.0,
    drop_proj: float = 0.0,
) -> Optional[bytes]:
    """An add/drop (or waiver-claim) card: ADD on the left, DROP on the right."""
    if not _can_render():
        return None
    try:
        w = _P(920)
        h = _P(430)
        img = PILImage.new("RGBA", (w, h), _BG + (255,))
        draw = ImageDraw.Draw(img)
        if submitted:
            badge = ("Submitted", _GREEN)
        elif "waiver" in kind.lower():
            badge = ("Queued", _BLUE)
        else:
            badge = ("Preview", _AMBER)
        _header(draw, w, kicker=kind, title=team_name,
                badge_text=badge[0], badge_color=badge[1])

        _player_tile(draw, img, w * 0.28, _P(150),
                     {"player": add_player, "label": "ADD", "label_color": _GREEN,
                      "proj": add_proj}, label_top=True)
        _player_tile(draw, img, w * 0.72, _P(150),
                     {"player": drop_player, "label": "DROP", "label_color": _ESPN_RED,
                      "proj": drop_proj}, label_top=True)

        _connector(draw, w * 0.28 + _P(95), w * 0.72 - _P(95), _P(230),
                   f"${bid} FAAB" if (bid and bid > 0) else "SWAP",
                   _BLUE if (bid and bid > 0) else _ESPN_RED)

        note = ("Queued for the next waiver run — it can still lose."
                if "waiver" in kind.lower() and not submitted else
                ("Submitted to ESPN." if submitted else
                 "Preview only — the dropped player is released immediately on confirm."))
        _footer(draw, w, h, note)
        return _finish(img)
    except Exception:  # noqa: BLE001
        return None


def render_roster_card(
    team_name: str,
    owner: str,
    week: Optional[int],
    record: str,
    capacity: str,
    starters: list[dict[str, Any]],
    bench: list[dict[str, Any]],
) -> Optional[bytes]:
    """A full-roster card: starters then bench, each a headshot row.

    ``starters``/``bench`` items: {"player", "slot", "proj", "inj"}.
    """
    if not _can_render():
        return None
    try:
        rows = starters + bench
        row_h = 58
        top = 128
        section_gap = 34
        # header + starters(section head + rows) + gap + bench(section head + rows)
        body = (30 + len(starters) * row_h) + section_gap + (30 + len(bench) * row_h)
        h_logical = top + body + 46
        w = _P(760)
        h = _P(h_logical)
        img = PILImage.new("RGBA", (w, h), _BG + (255,))
        draw = ImageDraw.Draw(img)

        wk = f"  ·  Week {week}" if week else ""
        _header(draw, w, kicker="Roster", title=f"{team_name}{wk}",
                badge_text=record or "—", badge_color=_PANEL_ALT)
        _draw_text(draw, (_P(28), _P(104)),
                   _fit(draw, f"{owner}   •   {capacity}", _font(_P(13)), w - _P(56)),
                   _font(_P(13)), _MUTED, anchor="la")

        y = _P(top)
        y = _section(draw, w, y, "STARTERS")
        for i, r in enumerate(starters):
            y = _roster_row(draw, img, w, y, _P(row_h), r, i)
        y += _P(section_gap)
        y = _section(draw, w, y, "BENCH / IR")
        for i, r in enumerate(bench):
            y = _roster_row(draw, img, w, y, _P(row_h), r, i)
        return _finish(img)
    except Exception:  # noqa: BLE001
        return None


# --- Card sub-components ----------------------------------------------------

def _player_tile(draw, img, cx, d, mv, *, label_top=False):
    """A centered player: avatar, optional label, name, meta, proj, slot move."""
    player = mv.get("player")
    ring = _team_color(getattr(player, "proTeam", None))
    if ring == (60, 68, 80):
        ring = _ESPN_RED
    avatar = _avatar(player, d, ring)
    ax = int(cx - d / 2)
    ay = _P(118)
    img.alpha_composite(avatar, (ax, ay))

    name = str(getattr(player, "name", "?"))
    pos = str(getattr(player, "position", "") or "")
    team = str(getattr(player, "proTeam", "") or "")
    jersey = getattr(player, "jersey", None)
    meta_bits = []
    if jersey not in (None, "", 0, "0"):
        meta_bits.append(f"#{jersey}")
    if pos:
        meta_bits.append(pos)
    if team and team.upper() != "NONE":
        meta_bits.append(team)
    meta = "  ·  ".join(meta_bits)

    if label_top and mv.get("label"):
        lf = _font(_P(13), bold=True)
        lw = _text_w(draw, mv["label"], lf) + _P(24)
        _chip(draw, (int(cx - lw / 2), _P(96), int(cx + lw / 2), _P(96) + _P(26)),
              mv["label"], lf, fill=mv.get("label_color", _ESPN_RED),
              text_fill=(255, 255, 255))

    name_y = ay + d + _P(16)
    _draw_text(draw, (cx, name_y),
               _fit(draw, name, _font(_P(21), bold=True), _P(300)),
               _font(_P(21), bold=True), _INK, anchor="ma")
    _draw_text(draw, (cx, name_y + _P(28)), meta, _font(_P(14)), _MUTED, anchor="ma")

    proj = mv.get("proj")
    if isinstance(proj, (int, float)) and proj:
        _draw_text(draw, (cx, name_y + _P(50)), f"{proj:.1f}",
                   _font(_P(30), bold=True), _INK, anchor="ma")
        _draw_text(draw, (cx, name_y + _P(88)), "PROJ", _font(_P(11), bold=True),
                   _MUTED, anchor="ma")

    # Slot transition chips (from -> to), when present.
    frm, to = mv.get("from"), mv.get("to")
    if frm and to:
        _slot_transition(draw, cx, name_y + _P(112), str(frm), str(to))


def _slot_transition(draw, cx, y, frm, to):
    f = _font(_P(12), bold=True)
    fw = _text_w(draw, frm.upper(), f) + _P(20)
    tw = _text_w(draw, to.upper(), f) + _P(20)
    gap = _P(26)
    total = fw + gap + tw
    x = cx - total / 2
    _chip(draw, (int(x), int(y), int(x + fw), int(y + _P(24))), frm.upper(), f,
          fill=_PANEL_ALT, text_fill=_MUTED)
    ax0 = x + fw + _P(5)
    ax1 = x + fw + gap - _P(5)
    ay = y + _P(12)
    draw.line([(ax0, ay), (ax1, ay)], fill=_INK, width=_S)
    draw.polygon([(ax1, ay - _P(4)), (ax1, ay + _P(4)), (ax1 + _P(6), ay)], fill=_INK)
    _chip(draw, (int(x + fw + gap), int(y), int(x + fw + gap + tw), int(y + _P(24))),
          to.upper(), f, fill=_GREEN, text_fill=(255, 255, 255))


def _connector(draw, x0, x1, y, label, color):
    """A horizontal double-arrow with a centered label pill between two tiles."""
    mid = (x0 + x1) / 2
    draw.line([(x0, y), (x1, y)], fill=_HAIR, width=_S)
    # arrowheads on both ends
    draw.polygon([(x0, y), (x0 + _P(9), y - _P(6)), (x0 + _P(9), y + _P(6))], fill=_MUTED)
    draw.polygon([(x1, y), (x1 - _P(9), y - _P(6)), (x1 - _P(9), y + _P(6))], fill=_MUTED)
    f = _font(_P(15), bold=True)
    lw = _text_w(draw, label, f) + _P(30)
    _chip(draw, (int(mid - lw / 2), int(y - _P(17)), int(mid + lw / 2), int(y + _P(17))),
          label, f, fill=color, text_fill=(255, 255, 255))


def _section(draw, w, y, title):
    _draw_text(draw, (_P(28), y), title, _font(_P(13), bold=True), _MUTED, anchor="la")
    draw.line([(_P(28), y + _P(24)), (w - _P(28), y + _P(24))], fill=_HAIR, width=_S)
    return y + _P(30)


def _roster_row(draw, img, w, y, row_h, r, idx):
    if idx % 2 == 1:
        draw.rectangle([_P(16), y, w - _P(16), y + row_h - _P(4)], fill=_PANEL_ALT)
    player = r.get("player")
    d = int(row_h - _P(16))
    ring = _team_color(getattr(player, "proTeam", None))
    if ring == (60, 68, 80):
        ring = _HAIR
    avatar = _avatar(player, d, ring)
    img.alpha_composite(avatar, (_P(30), int(y + _P(4))))

    cy = y + row_h / 2 - _P(2)
    # Slot chip.
    slot = str(r.get("slot", "") or "")
    sf = _font(_P(12), bold=True)
    sx0 = _P(30) + d + _P(14)
    sw = max(_text_w(draw, slot, sf) + _P(18), _P(56))
    starter = slot.upper() not in ("BE", "BENCH", "IR", "")
    _chip(draw, (int(sx0), int(cy - _P(12)), int(sx0 + sw), int(cy + _P(12))), slot, sf,
          fill=_ESPN_RED if starter else _PANEL_ALT,
          text_fill=(255, 255, 255) if starter else _MUTED)

    name_x = sx0 + sw + _P(16)
    name = str(getattr(player, "name", "?"))
    pos = str(getattr(player, "position", "") or "")
    team = str(getattr(player, "proTeam", "") or "")
    inj = str(r.get("inj", "") or "")
    _draw_text(draw, (name_x, cy - _P(11)),
               _fit(draw, name, _font(_P(16), bold=True), w - name_x - _P(150)),
               _font(_P(16), bold=True), _INK, anchor="lm")
    sub = " · ".join(b for b in (pos, team if team.upper() != "NONE" else "") if b)
    _draw_text(draw, (name_x, cy + _P(11)), sub, _font(_P(12)), _MUTED, anchor="lm")
    if inj:
        # An amber injury pill after the meta line (Arial has no ⚠ glyph).
        jf = _font(_P(11), bold=True)
        jx = name_x + _text_w(draw, sub, _font(_P(12))) + _P(10)
        jw = _text_w(draw, inj.upper(), jf) + _P(16)
        _chip(draw, (int(jx), int(cy + _P(3)), int(jx + jw), int(cy + _P(19))),
              inj.upper(), jf, fill=_AMBER, text_fill=(20, 20, 20))

    proj = r.get("proj")
    if isinstance(proj, (int, float)):
        _draw_text(draw, (w - _P(34), cy - _P(8)), f"{proj:.1f}",
                   _font(_P(18), bold=True), _INK, anchor="rm")
        _draw_text(draw, (w - _P(34), cy + _P(12)), "PROJ", _font(_P(9), bold=True),
                   _MUTED, anchor="rm")
    return y + row_h


def _footer(draw, w, h, text):
    draw.line([(_P(28), h - _P(44)), (w - _P(28), h - _P(44))], fill=_HAIR, width=_S)
    _draw_text(draw, (_P(28), h - _P(32)),
               _fit(draw, text, _font(_P(13)), w - _P(56)), _font(_P(13)),
               _MUTED, anchor="la")


# --- Result helper ---------------------------------------------------------

def visual(text: str, image_bytes: Optional[bytes]) -> Any:
    """Attach a rendered card to a tool's text output when one is available.

    Returns ``[text, Image]`` (rendered inline by Claude Desktop) when a card was
    produced, otherwise the plain ``text`` string. Tools annotate their return as
    ``list[str | Image]`` so MCPServer treats the value as content.
    """
    if image_bytes and _ImageHelper is not None:
        try:
            return [text, _ImageHelper(data=image_bytes, format="png")]
        except Exception:  # noqa: BLE001
            return text
    return text
