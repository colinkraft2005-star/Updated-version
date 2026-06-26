#!/usr/bin/env python3
"""
Build the game-log SQLite database for consistent player splits.

Run once from the ucla-basketball directory:
    python3 build_game_logs.py

Takes ~30 min. Re-runs are incremental — already-fetched games are skipped.

Tables written to scouting_hub.db:
    team_rankings    — BartTorvik-derived efficiency rank for every D1 team
    game_team_stats  — per-game team box score totals (both teams) for stat denominators
    player_positions — ESPN player name → Guard / Wing / Big
    player_game_logs — per-player, per-game box score with opp rank, ORB, DRB
    fetched_games    — build bookkeeping
"""

import json
import requests
import sqlite3
import time
import warnings
from datetime import datetime, timedelta, date as date_type

warnings.filterwarnings("ignore")

DB_PATH = "scouting_hub.db"
BART_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://barttorvik.com/",
    "Accept": "application/json",
}

P5_CONFS = {"ACC", "B10", "B12", "BE", "SEC"}

BART_TO_ESPN_OVERRIDES = {
    "USC": "USC Trojans",
    "Miami FL": "Miami Hurricanes",
    "Miami OH": "Miami (OH)",
    "North Carolina": "North Carolina Tar Heels",
    "Florida": "Florida Gators",
    "Indiana": "Indiana Hoosiers",
    "Illinois": "Illinois Fighting Illini",
    "Wisconsin": "Wisconsin Badgers",
    "Michigan": "Michigan Wolverines",
    "Michigan St.": "Michigan State Spartans",
    "Ohio St.": "Ohio State Buckeyes",
    "Penn St.": "Penn State Nittany Lions",
    "Iowa St.": "Iowa State Cyclones",
    "Kansas St.": "Kansas State Wildcats",
    "Oklahoma St.": "Oklahoma State Cowboys",
    "West Virginia": "West Virginia Mountaineers",
    "Texas A&M": "Texas A&M Aggies",
    "Mississippi St.": "Mississippi State Bulldogs",
    "Virginia Tech": "Virginia Tech Hokies",
    "N.C. State": "NC State Wolfpack",
    "Ole Miss": "Ole Miss Rebels",
    "Missouri": "Missouri Tigers",
    "Georgia": "Georgia Bulldogs",
    "Tennessee": "Tennessee Volunteers",
    "Arkansas": "Arkansas Razorbacks",
    "Louisiana St.": "LSU Tigers",
    "UCF": "UCF Knights",
    "UTEP": "UTEP Miners",
    "UAB": "UAB Blazers",
    "UNLV": "UNLV Rebels",
    "VCU": "VCU Rams",
    "SMU": "SMU Mustangs",
    "TCU": "TCU Horned Frogs",
    "BYU": "BYU Cougars",
    "UConn": "UConn Huskies",
    "St. John's": "St. John's Red Storm",
    "St. Mary's": "Saint Mary's Gaels",
    "Saint Mary's": "Saint Mary's Gaels",
    "Ball St.": "Ball State Cardinals",
    "Kent St.": "Kent State Golden Flashes",
    "Akron": "Akron Zips",
    "Toledo": "Toledo Rockets",
    "Ohio": "Ohio Bobcats",
}


# ─────────────────────────── DB INIT / MIGRATE ──────────────────────────────

