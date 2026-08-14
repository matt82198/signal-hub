"""Tests for the L2 nflverse adapters (schedules + player stats).

All HTTP is mocked via the injectable http_get contract from
signal_hub.adapters.base -- no test touches the network.
"""

import unittest

from signal_hub.adapters import base
from signal_hub.adapters import nflverse_schedules as sched
from signal_hub.adapters import nflverse_player_stats as pstats


class MockHttp:
    """Injectable http_get double. Records calls, replays canned responses."""

    def __init__(self, responses):
        # responses: list of HttpResponse or Exception, consumed in order.
        self.responses = list(responses)
        self.calls = []  # (url, headers) tuples

    def __call__(self, url, headers=None, timeout=30):
        self.calls.append((url, dict(headers or {})))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def http_200(body, etag=None):
    headers = {"ETag": etag} if etag else {}
    return base.HttpResponse(status=200, headers=headers, body=body)


SCHEDULES_CSV = (
    "game_id,season,game_type,week,gameday,weekday,gametime,"
    "away_team,away_score,home_team,home_score\n"
    "2025_01_PIT_NYJ,2025,REG,1,2025-09-07,Sunday,13:00,PIT,34,NYJ,32\n"
    "2025_02_CHI_DET,2025,REG,2,2025-09-14,Sunday,13:00,CHI,,DET,\n"
    "2025_22_KC_PHI,2025,SB,22,2026-02-08,Sunday,18:30,KC,20,PHI,27\n"
    "2026_00_CHI_BUF,2026,PRE,0,2026-08-09,Saturday,19:00,CHI,17,BUF,17\n"
    ",2025,REG,1,2025-09-07,Sunday,13:00,AAA,1,BBB,2\n"
    "2025_01_BAD_ROW,2025,REG,1,2025-09-07,Sunday,13:00,AAA,abc,BBB,2\n"
).encode("utf-8")


STATS_CSV = (
    "player_id,player_display_name,position,season,week,season_type,game_id,"
    "team,opponent_team,completions,attempts,passing_yards,passing_tds,"
    "carries,rushing_yards,rushing_tds,receptions,receiving_yards,"
    "receiving_tds,special_teams_tds\n"
    "00-0023459,Aaron Rodgers,QB,2025,1,REG,2025_01_PIT_NYJ,PIT,NYJ,"
    "22,30,244,4,1,-1,0,0,0,0,0\n"
    "00-0036322,Justin Fields,QB,2025,2,REG,2025_02_CHI_DET,CHI,DET,"
    "25,33,312,3,6,44,1,0,0,0,0\n"
    "00-0023853,Matt Prater,K,2025,1,REG,2025_01_BAL_BUF,BUF,BAL,"
    "0,0,,,0,0,0,0,,,0\n"
    ",No Id,QB,2025,1,REG,2025_01_PIT_NYJ,PIT,NYJ,1,1,9,0,0,0,0,0,0,0,0\n"
).encode("utf-8")


class TestBase(unittest.TestCase):
    def test_normalize_phase(self):
        self.assertEqual(base.normalize_phase("REG"), "REG")
        self.assertEqual(base.normalize_phase("PRE"), "PRE")
        for post in ("POST", "WC", "DIV", "CON", "SB"):
            self.assertEqual(base.normalize_phase(post), "POST")
        self.assertIsNone(base.normalize_phase("???"))
        self.assertIsNone(base.normalize_phase(""))

    def test_result_helpers_shapes(self):
        ok = base.ok_result({"x": 1}, etag='W/"abc"')
        self.assertEqual(ok["status"], "OK")
        self.assertEqual(ok["payload"], {"x": 1})
        self.assertEqual(ok["etag"], 'W/"abc"')
        self.assertIsNone(ok["error"])
        un = base.unchanged_result('W/"abc"')
        self.assertEqual(un["status"], "UNCHANGED")
        self.assertIsNone(un["payload"])
        err = base.error_result("boom")
        self.assertEqual(err["status"], "ERROR")
        self.assertEqual(err["error"], "boom")
        self.assertIsNone(err["payload"])


