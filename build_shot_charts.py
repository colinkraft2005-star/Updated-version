"""
build_shot_charts.py
Pulls play-by-play shot location data from ESPN for all games in fetched_games.
Table: shot_chart  (one row per shooting attempt)

Court coordinate system (ESPN):
  x: 0-50  (court width in feet)
  y: 0-94  (full court length)
  Shots at y > 47 are at the far basket — flip to half-court:
    x_norm = 50 - x
    y_norm = 94 - y
"""

import json
import sqlite3
import ssl
import time
import urllib.request
from datetime import timedelta, date as date_type

DB_PATH = "scouting_hub.db"
BASE    = "http://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary"
UA      = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DELAY   = 0.15   # seconds between requests


def build_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shot_chart (
            play_id         TEXT PRIMARY KEY,
            game_id         TEXT,
            game_date       TEXT,
            period          INTEGER,
            clock           TEXT,
            wallclock       TEXT,
            athlete_id      TEXT,
            player_name     TEXT,
            team_id         TEXT,
            shot_type       TEXT,
            scoring_play    INTEGER,
            points_attempted INTEGER,
            score_value     INTEGER,
            coord_x         REAL,
            coord_y         REAL,
            coord_x_norm    REAL,
            coord_y_norm    REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_shot_athlete ON shot_chart(athlete_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_shot_game    ON shot_chart(game_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_shot_team    ON shot_chart(team_id)")
    conn.commit()


def _utc_to_pacific_date(utc_str):
    """Convert ESPN UTC datetime string to US/Pacific calendar date (UTC-8)."""
    if not utc_str or 'T' not in utc_str:
        return utc_str[:10] if utc_str else ""
    try:
        date_str, time_str = utc_str.rstrip('Z').split('T')
        utc_hour = int(time_str[:2])
        if utc_hour < 8:
            d = date_type.fromisoformat(date_str) - timedelta(days=1)
            return str(d)
        return date_str
    except Exception:
        return utc_str[:10]


def already_fetched(conn, game_id):
    return conn.execute(
        "SELECT 1 FROM shot_chart WHERE game_id = ? LIMIT 1", (game_id,)
    ).fetchone() is not None


def parse_player_name(text, shot_type_text):
    """Extract player name from play description text."""
    if not text:
        return None
    # "Alfred Worrell Jr. misses 25-foot three point jumper"
    # "Tim Okojie makes layup"
    for kw in [" misses ", " makes "]:
        idx = text.find(kw)
        if idx > 0:
            return text[:idx].strip()
    return None


def flip_coords(x, y):
    """Normalize to half-court: shots from far end get mirrored."""
    if y is None or x is None:
        return x, y
    if y > 47:
        return round(50 - x, 1), round(94 - y, 1)
    return round(float(x), 1), round(float(y), 1)


def fetch_shots(game_id):
    url = f"{BASE}?event={game_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        r = urllib.request.urlopen(req, context=ctx, timeout=10)
        data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None, []

    # Grab game date from header — convert UTC to US/Pacific calendar date
    game_date = None
    try:
        game_date = _utc_to_pacific_date(data["header"]["competitions"][0]["date"])
    except (KeyError, IndexError):
        pass

    plays = data.get("plays", [])
    shots = []
    for p in plays:
        if not p.get("shootingPlay"):
            continue
        coord = p.get("coordinate") or {}
        x = coord.get("x")
        y = coord.get("y")
        if x is None or y is None:
            continue

        x_norm, y_norm = flip_coords(x, y)

        participants = p.get("participants") or []
        athlete_id = None
        if participants:
            athlete_id = (participants[0].get("athlete") or {}).get("id")

        player_name = parse_player_name(p.get("text", ""), p.get("type", {}).get("text"))

        shots.append((
            p.get("id"),
            game_id,
            game_date,
            (p.get("period") or {}).get("number"),
            (p.get("clock") or {}).get("displayValue"),
            p.get("wallclock"),
            athlete_id,
            player_name,
            (p.get("team") or {}).get("id"),
            (p.get("type") or {}).get("text"),
            int(bool(p.get("scoringPlay"))),
            p.get("pointsAttempted"),
            p.get("scoreValue"),
            float(x),
            float(y),
            x_norm,
            y_norm,
        ))
    return game_date, shots


def main():
    conn = sqlite3.connect(DB_PATH)
    build_table(conn)

    game_ids = [r[0] for r in conn.execute(
        "SELECT game_id FROM fetched_games ORDER BY game_id"
    ).fetchall()]

    already_done = conn.execute("SELECT COUNT(DISTINCT game_id) FROM shot_chart").fetchone()[0]
    print(f"Total games: {len(game_ids):,} | Already done: {already_done:,}")

    errors = 0
    total_shots = conn.execute("SELECT COUNT(*) FROM shot_chart").fetchone()[0]

    for idx, game_id in enumerate(game_ids):
        if already_fetched(conn, game_id):
            continue

        game_date, shots = fetch_shots(game_id)

        if shots is None:
            errors += 1
        else:
            if shots:
                conn.executemany(
                    "INSERT OR IGNORE INTO shot_chart VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    shots
                )
                conn.commit()
                total_shots += len(shots)

        time.sleep(DELAY)

        if (idx + 1) % 500 == 0:
            done = conn.execute("SELECT COUNT(DISTINCT game_id) FROM shot_chart").fetchone()[0]
            pct = 100 * (idx + 1) / len(game_ids)
            print(f"  {idx+1}/{len(game_ids)} ({pct:.0f}%) — {total_shots:,} shots — {errors} errors")

    done = conn.execute("SELECT COUNT(DISTINCT game_id) FROM shot_chart").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM shot_chart").fetchone()[0]
    print(f"\nDone. {done:,} games, {total:,} total shots. Errors: {errors}")
    conn.close()


if __name__ == "__main__":
    main()
