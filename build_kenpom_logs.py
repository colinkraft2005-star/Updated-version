#!/usr/bin/env python3
"""
Build KenPom-derived stats into scouting_hub.db.

Run once from the ucla-basketball directory:
    python3 build_kenpom_logs.py

Steps:
  1. Login to KenPom
  2. Scrape team rankings → kenpom_team_rankings table → batch-update kp_opp_rank
  3. Scrape 362 team roster pages → kenpom_players table (kp_id per player)
  4. Scrape each player's game log → update ortg_kp / usage_kp in player_game_logs

New columns added to player_game_logs:
    kp_opp_rank  INTEGER   — KenPom opponent rank (replaces BartTorvik opp_rank for quality splits)
    ortg_kp      REAL      — KenPom Offensive Rating for this player-game
    usage_kp     REAL      — KenPom Possession % (usage rate) for this player-game

Credentials stored here only; never written to DB or logs.
"""

import http.cookiejar
import re
import sqlite3
import ssl
import time
import urllib.parse
import urllib.request
import warnings

warnings.filterwarnings("ignore")

DB_PATH = "scouting_hub.db"
KP_EMAIL = "Ngeorgeton@gmail.com"
KP_PASSWORD = "Bearcats1"
KP_BASE = "https://kenpom.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
DELAY = 3.5  # seconds between requests

MONTHS = {"Nov": 11, "Dec": 12, "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4}


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def make_opener():
    ctx = ssl._create_unverified_context()
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(cj),
    )


def get_html(opener, url, delay=True):
    if delay:
        time.sleep(DELAY)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": KP_BASE + "/"})
    with opener.open(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def login(opener):
    opener.open(urllib.request.Request(KP_BASE + "/", headers={"User-Agent": UA}))
    payload = urllib.parse.urlencode({
        "email": KP_EMAIL, "password": KP_PASSWORD, "submit": "Login!",
    }).encode()
    req = urllib.request.Request(
        KP_BASE + "/handlers/login_handler.php",
        data=payload,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": KP_BASE + "/"},
    )
    opener.open(req, timeout=15)
    # Verify login by checking a gated page
    time.sleep(2)
    html = get_html(opener, KP_BASE + "/team.php?team=Duke", delay=False)
    if "subscribe" in html.lower() and len(html) < 40000:
        raise RuntimeError("KenPom login failed — check credentials")
    print("  KenPom login OK")


# ─── Parsing helpers ──────────────────────────────────────────────────────────

def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def _safe_int(s):
    try:
        return int(str(s).strip())
    except Exception:
        return None


def _safe_float(s):
    try:
        return float(str(s).strip())
    except Exception:
        return None


def _iso_date(date_str):
    """'Nov 3' or 'Mar 18' → '2025-11-03' / '2026-03-18'."""
    parts = date_str.strip().split()
    if len(parts) != 2:
        return None
    month = MONTHS.get(parts[0])
    if not month:
        return None
    day = _safe_int(parts[1])
    if not day:
        return None
    year = 2026 if month <= 4 else 2025
    return f"{year}-{month:02d}-{day:02d}"


# ─── Step 1: KenPom team rankings ─────────────────────────────────────────────

def fetch_kp_team_rankings(opener):
    """Returns list of (kp_name, kp_rank, adj_em, adj_o, adj_d, adj_tempo)."""
    html = get_html(opener, KP_BASE + "/index.php")
    table_m = re.search(
        r'<table[^>]*id=["\']ratings-table["\'][^>]*>(.*?)</table>', html, re.DOTALL | re.I
    )
    if not table_m:
        raise RuntimeError("KenPom ratings table not found")

    teams = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL):
        kp_link = re.search(r"team\.php\?team=([^\"']+)", tr)
        if not kp_link:
            continue
        kp_name = urllib.parse.unquote_plus(kp_link.group(1))
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
        vals = [strip_tags(c) for c in cells]
        if len(vals) < 8:
            continue
        rank = _safe_int(vals[0])
        adj_em = _safe_float(vals[4])
        adj_o = _safe_float(vals[5])
        adj_d = _safe_float(vals[7])
        adj_tempo = _safe_float(vals[9]) if len(vals) > 9 else None
        if rank and kp_name:
            teams.append((kp_name, rank, adj_em, adj_o, adj_d, adj_tempo))
    return teams