def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS team_rankings (
            espn_id   TEXT PRIMARY KEY,
            espn_name TEXT,
            bart_name TEXT,
            rank      INTEGER,
            adj_em    REAL
        );

        CREATE TABLE IF NOT EXISTS game_team_stats (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            game_date    TEXT,
            team_espn_id TEXT,
            opp_espn_id  TEXT,
            fgm INTEGER DEFAULT 0, fga INTEGER DEFAULT 0,
            fg3m INTEGER DEFAULT 0, fg3a INTEGER DEFAULT 0,
            ftm INTEGER DEFAULT 0, fta INTEGER DEFAULT 0,
            orb INTEGER DEFAULT 0, drb INTEGER DEFAULT 0,
            ast INTEGER DEFAULT 0, tov INTEGER DEFAULT 0,
            blk INTEGER DEFAULT 0, stl INTEGER DEFAULT 0,
            pf INTEGER DEFAULT 0,  pts INTEGER DEFAULT 0,
            opp_fga  INTEGER DEFAULT 0, opp_fg3a INTEGER DEFAULT 0,
            opp_orb  INTEGER DEFAULT 0, opp_drb  INTEGER DEFAULT 0,
            opp_tov  INTEGER DEFAULT 0,
            possessions REAL DEFAULT 0,
            UNIQUE(game_date, team_espn_id)
        );

        CREATE TABLE IF NOT EXISTS player_positions (
            player_name    TEXT PRIMARY KEY,
            position_group TEXT
        );

        CREATE TABLE IF NOT EXISTS player_game_logs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name      TEXT,
            team_espn_id     TEXT,
            team_name        TEXT,
            opponent_espn_id TEXT,
            opponent_name    TEXT,
            opp_rank         INTEGER DEFAULT 999,
            game_date        TEXT,
            min_played       INTEGER DEFAULT 0,
            pts              INTEGER DEFAULT 0,
            reb              INTEGER DEFAULT 0,
            orb              INTEGER DEFAULT 0,
            drb              INTEGER DEFAULT 0,
            ast              INTEGER DEFAULT 0,
            tov              INTEGER DEFAULT 0,
            stl              INTEGER DEFAULT 0,
            blk              INTEGER DEFAULT 0,
            fg_made          INTEGER DEFAULT 0,
            fg_att           INTEGER DEFAULT 0,
            fg3_made         INTEGER DEFAULT 0,
            fg3_att          INTEGER DEFAULT 0,
            ft_made          INTEGER DEFAULT 0,
            ft_att           INTEGER DEFAULT 0,
            pf               INTEGER DEFAULT 0,
            UNIQUE(player_name, team_espn_id, game_date)
        );

        CREATE TABLE IF NOT EXISTS fetched_games (
            game_id TEXT PRIMARY KEY,
            fetched INTEGER DEFAULT 0
        );
    """)
    # Migration: add orb/drb if upgrading from old schema
    for col in ("orb", "drb"):
        try:
            conn.execute(f"ALTER TABLE player_game_logs ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass
    conn.commit()


# ─────────────────────── BARTTORVIK RANKINGS + TEAM GAMES ───────────────────

def _norm_date(d):
    """Convert BartTorvik 'M/D/YY' to ISO 'YYYY-MM-DD'."""
    try:
        return datetime.strptime(d, "%m/%d/%y").strftime("%Y-%m-%d")
    except ValueError:
        return d


def _utc_to_pacific_date(utc_str):
    """
    Convert ESPN UTC datetime string to US/Pacific calendar date (UTC-8).
    ESPN returns timestamps like '2025-11-04T03:30Z' for games played Nov 3 at 7:30pm PST.
    Any UTC hour < 8 means the game was on the previous calendar day in Pacific time.
    """
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


def build_barttorvik_data(conn, espn_id_map):
    """
    Fetch BartTorvik game data. Returns team rankings dict AND populates
    game_team_stats with per-game team box scores (used as stat denominators).
    espn_id_map: {bart_name: espn_id}
    """
    print("Step 1: Fetching BartTorvik game data...")
    r = requests.get(
        "https://barttorvik.com/getgamestats.php?year=2026&json=1",
        headers=BART_HEADERS, verify=False, timeout=60,
    )
    data = r.json()

    team_em_sum, team_em_cnt = {}, {}
    team_game_rows = []

    for row in data:
        team_bt = str(row[2])
        try:
            em = float(row[7]) - float(row[8])
        except (TypeError, ValueError, IndexError):
            continue
        team_em_sum[team_bt] = team_em_sum.get(team_bt, 0.0) + em
        team_em_cnt[team_bt] = team_em_cnt.get(team_bt, 0) + 1

        # Parse embedded JSON box score (index 29)
        try:
            blob = json.loads(row[29]) if isinstance(row[29], str) else row[29]
            if not blob or len(blob) < 34:
                continue
            date_iso = _norm_date(str(blob[0]))
            t1_bt = str(blob[2])
            t2_bt = str(blob[3])
            t1_id = espn_id_map.get(t1_bt)
            t2_id = espn_id_map.get(t2_bt)
            if not t1_id or not t2_id:
                continue
            poss = float(blob[34]) if len(blob) > 34 and blob[34] else 0

            # Team 1 stats (indices 4-18), Team 2 stats (indices 19-33)
            def gi(lst, i):
                try: return int(lst[i]) if lst[i] is not None else 0
                except: return 0

            team_game_rows.append((
                date_iso, t1_id, t2_id,
                gi(blob,4), gi(blob,5), gi(blob,6), gi(blob,7),   # fgm,fga,fg3m,fg3a
                gi(blob,8), gi(blob,9), gi(blob,10), gi(blob,11),  # ftm,fta,orb,drb
                gi(blob,13), gi(blob,14), gi(blob,15), gi(blob,16), gi(blob,17), gi(blob,18),  # ast,tov,blk,stl,pf,pts
                gi(blob,20), gi(blob,22),  # opp_fga, opp_fg3a
                gi(blob,25), gi(blob,26), gi(blob,29),  # opp_orb, opp_drb, opp_tov
                poss,
            ))
            team_game_rows.append((
                date_iso, t2_id, t1_id,
                gi(blob,19), gi(blob,20), gi(blob,21), gi(blob,22),
                gi(blob,23), gi(blob,24), gi(blob,25), gi(blob,26),
                gi(blob,28), gi(blob,29), gi(blob,30), gi(blob,31), gi(blob,32), gi(blob,33),
                gi(blob,5), gi(blob,7),    # opp_fga, opp_fg3a
                gi(blob,10), gi(blob,11), gi(blob,14),  # opp_orb, opp_drb, opp_tov
                poss,
            ))
        except Exception:
            continue

    # Build rankings
    ranked = sorted(
        [(t, team_em_sum[t] / team_em_cnt[t]) for t in team_em_sum],
        key=lambda x: x[1], reverse=True,
    )
    bart_rankings = {team: (i + 1, em) for i, (team, em) in enumerate(ranked)}
    print(f"  Ranked {len(bart_rankings)} teams.")

    # Insert team game stats
    conn.executemany("""
        INSERT OR REPLACE INTO game_team_stats
        (game_date, team_espn_id, opp_espn_id,
         fgm, fga, fg3m, fg3a, ftm, fta, orb, drb,
         ast, tov, blk, stl, pf, pts,
         opp_fga, opp_fg3a, opp_orb, opp_drb, opp_tov, possessions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, team_game_rows)
    conn.commit()
    print(f"  Stored {len(team_game_rows)} team-game rows in game_team_stats.")

    return bart_rankings


