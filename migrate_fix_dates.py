#!/usr/bin/env python3
"""
migrate_fix_dates.py

Fixes the UTC→Pacific date bug in player_game_logs and shot_chart.

Root cause: ESPN API returns timestamps in UTC (e.g. "2025-11-04T03:30Z")
for games played Nov 3 at 7:30pm PST. The old code took [:10] giving "2025-11-04",
while game_team_stats (built from BartTorvik) correctly stored "2025-11-03".
This caused the JOIN to fail for all evening games, zeroing out all rate stats
(USG%, AST%, OREB%, DREB%, BLK%, STL%) for ~50% of games.

Steps:
  1. Drop and rebuild player_game_logs by re-fetching ESPN box scores (now with UTC fix)
  2. Fix shot_chart.game_date in place using the wallclock column (also UTC)
  3. Re-apply KenPom rank/ortg/usage from build_kenpom_logs.py

Run:
    caffeinate -i python3 -u migrate_fix_dates.py 2>&1 | tee /tmp/migrate.log
"""

import sqlite3
import subprocess
import sys

DB_PATH = "scouting_hub.db"


def fix_shot_chart_dates(conn):
    """Update shot_chart.game_date from UTC to Pacific using wallclock column."""
    print("Fixing shot_chart dates via wallclock...")
    # wallclock is stored as "2025-11-04T04:02:26Z" (UTC)
    # DATETIME(wallclock, '-8 hours') converts to Pacific
    # DATE(...) extracts the local calendar date
    cur = conn.execute("""
        UPDATE shot_chart
        SET game_date = DATE(DATETIME(wallclock, '-8 hours'))
        WHERE wallclock IS NOT NULL
          AND wallclock != ''
    """)
    conn.commit()
    print(f"  Updated {cur.rowcount:,} shot_chart rows")


def drop_player_game_logs(conn):
    """Drop player_game_logs and reset fetched_games so build_game_logs.py re-fetches all."""
    print("Dropping player_game_logs and resetting fetched_games flags...")
    conn.execute("DROP TABLE IF EXISTS player_game_logs")
    conn.execute("UPDATE fetched_games SET fetched = 0")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM fetched_games").fetchone()[0]
    print(f"  Dropped player_game_logs. Reset {n:,} game IDs to fetched=0.")


def main():
    conn = sqlite3.connect(DB_PATH)

    # Step 1: fix shot_chart dates in place
    fix_shot_chart_dates(conn)

    # Step 2: drop player_game_logs so build_game_logs re-fetches with correct dates
    drop_player_game_logs(conn)
    conn.close()

    # Step 3: rebuild player_game_logs (fetch_espn_box_scores step only)
    print("\nRebuilding player_game_logs via build_game_logs.py ...")
    print("This re-fetches all ESPN box scores. Takes ~30-45 min.")
    print("Do NOT interrupt — run with: caffeinate -i python3 -u migrate_fix_dates.py\n")
    ret = subprocess.call([sys.executable, "build_game_logs.py"])
    if ret != 0:
        print("build_game_logs.py exited with error.")
        sys.exit(1)

    # Step 4: re-apply KenPom ranks
    print("\nRe-applying KenPom data via build_kenpom_logs.py ...")
    ret = subprocess.call([sys.executable, "build_kenpom_logs.py"])
    if ret != 0:
        print("build_kenpom_logs.py exited with error.")
        sys.exit(1)

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