def fuzzy_match_kp_to_espn(kp_name, espn_name_map):
    """Match KenPom team name to ESPN team ID via name overlap."""
    kp_l = kp_name.lower().replace(".", "").replace("-", " ").replace("'", "")
    # Direct
    for espn_name, espn_id in espn_name_map.items():
        en_l = espn_name.lower()
        if kp_l in en_l or en_l.startswith(kp_l):
            return espn_id
    # Token overlap
    kp_tokens = set(kp_l.split())
    best_id, best_score = None, 0
    for espn_name, espn_id in espn_name_map.items():
        en_tokens = set(espn_name.lower().replace(".", "").split())
        score = len(kp_tokens & en_tokens)
        if score > best_score:
            best_score, best_id = score, espn_id
    return best_id if best_score >= 1 else None


def build_team_rankings(conn, opener):
    print("Step 1: KenPom team rankings...")
    conn.execute("""CREATE TABLE IF NOT EXISTS kenpom_team_rankings (
        kp_name      TEXT PRIMARY KEY,
        kp_rank      INTEGER,
        adj_em       REAL,
        adj_o        REAL,
        adj_d        REAL,
        adj_tempo    REAL,
        espn_id      TEXT
    )""")

    espn_rows = conn.execute("SELECT espn_id, espn_name FROM team_rankings").fetchall()
    espn_name_map = {r[1]: r[0] for r in espn_rows}

    teams = fetch_kp_team_rankings(opener)
    matched = 0
    for kp_name, rank, adj_em, adj_o, adj_d, adj_tempo in teams:
        espn_id = fuzzy_match_kp_to_espn(kp_name, espn_name_map)
        if espn_id:
            matched += 1
        conn.execute("""INSERT OR REPLACE INTO kenpom_team_rankings
            (kp_name, kp_rank, adj_em, adj_o, adj_d, adj_tempo, espn_id)
            VALUES (?,?,?,?,?,?,?)""",
            (kp_name, rank, adj_em, adj_o, adj_d, adj_tempo, espn_id))
    conn.commit()
    print(f"  {matched}/{len(teams)} KenPom teams matched to ESPN IDs")

    # Migrate player_game_logs: add kp_opp_rank column
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(player_game_logs)").fetchall()]
    for col, typ in [("kp_opp_rank", "INTEGER"), ("ortg_kp", "REAL"), ("usage_kp", "REAL")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE player_game_logs ADD COLUMN {col} {typ}")
    conn.commit()

    # Build name-based lookup: normalize kp_name → kp_rank
    kp_rows = conn.execute("SELECT kp_name, kp_rank FROM kenpom_team_rankings").fetchall()
    def _normalize(s):
        return s.lower().replace(".", "").replace("-", " ").replace("'", "").replace("&", "and").strip()
    kp_lookup = {_normalize(r[0]): r[1] for r in kp_rows}

    # ESPN opponent names → KenPom rank via name fuzzy match
    opp_rows = conn.execute(
        "SELECT DISTINCT opponent_espn_id, opponent_name FROM player_game_logs"
    ).fetchall()

    espn_to_kp_rank = {}
    for oid, oname in opp_rows:
        if not oname:
            continue
        n = _normalize(oname)
        # Strip mascot words ESPN appends (e.g. "Arizona Wildcats" → try "arizona wildcats", then "arizona")
        if n in kp_lookup:
            espn_to_kp_rank[oid] = kp_lookup[n]
            continue
        # Token overlap: pick kp team with most shared tokens
        n_tokens = set(n.split())
        best, best_score = None, 0
        for kp_n, kp_rank in kp_lookup.items():
            score = len(n_tokens & set(kp_n.split()))
            if score > best_score:
                best_score, best = kp_rank, kp_n
        if best_score >= 1:
            espn_to_kp_rank[oid] = best

    conn.executemany(
        "UPDATE player_game_logs SET kp_opp_rank = ? WHERE opponent_espn_id = ? AND kp_opp_rank IS NULL",
        [(rank, oid) for oid, rank in espn_to_kp_rank.items()]
    )
    # Hardcoded overrides: ESPN abbreviated names that don't token-match KenPom names
    _overrides = {
        "UConn Huskies":            9,
        "UAlbany Great Danes":      322,
        "UIC Flames":               110,
        "Pennsylvania Quakers":     156,
        "SIU Edwardsville Cougars": 258,
    }
    conn.executemany(
        "UPDATE player_game_logs SET kp_opp_rank = ? WHERE opponent_name = ? AND kp_opp_rank IS NULL",
        list(_overrides.items())
    )
    conn.commit()
    updated = conn.execute("SELECT COUNT(*) FROM player_game_logs WHERE kp_opp_rank IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM player_game_logs").fetchone()[0]
    print(f"  kp_opp_rank populated: {updated:,}/{total:,} rows")


# ─── Step 2: KenPom player ID discovery ───────────────────────────────────────

def discover_player_ids(conn, opener):
    print("\nStep 2: Discovering KenPom player IDs from team pages...")
    conn.execute("""CREATE TABLE IF NOT EXISTS kenpom_players (
        kp_id        INTEGER PRIMARY KEY,
        kp_name      TEXT,
        kp_team      TEXT,
        espn_team_id TEXT,
        fetched      INTEGER DEFAULT 0
    )""")
    conn.commit()

    already = conn.execute("SELECT COUNT(*) FROM kenpom_players").fetchone()[0]
    if already > 0:
        print(f"  {already} players already in kenpom_players, skipping discovery")
        return

    # Collect all (kp_team_url, espn_team_id) pairs
    kp_teams = conn.execute(
        "SELECT kp_name, espn_id FROM kenpom_team_rankings WHERE espn_id IS NOT NULL"
    ).fetchall()

    total_found = 0
    for i, (kp_name, espn_id) in enumerate(kp_teams):
        team_url = KP_BASE + "/team.php?team=" + urllib.parse.quote_plus(kp_name)
        try:
            html = get_html(opener, team_url)
        except Exception as e:
            print(f"  [{i+1}/{len(kp_teams)}] {kp_name}: error {e}")
            continue

        player_links = re.findall(
            r"href=['\"]player\.php\?p=(\d+)['\"][^>]*><b?>(.*?)</b?>?</a>",
            html, re.I
        )
        # Also catch non-bold links
        player_links += re.findall(
            r"href=['\"]player\.php\?p=(\d+)['\"][^>]*>([^<]{2,40})</a>",
            html, re.I
        )
        seen = set()
        for kp_id, raw_name in player_links:
            kp_id = int(kp_id)
            name = strip_tags(raw_name).strip()
            if kp_id in seen or not name:
                continue
            seen.add(kp_id)
            conn.execute("""INSERT OR IGNORE INTO kenpom_players
                (kp_id, kp_name, kp_team, espn_team_id) VALUES (?,?,?,?)""",
                (kp_id, name, kp_name, espn_id))
            total_found += 1

        if (i + 1) % 50 == 0:
            conn.commit()
            print(f"  {i+1}/{len(kp_teams)} team pages — {total_found} players found")

    conn.commit()
    print(f"  Done. {total_found} total players discovered.")


# ─── Step 3: Parse player game logs ───────────────────────────────────────────

DATE_PAT = re.compile(r"^(Nov|Dec|Jan|Feb|Mar|Apr)\s+\d{1,2}$")
RANK_PAT = re.compile(r"^\d{1,3}$")


def parse_player_game_logs(html):
    """
    Returns list of dicts: {game_date, kp_opp_rank, ortg_kp, usage_kp}
    Handles KenPom's multiple-games-per-<tr> layout.
    """
    games = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL | re.I):
        ths = re.findall(r"<th[^>]*>(.*?)</th>", table, re.DOTALL | re.I)
        col_names = [strip_tags(h) for h in ths]
        if "Opponent" not in col_names or "ORtg" not in col_names:
            continue

        all_tds = re.findall(r"<td[^>]*>(.*?)</td>", table, re.DOTALL | re.I)
        vals = [strip_tags(c).replace("&#149;", "*") for c in all_tds]

        # Walk through cells, detect game starts by date pattern
        i = 0
        while i < len(vals) - 12:
            if DATE_PAT.match(vals[i]):
                date_str = vals[i]
                iso = _iso_date(date_str)
                if not iso:
                    i += 1
                    continue

                # [date, opp_rank, opp_name, result, ot, site, conf, award, starter,
                #  mp, ortg, poss, pts, 2pt, 3pt, ft, or, dr, a, to, blk, stl, pf]
                try:
                    opp_rank_raw = vals[i + 1]
                    opp_kp_rank = _safe_int(opp_rank_raw) if RANK_PAT.match(opp_rank_raw) else 999
                    ortg = _safe_float(vals[i + 11])
                    usage = _safe_float(vals[i + 12])
                    games.append({
                        "game_date": iso,
                        "kp_opp_rank": opp_kp_rank,
                        "ortg_kp": ortg,
                        "usage_kp": usage,
                    })
                except IndexError:
                    pass
                i += 23  # skip to after PF
            else:
                i += 1

    return games