# ─────────────────────── ESPN TEAM MAP ──────────────────────────────────────

def fetch_espn_teams():
    print("Step 2: Fetching ESPN team list...")
    r = requests.get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams?limit=400",
        timeout=15,
    )
    d = r.json()
    teams = d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    return {t["team"]["id"]: t["team"]["displayName"] for t in teams}


def build_name_to_espn_id(bart_rankings, espn_teams):
    """Returns {bart_name: espn_id} mapping."""
    espn_name_to_id = {v: k for k, v in espn_teams.items()}
    result = {}
    for bart_name in bart_rankings:
        espn_id = None
        override = BART_TO_ESPN_OVERRIDES.get(bart_name)
        if override:
            for espn_name, eid in espn_name_to_id.items():
                if override.lower() in espn_name.lower():
                    espn_id = eid
                    break
        if espn_id is None and bart_name in espn_name_to_id:
            espn_id = espn_name_to_id[bart_name]
        if espn_id is None:
            bl = bart_name.lower().replace(".", "").replace("-", " ")
            for espn_name, eid in espn_name_to_id.items():
                el = espn_name.lower().replace(".", "").replace("-", " ")
                if el.startswith(bl + " ") or el == bl:
                    espn_id = eid
                    break
        if espn_id is None:
            first = bart_name.split()[0].lower().replace(".", "")
            for espn_name, eid in espn_name_to_id.items():
                if espn_name.lower().startswith(first):
                    espn_id = eid
                    break
        if espn_id:
            result[bart_name] = espn_id
    return result


