"""nflverse weekly player stats adapter (design 2a, `nflverse_player_stats`).

URL VERIFICATION (design risk #1 -- probed live 2026-08-13 during L2 dev):

- PINNED (worked):
  https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv
  -> HTTP 200 for season 2025 via a 302 to objects.githubusercontent.com;
  final response carries an ETag (conditional GET works). Header verified
  to contain: player_id, player_name, player_display_name, position,
  season, week, season_type, game_id (e.g. "2025_01_PIT_NYJ"), team,
  opponent_team, completions, attempts, passing_yards, passing_tds,
  carries, rushing_yards, rushing_tds, receptions, targets,
  receiving_yards, receiving_tds, special_teams_tds, fantasy_points, ...
- 404 (dead / not yet published):
  .../stats_player/stats_player_week_2026.csv (season asset appears once
  the season has data -- a pre-season 404 is expected and reported loud),
  .../player_stats/player_stats_2025.csv (old nfl_data_py-generation
  naming, retired).

The presence of game_id in the verified shape means stat lines key
directly on (player_id, game_id) with no schedule join. Payload::

    {"season": int, "stat_lines": {"<player_id>|<game_id>": {
         player_id, player_name, position, team, opponent_team,
         season, week, phase, game_id,
         completions, attempts, passing_yards, passing_tds,
         carries, rushing_yards, rushing_tds,
         receptions, receiving_yards, receiving_tds,
         special_teams_tds, total_tds}, ...},
     "row_count": int, "skipped_rows": int, "source_url": str}

phase is REG | PRE | POST. Empty numeric cells coerce to 0 (kickers have
blank passing columns). total_tds = passing + rushing + receiving +
special-teams TDs. If the header drifts from the verified shape the
adapter fails LOUD with status ERROR naming the missing columns -- it
never returns an empty stat table that would look identical to "nobody
played well".
"""

import csv
import io

from signal_hub.adapters import base

SOURCE = "nflverse_player_stats"

# Ordered candidates, most-preferred first. Only the verified pattern is
# pinned; add fallbacks here if nflverse renames again (they will fail
# loud through the shape check regardless).
CANDIDATE_URL_TEMPLATES = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv",
)

RELEASES_PAGE = "https://github.com/nflverse/nflverse-data/releases"

REQUIRED_COLUMNS = (
    "player_id",
    "player_display_name",
    "season",
    "week",
    "season_type",
    "game_id",
    "team",
    "opponent_team",
    "passing_yards",
    "passing_tds",
    "rushing_yards",
    "rushing_tds",
    "receiving_yards",
    "receiving_tds",
)

_INT_COLUMNS = (
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "receptions",
    "receiving_yards",
    "receiving_tds",
    "special_teams_tds",
)


def url_for_season(season):
    return CANDIDATE_URL_TEMPLATES[0].format(season=season)


def fetch(http_get, season, etag=None, timeout=base.DEFAULT_TIMEOUT):
    """Fetch and normalize the season's weekly stat lines."""
    url = url_for_season(season)
    response, terminal = base.conditional_get(
        http_get, url, etag=etag, timeout=timeout
    )
    if terminal is not None:
        if response is not None and response.status == 404:
            return base.error_result(
                "HTTP 404 for %s -- either the %s season asset is not "
                "published yet (expected before the season has stat rows) "
                "or the nflverse release-asset naming drifted again; check "
                "%s and update CANDIDATE_URL_TEMPLATES in %s"
                % (url, season, RELEASES_PAGE, __name__)
            )
        return terminal
    payload, error = parse(response.body, season=season, url=url)
    if error is not None:
        return base.error_result(error)
    return base.ok_result(payload, etag=response.header("ETag"))


def parse(csv_bytes, season, url=""):
    """Pure parse: stats CSV bytes -> (payload, None) or (None, error)."""
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    header = reader.fieldnames or []
    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        return None, (
            "player-stats shape drift at %s: missing columns %s -- the "
            "verified 2026-08-13 shape changed; refusing to emit a stat "
            "table that may silently drop stats (check %s)"
            % (url or "<inline>", ", ".join(missing), RELEASES_PAGE)
        )

    stat_lines = {}
    skipped = 0
    for row in reader:
        line = _normalize_row(row)
        if line is None:
            skipped += 1
        else:
            stat_lines["%s|%s" % (line["player_id"], line["game_id"])] = line
    return {
        "season": season,
        "stat_lines": stat_lines,
        "row_count": len(stat_lines),
        "skipped_rows": skipped,
        "source_url": url,
    }, None


def _normalize_row(row):
    player_id = (row.get("player_id") or "").strip()
    game_id = (row.get("game_id") or "").strip()
    phase = base.normalize_phase(row.get("season_type"))
    if not player_id or not game_id or phase is None:
        return None
    try:
        season = int(row["season"])
        week = int(row["week"])
        stats = {col: _num(row.get(col)) for col in _INT_COLUMNS}
    except (ValueError, TypeError):
        return None
    line = {
        "player_id": player_id,
        "player_name": (row.get("player_display_name") or "").strip(),
        "position": (row.get("position") or "").strip(),
        "team": (row.get("team") or "").strip(),
        "opponent_team": (row.get("opponent_team") or "").strip(),
        "season": season,
        "week": week,
        "phase": phase,
        "game_id": game_id,
    }
    line.update(stats)
    line["total_tds"] = (
        stats["passing_tds"]
        + stats["rushing_tds"]
        + stats["receiving_tds"]
        + stats["special_teams_tds"]
    )
    return line


def _num(value):
    """Empty numeric cell -> 0; populated cell must parse (int-valued)."""
    value = (value or "").strip()
    if not value:
        return 0
    return int(float(value))