class TestSchedules(unittest.TestCase):
    def test_fetch_ok_normalizes_games(self):
        http = MockHttp([http_200(SCHEDULES_CSV, etag='W/"e1"')])
        result = sched.fetch(http)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["etag"], 'W/"e1"')
        games = result["payload"]["games"]
        by_id = {g["game_id"]: g for g in games}
        self.assertEqual(len(games), 4)

        final = by_id["2025_01_PIT_NYJ"]
        self.assertEqual(final["phase"], "REG")
        self.assertEqual(final["season"], 2025)
        self.assertEqual(final["week"], 1)
        self.assertEqual(final["home_team"], "NYJ")
        self.assertEqual(final["away_team"], "PIT")
        self.assertEqual(final["home_score"], 32)
        self.assertEqual(final["away_score"], 34)
        self.assertTrue(final["final"])
        self.assertEqual(final["winner"], "PIT")
        self.assertEqual(final["gameday"], "2025-09-07")
        self.assertEqual(final["gametime"], "13:00")

        scheduled = by_id["2025_02_CHI_DET"]
        self.assertFalse(scheduled["final"])
        self.assertIsNone(scheduled["home_score"])
        self.assertIsNone(scheduled["away_score"])
        self.assertIsNone(scheduled["winner"])

        self.assertEqual(by_id["2025_22_KC_PHI"]["phase"], "POST")
        self.assertEqual(by_id["2025_22_KC_PHI"]["winner"], "PHI")

        pre = by_id["2026_00_CHI_BUF"]
        self.assertEqual(pre["phase"], "PRE")
        self.assertEqual(pre["winner"], "TIE")
        self.assertTrue(pre["final"])

        # malformed rows (missing game_id, non-numeric score) skipped, counted
        self.assertEqual(result["payload"]["skipped_rows"], 2)

    def test_fetch_sends_if_none_match_and_handles_304(self):
        http = MockHttp([base.HttpResponse(status=304, headers={}, body=b"")])
        result = sched.fetch(http, etag='W/"e1"')
        self.assertEqual(result["status"], "UNCHANGED")
        self.assertEqual(result["etag"], 'W/"e1"')
        self.assertIsNone(result["payload"])
        url, headers = http.calls[0]
        self.assertEqual(url, sched.SCHEDULES_URL)
        self.assertEqual(headers.get("If-None-Match"), 'W/"e1"')

    def test_fetch_without_etag_sends_no_conditional_header(self):
        http = MockHttp([http_200(SCHEDULES_CSV)])
        sched.fetch(http)
        _, headers = http.calls[0]
        self.assertNotIn("If-None-Match", headers)

    def test_missing_required_column_is_error(self):
        bad = b"game_id,season\n2025_01_PIT_NYJ,2025\n"
        http = MockHttp([http_200(bad)])
        result = sched.fetch(http)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("game_type", result["error"])
        self.assertIn(sched.SCHEDULES_URL, result["error"])

    def test_http_failure_is_error(self):
        http = MockHttp([base.HttpResponse(status=500, headers={}, body=b"")])
        result = sched.fetch(http)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("500", result["error"])

    def test_http_exception_is_error(self):
        http = MockHttp([OSError("connection refused")])
        result = sched.fetch(http)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("connection refused", result["error"])


class TestPlayerStats(unittest.TestCase):
    def test_fetch_ok_keys_stat_lines_by_player_and_game(self):
        http = MockHttp([http_200(STATS_CSV, etag='"s1"')])
        result = pstats.fetch(http, season=2025)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["etag"], '"s1"')
        payload = result["payload"]
        self.assertEqual(payload["season"], 2025)
        lines = payload["stat_lines"]
        key = "00-0036322|2025_02_CHI_DET"
        self.assertIn(key, lines)
        fields = lines[key]
        self.assertEqual(fields["player_id"], "00-0036322")
        self.assertEqual(fields["player_name"], "Justin Fields")
        self.assertEqual(fields["team"], "CHI")
        self.assertEqual(fields["opponent_team"], "DET")
        self.assertEqual(fields["game_id"], "2025_02_CHI_DET")
        self.assertEqual(fields["phase"], "REG")
        self.assertEqual(fields["week"], 2)
        self.assertEqual(fields["passing_yards"], 312)
        self.assertEqual(fields["passing_tds"], 3)
        self.assertEqual(fields["rushing_yards"], 44)
        self.assertEqual(fields["total_tds"], 4)  # 3 pass + 1 rush

        # empty numeric cells coerce to 0 (kicker row)
        kicker = lines["00-0023853|2025_01_BAL_BUF"]
        self.assertEqual(kicker["passing_yards"], 0)
        self.assertEqual(kicker["receiving_yards"], 0)

        # row without player_id skipped and counted
        self.assertEqual(payload["skipped_rows"], 1)
        self.assertEqual(len(lines), 3)

    def test_pinned_url_and_conditional_get(self):
        http = MockHttp([base.HttpResponse(status=304, headers={}, body=b"")])
        result = pstats.fetch(http, season=2025, etag='"s1"')
        self.assertEqual(result["status"], "UNCHANGED")
        url, headers = http.calls[0]
        self.assertEqual(
            url,
            "https://github.com/nflverse/nflverse-data/releases/download/"
            "stats_player/stats_player_week_2025.csv",
        )
        self.assertEqual(headers.get("If-None-Match"), '"s1"')

    def test_404_fails_loud_with_actionable_error(self):
        http = MockHttp([base.HttpResponse(status=404, headers={}, body=b"")])
        result = pstats.fetch(http, season=2026)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("404", result["error"])
        self.assertIn("stats_player_week_2026.csv", result["error"])
        self.assertIn("nflverse-data/releases", result["error"])

    def test_shape_drift_fails_loud(self):
        drifted = (
            b"player_id,player_display_name,season,week,season_type,game_id,"
            b"team,opponent_team\n"
            b"00-1,X Y,2025,1,REG,2025_01_A_B,A,B\n"
        )
        http = MockHttp([http_200(drifted)])
        result = pstats.fetch(http, season=2025)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("passing_yards", result["error"])
        self.assertIn("shape", result["error"].lower())

    def test_http_exception_is_error(self):
        http = MockHttp([OSError("timed out")])
        result = pstats.fetch(http, season=2025)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("timed out", result["error"])

    def test_never_silently_empty(self):
        # A 200 with headers but zero data rows must NOT look like
        # "nobody played well" -- payload carries row_count for the caller.
        header_only = STATS_CSV.split(b"\n", 1)[0] + b"\n"
        http = MockHttp([http_200(header_only)])
        result = pstats.fetch(http, season=2025)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["payload"]["row_count"], 0)
        self.assertEqual(result["payload"]["stat_lines"], {})


if __name__ == "__main__":
    unittest.main()
