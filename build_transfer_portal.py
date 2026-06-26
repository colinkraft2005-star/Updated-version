"""
build_transfer_portal.py
Pulls transfer portal data from srating.io and stores it in scouting_hub.db.
Table: transfer_portal  (one row per portal player, 2026 season)
"""

import json
import re
import sqlite3
import ssl
import urllib.request

DB_PATH    = "scouting_hub.db"
CBB_ORG_ID = "f1c37c98-3b4c-11ef-94bc-2a93761010b8"
D1_DIV_ID  = "bf602dc4-3b4a-11ef-94bc-2a93761010b8"
SEASON     = 2026
UA         = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def get_tokens():
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        "https://srating.io/cbb/ranking?view=transfer",
        headers={"User-Agent": UA},
    )
    html = urllib.request.urlopen(req, context=ctx, timeout=15).read().decode("utf-8", "ignore")
    kryptos = re.search(r'kryptos\\":\\"([0-9a-f-]{36})\\"', html)
    secret  = re.search(r'secret_id\\":\\"([0-9a-f-]{36})\\"', html)
    if not kryptos:
        raise RuntimeError("Could not find kryptos token in page HTML")
    return kryptos.group(1), (secret.group(1) if secret else None)


def fetch_transfer_data(kryptos, secret):
    ctx = ssl.create_default_context()
    payload = json.dumps({
        "class": "ranking",
        "function": "load",
        "arguments": {
            "organization_id": CBB_ORG_ID,
            "division_id": D1_DIV_ID,
            "season": SEASON,
            "fxn": "getTransferRanking",
        },
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "X-KRYPTOS-ID": kryptos,
    }
    if secret:
        headers["X-SECRET-ID"] = secret
    req = urllib.request.Request(
        "https://srating.io/api", data=payload, headers=headers, method="POST"
    )
    r = urllib.request.urlopen(req, context=ctx, timeout=30)
    return json.loads(r.read().decode("utf-8", "ignore"))


def build_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transfer_portal (
            player_id           TEXT,
            first_name          TEXT,
            last_name           TEXT,
            position            TEXT,
            height              TEXT,
            from_team           TEXT,
            to_team             TEXT,
            committed           INTEGER,
            rank                INTEGER,
            elo                 INTEGER,
            games               INTEGER,
            mpg                 REAL,
            ppg                 REAL,
            rpg                 REAL,
            apg                 REAL,
            spg                 REAL,
            bpg                 REAL,
            topg                REAL,
            fg_pct              REAL,
            two_pt_pct          REAL,
            three_pt_pct        REAL,
            ft_pct              REAL,
            ts_pct              REAL,
            efg_pct             REAL,
            usg_pct             REAL,
            oreb_pct            REAL,
            dreb_pct            REAL,
            ast_pct             REAL,
            stl_pct             REAL,
            blk_pct             REAL,
            tov_pct             REAL,
            ortg                REAL,
            drtg                REAL,
            per                 REAL,
            ert                 REAL,
            plus_minus          REAL,
            season              INTEGER,
            updated_at          TEXT,
            PRIMARY KEY (player_id)
        )
    """)
    conn.commit()


def load_records(conn, data):
    rows = []
    for rec in data.values():
        p = rec.get("player") or {}
        rows.append((
            rec.get("player_id"),
            p.get("first_name"),
            p.get("last_name"),
            p.get("position"),
            p.get("height"),
            rec.get("team_name"),
            rec.get("committed_team_name"),
            int(rec.get("committed") or 0),
            rec.get("rank"),
            rec.get("elo"),
            rec.get("games"),
            rec.get("minutes_per_game"),
            rec.get("points_per_game"),
            rec.get("total_rebounds_per_game"),
            rec.get("assists_per_game"),
            rec.get("steals_per_game"),
            rec.get("blocks_per_game"),
            rec.get("turnovers_per_game"),
            rec.get("field_goal_percentage"),
            rec.get("two_point_field_goal_percentage"),
            rec.get("three_point_field_goal_percentage"),
            rec.get("free_throw_percentage"),
            rec.get("true_shooting_percentage"),
            rec.get("effective_field_goal_percentage"),
            rec.get("usage_percentage"),
            rec.get("offensive_rebound_percentage"),
            rec.get("defensive_rebound_percentage"),
            rec.get("assist_percentage"),
            rec.get("steal_percentage"),
            rec.get("block_percentage"),
            rec.get("turnover_percentage"),
            rec.get("offensive_rating"),
            rec.get("defensive_rating"),
            rec.get("player_efficiency_rating"),
            rec.get("efficiency_rating"),
            rec.get("plus_minus"),
            rec.get("season"),
            rec.get("updated_at"),
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO transfer_portal VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    return len(rows)


if __name__ == "__main__":
    print("Fetching srating.io tokens...")
    kryptos, secret = get_tokens()
    print(f"  kryptos: {kryptos[:8]}...")

    print("Fetching transfer portal data...")
    data = fetch_transfer_data(kryptos, secret)
    print(f"  {len(data):,} players returned")

    conn = sqlite3.connect(DB_PATH)
    build_table(conn)
    n = load_records(conn, data)
    conn.close()

    print(f"  {n:,} rows written to transfer_portal table")
    print("Done.")