def build_espn_rank_lookup(bart_rankings, name_to_espn_id):
    return {name_to_espn_id[b]: rank for b, (rank, _) in bart_rankings.items() if b in name_to_espn_id}


# ─────────────────────── POSITIONS ──────────────────────────────────────────

def fetch_all_positions(conn, espn_teams):
    """Fetch ESPN roster positions for all teams. Store Guard/Wing/Big."""
    print("Step 3: Fetching player positions from ESPN rosters...")
    pos_map = {"G": "Guard", "F": "Wing", "C": "Big"}
    rows = []
    for i, team_id in enumerate(espn_teams):
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
                f"mens-college-basketball/teams/{team_id}/roster?season=2026",
                timeout=15,
            )
            if r.status_code == 200:
                for athlete in r.json().get("athletes", []):
                    name = athlete.get("displayName", "")
                    abbr = athlete.get("position", {}).get("abbreviation", "F")
                    group = pos_map.get(abbr, "Wing")
                    if name:
                        rows.append((name, group))
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(espn_teams)} rosters scanned")
        time.sleep(0.12)

    conn.executemany(
        "INSERT OR REPLACE INTO player_positions (player_name, position_group) VALUES (?,?)",
        rows,
    )
    conn.commit()
    print(f"  Stored {len(rows)} player positions.")


# ─────────────────────── GAME ID COLLECTION ─────────────────────────────────

def collect_game_ids(espn_teams):
    print(f"Step 4: Collecting game IDs from {len(espn_teams)} team schedules...")
    game_ids = set()
    for i, team_id in enumerate(espn_teams):
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
                f"mens-college-basketball/teams/{team_id}/schedule?season=2026",
                timeout=15,
            )
            if r.status_code == 200:
                for event in r.json().get("events", []):
                    game_ids.add(event["id"])
        except Exception:
            pass
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(espn_teams)} teams — {len(game_ids)} games so far")
        time.sleep(0.15)
    print(f"  Found {len(game_ids)} unique games.")
    return game_ids


# ─────────────────────── BOX SCORE FETCH ────────────────────────────────────

def _parse_ma(s):
    parts = str(s).split("-")
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return 0, 0


def _si(s):
    try:
        return int(str(s).split(":")[0])
    except (ValueError, TypeError):
        return 0


def fetch_box_score(game_id):
    r = requests.get(
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
        f"mens-college-basketball/summary?event={game_id}",
        timeout=15,
    )
    if r.status_code != 200:
        return None
    d = r.json()
    game_date = _utc_to_pacific_date(
        d.get("header", {}).get("competitions", [{}])[0].get("date", "")
    )
    competitors = (
        d.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    )
    team_id_to_name = {c["team"]["id"]: c["team"]["displayName"] for c in competitors}
    team_ids = list(team_id_to_name.keys())

    rows = []
    for team_section in d.get("boxscore", {}).get("players", []):
        team_id   = team_section.get("team", {}).get("id")
        team_name = team_section.get("team", {}).get("displayName", "")
        opp_id    = next((tid for tid in team_ids if tid != team_id), None)
        opp_name  = team_id_to_name.get(opp_id, "")

        for stat_group in team_section.get("statistics", []):
            labels = stat_group.get("labels", [])
            idx = {lbl: i for i, lbl in enumerate(labels)}

            for athlete in stat_group.get("athletes", []):
                stats = athlete.get("stats", [])
                if not stats:
                    continue
                mp = _si(stats[idx["MIN"]]) if "MIN" in idx else 0
                if mp == 0:
                    continue

                player_name = athlete.get("athlete", {}).get("displayName", "")
                fg_m,  fg_a  = _parse_ma(stats[idx["FG"]])  if "FG"  in idx else (0, 0)
                fg3_m, fg3_a = _parse_ma(stats[idx["3PT"]]) if "3PT" in idx else (0, 0)
                ft_m,  ft_a  = _parse_ma(stats[idx["FT"]])  if "FT"  in idx else (0, 0)
                orb = _si(stats[idx["OREB"]]) if "OREB" in idx else 0
                drb = _si(stats[idx["DREB"]]) if "DREB" in idx else 0
                reb = _si(stats[idx["REB"]])  if "REB"  in idx else (orb + drb)

                rows.append({
                    "player_name":      player_name,
                    "team_espn_id":     team_id,
                    "team_name":        team_name,
                    "opponent_espn_id": opp_id,
                    "opponent_name":    opp_name,
                    "game_date":        game_date,
                    "min_played":       mp,
                    "pts":  _si(stats[idx["PTS"]]) if "PTS" in idx else 0,
                    "reb":  reb,
                    "orb":  orb,
                    "drb":  drb,
                    "ast":  _si(stats[idx["AST"]]) if "AST" in idx else 0,
                    "tov":  _si(stats[idx["TO"]])  if "TO"  in idx else 0,
                    "stl":  _si(stats[idx["STL"]]) if "STL" in idx else 0,
                    "blk":  _si(stats[idx["BLK"]]) if "BLK" in idx else 0,
                    "fg_made":  fg_m,  "fg_att":  fg_a,
                    "fg3_made": fg3_m, "fg3_att": fg3_a,
                    "ft_made":  ft_m,  "ft_att":  ft_a,
                    "pf": _si(stats[idx["PF"]]) if "PF" in idx else 0,
                })
    return rows or None


