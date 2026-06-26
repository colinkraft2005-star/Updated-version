"""
build_synergy_enriched.py

Scrapes Synergy Sports for College Men 2025-26 enriched per-player stats.
Run AFTER build_synergy_playtypes.py has finished (uses same DB).

Tables populated:
  synergy_shots    — shooting splits (FG%, 2P%, 3P%, eFG%, assisted%, blocks, fouls)
  synergy_drives   — ball-handler drive stats (PPP, FG%, pass rate, foul rate)
  synergy_defense  — defensive activity (stops, blocks, live-ball TOs, closeouts)

Play-type stats (Spot-Up, ISO, P&R, etc.) live in synergy_playtypes.
Offensive rebounds are captured as the "Offensive Rebound" play type there.

Rankings are NOT available via direct API calls with the bearer token alone —
they require browser-session context. Not scraped here.

Usage:
    caffeinate -i python3 -u build_synergy_enriched.py 2>&1 | tee /tmp/synergy_enriched.log
    caffeinate -i python3 -u build_synergy_enriched.py test    # Karaban only
"""

import asyncio, json, sqlite3, sys, time, urllib.request, urllib.error
from pathlib import Path

# Re-use auth + HTTP helpers from the play-types scraper
sys.path.insert(0, str(Path(__file__).parent))
from build_synergy_playtypes import (
    login, get_bearer, bearer_holder, get_all_players,
    http_post, http_get,
    LEAGUE, SEASON, COMP_KEY, BASE_API, DB_PATH, DELAY,
)