def scrape_player_game_logs(conn, opener):
    print("\nStep 3: Scraping player game logs from KenPom...")

    # Get players that (a) have rows in player_game_logs and (b) haven't been fetched yet
    players = conn.execute("""
        SELECT kp.kp_id, kp.kp_name, kp.espn_team_id
        FROM kenpom_players kp
        WHERE kp.fetched = 0
          AND EXISTS (
              SELECT 1 FROM player_game_logs p
              WHERE p.team_espn_id = kp.espn_team_id
          )
        ORDER BY kp.kp_id
    """).fetchall()

    total = len(players)
    print(f"  {total} players to fetch (est. {total * DELAY / 60:.0f} min)")

    errors = 0
    for idx, (kp_id, kp_name, espn_team_id) in enumerate(players):
        url = f"{KP_BASE}/player.php?p={kp_id}"
        try:
            html = get_html(opener, url)
        except Exception as e:
            errors += 1
            conn.execute("UPDATE kenpom_players SET fetched = -1 WHERE kp_id = ?", (kp_id,))
            if idx % 100 == 0:
                conn.commit()
            continue

        game_logs = parse_player_game_logs(html)

        for g in game_logs:
            # Match to player_game_logs by (team_espn_id, game_date, kp_name ≈ player_name)
            # KenPom name and ESPN name differ slightly; match by team + date + name similarity
            conn.execute("""
                UPDATE player_game_logs
                SET ortg_kp    = ?,
                    usage_kp   = ?,
                    kp_opp_rank = COALESCE(kp_opp_rank, ?)
                WHERE team_espn_id = ?
                  AND game_date    = ?
                  AND (
                      player_name = ?
                      OR LOWER(REPLACE(player_name, "'", "")) =
                         LOWER(REPLACE(?, "'", ""))
                  )
            """, (
                g["ortg_kp"], g["usage_kp"], g["kp_opp_rank"],
                espn_team_id, g["game_date"],
                kp_name, kp_name,
            ))

        conn.execute("UPDATE kenpom_players SET fetched = 1 WHERE kp_id = ?", (kp_id,))

        if (idx + 1) % 200 == 0:
            conn.commit()
            pct = (idx + 1) * 100 // total
            print(f"  {idx+1}/{total} ({pct}%) — {errors} errors")

    conn.commit()
    ortg_filled = conn.execute("SELECT COUNT(*) FROM player_game_logs WHERE ortg_kp IS NOT NULL").fetchone()[0]
    print(f"\n  Done. ortg_kp populated: {ortg_filled:,} rows. Errors: {errors}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)
    opener = make_opener()

    print("=== build_kenpom_logs.py ===")
    print("Logging into KenPom...")
    login(opener)

    build_team_rankings(conn, opener)
    discover_player_ids(conn, opener)
    scrape_player_game_logs(conn, opener)

    conn.close()
    print("\n=== Complete ===")


if __name__ == "__main__":
    main()