# ─────────────────────── MAIN ────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)

    # 1. ESPN teams
    espn_teams = fetch_espn_teams()

    # 2. Build bart→espn name map (needed before BartTorvik game parse)
    # Use a stub rankings dict first for the name map, then rebuild properly
    # We need rankings to build the map, and the map to build rankings. Bootstrap:
    # First pass: get just rankings without team game stats to build the map
    print("Step 1a: Quick BartTorvik rank bootstrap for name matching...")
    r_bt = requests.get(
        "https://barttorvik.com/getgamestats.php?year=2026&json=1",
        headers=BART_HEADERS, verify=False, timeout=60,
    )
    bt_data = r_bt.json()
    em_sum, em_cnt = {}, {}
    for row in bt_data:
        t = str(row[2])
        try:
            em = float(row[7]) - float(row[8])
            em_sum[t] = em_sum.get(t, 0.0) + em
            em_cnt[t] = em_cnt.get(t, 0) + 1
        except Exception:
            continue
    stub_rankings = {t: (i+1, em_sum[t]/em_cnt[t])
                     for i, (t, _) in enumerate(sorted(em_sum.items(), key=lambda x: em_sum[x[0]]/em_cnt[x[0]], reverse=True))}
    name_to_espn = build_name_to_espn_id(stub_rankings, espn_teams)
    espn_id_to_rank = build_espn_rank_lookup(stub_rankings, name_to_espn)

    # Store team rankings
    conn.execute("DELETE FROM team_rankings")
    for bart_name, espn_id in name_to_espn.items():
        rank, em = stub_rankings.get(bart_name, (999, 0))
        conn.execute(
            "INSERT OR REPLACE INTO team_rankings (espn_id, espn_name, bart_name, rank, adj_em) VALUES (?,?,?,?,?)",
            (espn_id, espn_teams.get(espn_id, ""), bart_name, rank, em),
        )
    conn.commit()
    print(f"  Matched {len(name_to_espn)}/{len(stub_rankings)} teams.")

    # 3. Parse BartTorvik game data → game_team_stats
    print("Step 1b: Parsing BartTorvik team box scores into game_team_stats...")
    team_game_rows = []
    for row in bt_data:
        try:
            blob = json.loads(row[29]) if isinstance(row[29], str) else row[29]
            if not blob or len(blob) < 34:
                continue
            date_iso = _norm_date(str(blob[0]))
            t1_id = name_to_espn.get(str(blob[2]))
            t2_id = name_to_espn.get(str(blob[3]))
            if not t1_id or not t2_id:
                continue
            poss = float(blob[34]) if len(blob) > 34 and blob[34] else 0

            def gi(lst, i):
                try: return int(lst[i]) if lst[i] is not None else 0
                except: return 0

            for team_id, opp_id, off, oth in [(t1_id, t2_id, 4, 19), (t2_id, t1_id, 19, 4)]:
                team_game_rows.append((
                    date_iso, team_id, opp_id,
                    gi(blob,off+0), gi(blob,off+1), gi(blob,off+2), gi(blob,off+3),
                    gi(blob,off+4), gi(blob,off+5), gi(blob,off+6), gi(blob,off+7),
                    gi(blob,off+9), gi(blob,off+10), gi(blob,off+11), gi(blob,off+12),
                    gi(blob,off+13), gi(blob,off+14),
                    gi(blob,oth+1), gi(blob,oth+3),
                    gi(blob,oth+6), gi(blob,oth+7), gi(blob,oth+10),
                    poss,
                ))
        except Exception:
            continue

    conn.executemany("""
        INSERT OR REPLACE INTO game_team_stats
        (game_date, team_espn_id, opp_espn_id,
         fgm, fga, fg3m, fg3a, ftm, fta, orb, drb,
         ast, tov, blk, stl, pf, pts,
         opp_fga, opp_fg3a, opp_orb, opp_drb, opp_tov, possessions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, team_game_rows)
    conn.commit()
    print(f"  Stored {len(team_game_rows)} team-game rows.")

    # 4. Fetch player positions
    fetch_all_positions(conn, espn_teams)

    # 5. Collect game IDs
    game_ids = collect_game_ids(espn_teams)
    for gid in game_ids:
        conn.execute("INSERT OR IGNORE INTO fetched_games (game_id, fetched) VALUES (?,0)", (gid,))
    conn.commit()

    # Reset all games to re-fetch (needed to populate orb/drb)
    needs_reset = conn.execute(
        "SELECT COUNT(*) FROM player_game_logs WHERE orb = 0 AND reb > 0"
    ).fetchone()[0]
    if needs_reset > 0:
        print(f"  Resetting {needs_reset:,} rows need orb/drb — re-fetching all games...")
        conn.execute("UPDATE fetched_games SET fetched = 0")
        conn.commit()

    # 6. Fetch box scores
    rank_cache = {
        row[0]: row[1]
        for row in conn.execute("SELECT espn_id, rank FROM team_rankings").fetchall()
    }
    pending = [r[0] for r in conn.execute(
        "SELECT game_id FROM fetched_games WHERE fetched = 0"
    ).fetchall()]
    total = len(pending)
    print(f"Step 5: Fetching {total} box scores (est. {total*0.25/60:.0f} min)...")

    errors = 0
    for i, game_id in enumerate(pending):
        try:
            player_rows = fetch_box_score(game_id)
            if player_rows:
                for pr in player_rows:
                    opp_rank = rank_cache.get(pr["opponent_espn_id"], 999)
                    conn.execute("""
                        INSERT OR REPLACE INTO player_game_logs
                        (player_name, team_espn_id, team_name, opponent_espn_id, opponent_name,
                         opp_rank, game_date, min_played, pts, reb, orb, drb,
                         ast, tov, stl, blk, fg_made, fg_att, fg3_made, fg3_att,
                         ft_made, ft_att, pf)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        pr["player_name"], pr["team_espn_id"], pr["team_name"],
                        pr["opponent_espn_id"], pr["opponent_name"], opp_rank,
                        pr["game_date"], pr["min_played"], pr["pts"],
                        pr["reb"], pr["orb"], pr["drb"],
                        pr["ast"], pr["tov"], pr["stl"], pr["blk"],
                        pr["fg_made"], pr["fg_att"], pr["fg3_made"], pr["fg3_att"],
                        pr["ft_made"], pr["ft_att"], pr["pf"],
                    ))
            conn.execute("UPDATE fetched_games SET fetched=1 WHERE game_id=?", (game_id,))
        except Exception:
            errors += 1
        if (i + 1) % 200 == 0:
            conn.commit()
            print(f"  {i+1}/{total} ({(i+1)/total*100:.0f}%) — {errors} errors")
        time.sleep(0.25)

    conn.commit()
    conn.close()

    n = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM player_game_logs").fetchone()[0]
    print(f"\nDone. {n:,} player-game rows. Errors skipped: {errors}")


if __name__ == "__main__":
    main()