# ── DB setup ───────────────────────────────────────────────────────────────
def build_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS synergy_shots (
        synergy_id        TEXT PRIMARY KEY,
        player_name       TEXT,
        team_name         TEXT,
        -- volume
        total_shots       INTEGER,
        fg_attempt        INTEGER,
        fg_made           INTEGER,
        fg2_attempt       INTEGER,
        fg2_made          INTEGER,
        fg3_attempt       INTEGER,
        fg3_made          INTEGER,
        -- efficiency
        fg_pct            REAL,
        fg2_pct           REAL,
        fg3_pct           REAL,
        efg_pct           REAL,
        points_scored     INTEGER,
        ppp               REAL,
        -- context
        assist_pct        REAL,   -- % of FGM that were assisted
        shot_foul_rate    REAL,   -- shot fouls drawn per 100 FGA
        block_pct         REAL,   -- % of FGA blocked
        avg_def_distance  REAL,   -- closest defender distance (inches)
        games_played      INTEGER,
        updated           TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_shots_player ON synergy_shots(player_name);
    CREATE INDEX IF NOT EXISTS ix_shots_team   ON synergy_shots(team_name);

    CREATE TABLE IF NOT EXISTS synergy_drives (
        synergy_id        TEXT PRIMARY KEY,
        player_name       TEXT,
        team_name         TEXT,
        -- volume
        total_drives      INTEGER,
        games_played      INTEGER,
        drives_per_game   REAL,
        -- efficiency
        points_scored     INTEGER,
        ppp               REAL,
        fg_attempt        INTEGER,
        fg_made           INTEGER,
        fg_pct            REAL,
        -- outcomes (per-drive rates)
        shot_rate         REAL,   -- % of drives ending in FGA
        pass_rate         REAL,   -- % of drives ending in pass
        foul_rate         REAL,   -- % of drives drawing a foul with FTs
        turnover_rate     REAL,
        -- pass quality
        assist_made       INTEGER,
        assist_attempt    INTEGER,
        updated           TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_drives_player ON synergy_drives(player_name);
    CREATE INDEX IF NOT EXISTS ix_drives_team   ON synergy_drives(team_name);

    CREATE TABLE IF NOT EXISTS synergy_defense (
        synergy_id             TEXT PRIMARY KEY,
        player_name            TEXT,
        team_name              TEXT,
        -- overall defensive load
        total_def_chances      INTEGER,
        live_ball_tos_forced   INTEGER,
        blocks                 INTEGER,
        rotations              INTEGER,
        -- stops by play type
        stopped_drives         INTEGER,
        stopped_picks          INTEGER,
        stopped_isolations     INTEGER,
        stopped_posts          INTEGER,
        -- help-defense usage
        helped_by_stunting     INTEGER,
        helped_by_trapping     INTEGER,
        helped_by_digging      INTEGER,
        helped_by_loading_up   INTEGER,
        -- closeout defense (opponent shots on Karaban's closeouts)
        total_closeouts        INTEGER,
        closeout_fg_attempt    INTEGER,
        closeout_fg_made       INTEGER,
        closeout_fg_pct        REAL,
        closeout_pts_allowed   INTEGER,
        closeout_ppp_allowed   REAL,
        updated                TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_def_player ON synergy_defense(player_name);
    CREATE INDEX IF NOT EXISTS ix_def_team   ON synergy_defense(team_name);
    """)
    conn.commit()

# ── Per-player fetch helpers ───────────────────────────────────────────────
def pct(n, d, decimals=3):
    return round(n / d, decimals) if d else None

def get_shots(pid, team_id, name, team):
    """feedEventReports/shot — all shots where player is the actorplayer."""
    bearer = get_bearer()
    expr = (f"season eq oid({SEASON}) and offensiveteam eq oid({team_id}) "
            f"and actorplayer eq oid({pid}) group actorteam, actorplayer")
    status, data = http_post(f"{BASE_API}/feedEventReports/shot",
                             {"expressions": [expr]}, bearer)
    if status != 200 or not isinstance(data, list) or not data:
        return None
    d = data[0]
    fga   = d.get("fgAttempt", 0)
    fgm   = d.get("fgMade", 0)
    fg2a  = d.get("fg2Attempt", 0)
    fg2m  = d.get("fg2Made", 0)
    fg3a  = d.get("fg3Attempt", 0)
    fg3m  = d.get("fg3Made", 0)
    pts   = d.get("pointsScored", 0)
    shots = d.get("totalShots", 0)
    asst  = d.get("assistMade", 0)
    aop   = d.get("assistOpportunity", 0)
    sf    = d.get("shotFoul", 0)
    blk   = d.get("blockedShot", 0)
    dist  = d.get("closestDefenderDistance", None)
    gp    = d.get("gamesPlayed", 0)
    efg   = round((fgm + 0.5 * fg3m) / fga, 3) if fga else None
    return {
        "synergy_id": pid, "player_name": name, "team_name": team,
        "total_shots": shots,
        "fg_attempt": fga, "fg_made": fgm,
        "fg2_attempt": fg2a, "fg2_made": fg2m,
        "fg3_attempt": fg3a, "fg3_made": fg3m,
        "fg_pct":  pct(fgm, fga),
        "fg2_pct": pct(fg2m, fg2a),
        "fg3_pct": pct(fg3m, fg3a),
        "efg_pct": efg,
        "points_scored": pts,
        "ppp": pct(pts, shots),
        "assist_pct":   pct(asst, fgm),
        "shot_foul_rate": round(100 * sf / fga, 1) if fga else None,
        "block_pct":      pct(blk, fga),
        "avg_def_distance": round(dist, 1) if dist else None,
        "games_played": gp,
        "updated": time.strftime("%Y-%m-%d"),
    }

def get_drives(pid, team_id, name, team):
    """feedEventReports/drive — drives where player is the ball handler."""
    bearer = get_bearer()
    expr = (f"season eq oid({SEASON}) and offensiveteam eq oid({team_id}) "
            f"and (match(eventactors, playerrole eq 'ballHandler' "
            f"and player eq oid({pid})))")
    status, data = http_post(f"{BASE_API}/feedEventReports/drive",
                             {"expressions": [expr]}, bearer)
    if status != 200 or not isinstance(data, list) or not data:
        return None
    d     = data[0]
    total = d.get("totalDrives", 0)
    if total == 0:
        return None
    pts   = d.get("pointsScored", 0)
    fga   = d.get("fgAttempt", 0)
    fgm   = d.get("fgMade", 0)
    pas   = d.get("pass", 0)
    foul  = d.get("foulDrawnWithFt", 0)
    to_   = d.get("turnover", 0)
    asst  = d.get("assistMade", 0)
    aatm  = d.get("assistAttempt", 0)
    gp    = d.get("gamesPlayed", 0)
    return {
        "synergy_id": pid, "player_name": name, "team_name": team,
        "total_drives": total,
        "games_played": gp,
        "drives_per_game": round(total / gp, 1) if gp else None,
        "points_scored": pts,
        "ppp": pct(pts, total),
        "fg_attempt": fga, "fg_made": fgm,
        "fg_pct": pct(fgm, fga),
        "shot_rate":     pct(fga, total),
        "pass_rate":     pct(pas, total),
        "foul_rate":     pct(foul, total),
        "turnover_rate": pct(to_, total),
        "assist_made":    asst,
        "assist_attempt": aatm,
        "updated": time.strftime("%Y-%m-%d"),
    }

def get_defense(pid, team_id, name, team):
    """
    Combines:
      feedEventReports/defensiveCount  — overall defensive activity
      feedEventReports/closeout        — closeout defense stats
    """
    bearer = get_bearer()

    # ── Overall defensive counts ──────────────────────────────────────────
    dc_expr = (f"season eq oid({SEASON}) and defensiveteam eq oid({team_id}) "
               f"and playerdefensiveeventcounts eq oid({pid}) and valid eq true")
    s1, d1 = http_post(f"{BASE_API}/feedEventReports/defensiveCount",
                       {"expressions": [dc_expr]}, bearer)
    dc = d1[0] if s1 == 200 and isinstance(d1, list) and d1 else {}

    # ── Closeout defense ─────────────────────────────────────────────────
    cl_expr = (f"season eq oid({SEASON}) and defensiveteam eq oid({team_id}) "
               f"and actorplayer eq oid({pid}) group actorteam, actorballhandlerdefender")
    s2, d2 = http_post(f"{BASE_API}/feedEventReports/closeout",
                       {"expressions": [cl_expr]}, bearer)
    cl = d2[0] if s2 == 200 and isinstance(d2, list) and d2 else {}

    if not dc and not cl:
        return None

    total_chances = dc.get("totalChances", 0)
    clo_total     = cl.get("totalCloseouts", 0)
    clo_fga       = cl.get("fgAttempt", 0)
    clo_fgm       = cl.get("fgMade", 0)
    clo_pts       = cl.get("pointsScored", 0)

    return {
        "synergy_id": pid, "player_name": name, "team_name": team,
        "total_def_chances": total_chances,
        "live_ball_tos_forced": dc.get("liveBallTurnoversForced", 0),
        "blocks":    dc.get("blocks", 0),
        "rotations": dc.get("rotations", 0),
        "stopped_drives":      dc.get("stoppedDrive", 0),
        "stopped_picks":       dc.get("stoppedPick", 0),
        "stopped_isolations":  dc.get("stoppedIsolation", 0),
        "stopped_posts":       dc.get("stoppedPost", 0),
        "helped_by_stunting":  dc.get("helpedByStunting", 0),
        "helped_by_trapping":  dc.get("helpedByTrapping", 0),
        "helped_by_digging":   dc.get("helpedByDigging", 0),
        "helped_by_loading_up": dc.get("helpedByLoadingUp", 0),
        "total_closeouts":      clo_total,
        "closeout_fg_attempt":  clo_fga,
        "closeout_fg_made":     clo_fgm,
        "closeout_fg_pct":      pct(clo_fgm, clo_fga),
        "closeout_pts_allowed": clo_pts,
        "closeout_ppp_allowed": pct(clo_pts, clo_total),
        "updated": time.strftime("%Y-%m-%d"),
    }

# ── Upsert helpers ─────────────────────────────────────────────────────────
def upsert(conn, table, row: dict):
    cols = list(row.keys())
    vals = list(row.values())
    placeholders = ",".join(["?"] * len(cols))
    col_str = ",".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})",
        vals
    )

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB_PATH)
    build_tables(conn)

    # Already-done sets per table
    done_shots = {r[0] for r in conn.execute(
        "SELECT synergy_id FROM synergy_shots").fetchall()}
    done_drives = {r[0] for r in conn.execute(
        "SELECT synergy_id FROM synergy_drives").fetchall()}
    done_defense = {r[0] for r in conn.execute(
        "SELECT synergy_id FROM synergy_defense").fetchall()}
    print(f"Already stored: {len(done_shots)} shots, "
          f"{len(done_drives)} drives, {len(done_defense)} defense")

    print("\nFetching D1 player list…")
    players = get_all_players()
    print(f"Total D1 players: {len(players)}")

    todo = list(players.items())
    errors = {"shots": 0, "drives": 0, "defense": 0}

    for i, (pid, info) in enumerate(todo):
        name    = info["name"]
        team    = info["team"]
        team_id = info.get("team_id", "")
        if not team_id:
            continue

        # ── Shots ──────────────────────────────────────────────────────
        if pid not in done_shots:
            row = get_shots(pid, team_id, name, team)
            time.sleep(DELAY)
            if row:
                upsert(conn, "synergy_shots", row)
            else:
                errors["shots"] += 1

        # ── Drives ─────────────────────────────────────────────────────
        if pid not in done_drives:
            row = get_drives(pid, team_id, name, team)
            time.sleep(DELAY)
            if row:
                upsert(conn, "synergy_drives", row)
            else:
                errors["drives"] += 1

        # ── Defense ────────────────────────────────────────────────────
        if pid not in done_defense:
            row = get_defense(pid, team_id, name, team)
            time.sleep(DELAY * 2)   # two internal calls
            if row:
                upsert(conn, "synergy_defense", row)
            else:
                errors["defense"] += 1

        if (i + 1) % 50 == 0:
            conn.commit()
            pct_done = 100 * (i + 1) / len(todo)
            n_shots  = conn.execute("SELECT COUNT(*) FROM synergy_shots").fetchone()[0]
            n_drives = conn.execute("SELECT COUNT(*) FROM synergy_drives").fetchone()[0]
            n_def    = conn.execute("SELECT COUNT(*) FROM synergy_defense").fetchone()[0]
            print(f"  {i+1}/{len(todo)} ({pct_done:.1f}%) — "
                  f"shots={n_shots} drives={n_drives} defense={n_def} | "
                  f"errors={errors}")

    conn.commit()
    n_shots  = conn.execute("SELECT COUNT(*) FROM synergy_shots").fetchone()[0]
    n_drives = conn.execute("SELECT COUNT(*) FROM synergy_drives").fetchone()[0]
    n_def    = conn.execute("SELECT COUNT(*) FROM synergy_defense").fetchone()[0]
    print(f"\nDone. shots={n_shots}, drives={n_drives}, defense={n_def}. Errors={errors}")
    conn.close()


# ── Test mode (single player: Karaban) ────────────────────────────────────
def test():
    print("=== TEST MODE: Alex Karaban ===\n")
    tok = asyncio.run(login())
    bearer_holder["token"] = tok
    bearer_holder["expires_at"] = time.time() + 570

    pid     = "636aaf8e12087ae7a21c9caf"
    team_id = "54457dd3300969b132fcfea2"
    name    = "Alex Karaban"
    team    = "Connecticut"

    print("── Shots ──")
    row = get_shots(pid, team_id, name, team)
    if row:
        for k, v in row.items():
            if k not in ("synergy_id","player_name","team_name","updated"):
                print(f"  {k:<25} {v}")
    else:
        print("  No data")

    print("\n── Drives ──")
    row = get_drives(pid, team_id, name, team)
    if row:
        for k, v in row.items():
            if k not in ("synergy_id","player_name","team_name","updated"):
                print(f"  {k:<25} {v}")
    else:
        print("  No data")

    print("\n── Defense ──")
    row = get_defense(pid, team_id, name, team)
    if row:
        for k, v in row.items():
            if k not in ("synergy_id","player_name","team_name","updated"):
                print(f"  {k:<25} {v}")
    else:
        print("  No data")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test()
    else:
        main()
