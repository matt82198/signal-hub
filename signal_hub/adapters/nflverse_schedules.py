"""nflverse schedules adapter (design section 2a, source `nflverse_schedules`).

Fetches nfldata's games.csv (the same raw URL trend-indicator uses) and
normalizes it to the snapshot payload::

    {"games": [{game_id, season, week, phase, gameday, gametime,
                home_team, away_team, home_score, away_score,
                final, winner}, ...],
     "skipped_rows": int, "row_count": int, "source_url": str}

phase is REG | PRE | POST (WC/DIV/CON/SB fold into POST). Scores are int
or None; a game is `final` when both scores are present; `winner` is the
winning team code, "TIE", or None. Rows missing a game_id, with an
unknown game_type, or with non-numeric populated scores are skipped and
counted in skipped_rows (malformed-row tolerance, never a crash).

Verified 2026-08-13 by live probe: URL below returns HTTP 200 with an
ETag and a header containing all REQUIRED_COLUMNS.
"""

import csv
import io

from signal_hub.adapters import base

SOURCE = "nflverse_schedules"
SCHEDULES_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
)

REQUIRED_COLUMNS = (
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "away_score",
    "home_team",
    "home_score",
)


def fetch(http_get, etag=None, timeout=base.DEFAULT_TIMEOUT):
    """Fetch and normalize the schedule. Injected http_get only."""
    response, terminal = base.conditional_get(
        http_get, SCHEDULES_URL, etag=etag, timeout=timeout
    )
    if terminal is not None:
        return terminal
    payload, error = parse(response.body)
    if error is not None:
        return base.error_result(error)
    return base.ok_result(payload, etag=response.header("ETag"))


def parse(csv_bytes):
    """Pure parse: games.csv bytes -> (payload, None) or (None, error)."""
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    header = reader.fieldnames or []
    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        return None, (
            "schedule shape drift at %s: missing columns %s (got %s)"
            % (SCHEDULES_URL, ", ".join(missing), ", ".join(header) or "empty")
        )

    games = []
    skipped = 0
    for row in reader:
        game = _normalize_row(row)
        if game is None:
            skipped += 1
        else:
            games.append(game)
    return {
        "games": games,
        "row_count": len(games),
        "skipped_rows": skipped,
        "source_url": SCHEDULES_URL,
    }, None


def _normalize_row(row):
    game_id = (row.get("game_id") or "").strip()
    phase = base.normalize_phase(row.get("game_type"))
    if not game_id or phase is None:
        return None
    try:
        season = int(row["season"])
        week = int(row["week"])
        home_score = _score(row.get("home_score"))
        away_score = _score(row.get("away_score"))
    except (ValueError, TypeError):
        return None

    final = home_score is not None and away_score is not None
    winner = None
    if final:
        home, away = row["home_team"].strip(), row["away_team"].strip()
        if home_score > away_score:
            winner = home
        elif away_score > home_score:
            winner = away
        else:
            winner = "TIE"
    return {
        "game_id": game_id,
        "season": season,
        "week": week,
        "phase": phase,
        "gameday": (row.get("gameday") or "").strip(),
        "gametime": (row.get("gametime") or "").strip(),
        "home_team": row["home_team"].strip(),
        "away_team": row["away_team"].strip(),
        "home_score": home_score,
        "away_score": away_score,
        "final": final,
        "winner": winner,
    }


def _score(value):
    """Empty cell -> None (not played); populated cell must parse as int."""
    value = (value or "").strip()
    if not value:
        return None
    return int(float(value))
