"""
build_synergy_playtypes.py

Scrapes Synergy Sports for College Men 2025-26 play-type data.
Stores aggregated stats in synergy_playtypes table in scouting_hub.db.

Strategy:
  - GET possessionplaylists/statistics/1/clips with rootplayer eq oid(PLAYER_ID)
    → returns only the player's own offensive possessions (Spot-Up, Transition,
      ISO, Post-Up, P&R BH, Off-Screen, Hand-Off, Cut, Off-Rebound, etc.)
  - POST feedEventReports/pick with playerrole eq 'screener'
    → P&R Screener stats (screener is not rootplayer, so not in clips)

Table: synergy_playtypes
  (synergy_id, player_name, team_name, play_type,
   possessions, points, ppp, freq_pct, updated)

Usage:
    caffeinate -i python3 -u build_synergy_playtypes.py 2>&1 | tee /tmp/synergy_build.log
"""

import asyncio, base64, json, sqlite3, time, urllib.request, urllib.error
from pathlib import Path
from playwright.async_api import async_playwright

# ── Config ─────────────────────────────────────────────────────────────────
USERNAME = "colinkraft2005@gmail.com"
PASSWORD = "Colin4036!"
APP_URL  = "https://apps.synergysports.com"
LEAGUE   = "54457dce300969b132fcfb37"
SEASON   = "68a4bd7588184c4b74497a91"
COMP_KEY = f"{LEAGUE}:CEE"
BASE_API = "https://basketball.synergysportstech.com/api"
DB_PATH  = "scouting_hub.db"
DELAY    = 0.15   # seconds between API requests

# NCAA Division I conference ID (used as player list filter)
D1_CONF  = "54457dce300969b132fcfb4a"

# ── DB setup ───────────────────────────────────────────────────────────────
def build_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synergy_playtypes (
            synergy_id   TEXT,
            player_name  TEXT,
            team_name    TEXT,
            play_type    TEXT,
            possessions  INTEGER,
            points       REAL,
            ppp          REAL,
            freq_pct     REAL,
            updated      TEXT,
            PRIMARY KEY (synergy_id, play_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_spt_player ON synergy_playtypes(player_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_spt_team   ON synergy_playtypes(team_name)")
    conn.commit()

# ── Auth ───────────────────────────────────────────────────────────────────
bearer_holder = {"token": "", "expires_at": 0}

async def login():
    """Login via Playwright and return fresh bearer token."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
        tok_holder = {"t": ""}

        async def on_resp(response):
            tok = dict(response.request.headers).get("authorization", "")
            if tok.startswith("Bearer ") and not tok_holder["t"]:
                tok_holder["t"] = tok

        page.on("response", on_resp)
        try:
            await page.goto(f"{APP_URL}/basketball/players?leagueId={LEAGUE}&seasonId={SEASON}",
                            wait_until="domcontentloaded", timeout=20000)
        except: pass
        await page.wait_for_timeout(1500)
        for s in ['input[placeholder*="user" i]', 'input[type="email"]']:
            if await page.locator(s).count(): await page.fill(s, USERNAME); break
        for s in ['input[type="password"]']:
            if await page.locator(s).count(): await page.fill(s, PASSWORD); break
        for s in ['button[type="submit"]', 'button:has-text("Login")']:
            if await page.locator(s).count(): await page.click(s); break
        try: await page.wait_for_url(f"{APP_URL}/**", timeout=15000)
        except: pass
        await page.wait_for_timeout(2000)
        await browser.close()
        return tok_holder["t"]

def get_bearer():
    if bearer_holder["token"] and time.time() < bearer_holder["expires_at"] - 30:
        return bearer_holder["token"]
    print("  [auth] Refreshing bearer token…")
    tok = asyncio.run(login())
    bearer_holder["token"] = tok
    bearer_holder["expires_at"] = time.time() + 570   # ~9.5 min
    print(f"  [auth] Token refreshed: {tok[:60]}…")
    return tok

def http_post(url, body, bearer):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": bearer,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}

def http_get(url, bearer):
    req = urllib.request.Request(url, headers={
        "Authorization": bearer,
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}

# ── Player list (D1 only, paginated) ──────────────────────────────────────
def get_all_players():
    """Fetch D1 player list via playerswithboxscore POST (conferenceIds filter)."""
    players = {}
    skip = 0
    take = 200

    while True:
        bearer = get_bearer()
        url = f"{BASE_API}/leagues/{LEAGUE}/seasons/{SEASON}/playerswithboxscore"
        body = {
            "competitionDefinitionKey": COMP_KEY,
            "conferenceIds": [D1_CONF],   # D1 only
            "divisionIds": None,
            "teamId": None,
            "offensiveRoles": [],
            "searchTerm": "",
            "skip": skip,
            "take": take,
        }
        status, data = http_post(url, body, bearer)
        if status != 200:
            print(f"  Player list error: {status} at skip={skip}")
            break

        result = data.get("result", [])
        if not result:
            break

        for item in result:
            pid = item.get("id", "")
            name = (item.get("name") or
                    f"{item.get('firstName','')} {item.get('lastName','')}".strip())
            team = (item.get("team") or {}).get("name", "")
            tid  = (item.get("team") or {}).get("id", "")
            if pid:
                players[pid] = {"name": name, "team": team, "team_id": tid}

        total = data.get("totalRecords", len(result))
        pct = 100 * (skip + len(result)) / max(total, 1)
        print(f"  Players: {skip + len(result)}/{total} ({pct:.0f}%)")

        if len(result) < take:
            break
        skip += take
        time.sleep(DELAY)

    return players

# ── Points from clip title ─────────────────────────────────────────────────
def parse_points(title: str) -> float:
    """Extract points from clip title (approximation — misses FT-only plays)."""
    t = title.strip().lower()
    if t.startswith("make 3"):  return 3.0
    if t.startswith("make 2"):  return 2.0
    if t.startswith("make 1"):  return 1.0
    if "and 1" in t:            return 1.0   # bonus FT on top of a make
    if "foul drawn" in t:       return 1.4   # ~2 FTs × 70% make rate
    return 0.0

# ── Play-type stats for one player ─────────────────────────────────────────
def get_clips_playtypes(pid: str) -> dict:
    """
    GET all rootplayer clips for a player and aggregate by play type.
    Returns {play_type: {"possessions": int, "points": float}}
    """
    bearer = get_bearer()
    expr = base64.b64encode(f"rootplayer eq oid({pid})".encode()).decode()
    url = (f"{BASE_API}/possessionplaylists/statistics/1/clips"
           f"?expression={expr}&take=9999&sort=gameDate:desc")

    status, data = http_get(url, bearer)
    if status != 200:
        return {}

    by_type = {}
    for clip in data.get("result", []):
        title    = clip.get("title", "")
        comments = clip.get("comments", "")
        parts    = comments.split(" > ")
        if len(parts) < 2:
            continue
        play_type = parts[1].strip()
        pts = parse_points(title)

        if play_type not in by_type:
            by_type[play_type] = {"possessions": 0, "points": 0.0}
        by_type[play_type]["possessions"] += 1
        by_type[play_type]["points"] += pts

    return by_type

def get_screener_playtypes(pid: str, team_id: str) -> dict:
    """
    POST feedEventReports/pick with screener role to get P&R Screener stats.
    Returns {"P&R Screener": {"possessions": int, "points": float}} or empty.
    """
    if not team_id:
        return {}
    bearer = get_bearer()
    expr = (f"season eq oid({SEASON}) and offensiveteam eq oid({team_id}) "
            f"and (match(eventactors, playerrole eq 'screener' and player eq oid({pid})))")
    status, data = http_post(f"{BASE_API}/feedEventReports/pick", {"expressions": [expr]}, bearer)
    if status != 200 or not isinstance(data, list) or not data:
        return {}
    d = data[0]
    total_picks = d.get("totalPicks", 0)
    if total_picks == 0:
        return {}
    pts = float(d.get("pointsScored", 0))
    return {"P&R Screener": {"possessions": total_picks, "points": pts}}

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB_PATH)
    build_table(conn)

    done_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT synergy_id FROM synergy_playtypes"
    ).fetchall()}
    print(f"Already stored: {len(done_ids)} players")

    print("\nFetching D1 player list…")
    players = get_all_players()
    print(f"Total D1 players: {len(players)}")

    Path("/tmp/synergy_all_players.json").write_text(json.dumps({
        "player_count": len(players),
        "sample": {pid: v for pid, v in list(players.items())[:20]}
    }, indent=2))

    todo = [(pid, info) for pid, info in players.items() if pid not in done_ids]
    print(f"Players to fetch: {len(todo)}")

    errors = 0
    for i, (pid, info) in enumerate(todo):
        name    = info["name"]
        team    = info["team"]
        team_id = info.get("team_id", "")

        # Primary: rootplayer clips (all play types except screener)
        by_type = get_clips_playtypes(pid)
        time.sleep(DELAY)

        # Add P&R Screener from feedEventReports
        screener = get_screener_playtypes(pid, team_id)
        by_type.update(screener)
        time.sleep(DELAY)

        if not by_type:
            errors += 1
        else:
            total_poss = sum(v["possessions"] for v in by_type.values())
            now = time.strftime("%Y-%m-%d")
            rows = []
            for pt, stats in by_type.items():
                poss = stats["possessions"]
                pts  = stats["points"]
                ppp  = pts / poss if poss else 0.0
                freq = 100.0 * poss / total_poss if total_poss else 0.0
                rows.append((pid, name, team, pt, poss, round(pts,1), round(ppp,3), round(freq,1), now))

            conn.executemany("""
                INSERT OR REPLACE INTO synergy_playtypes
                (synergy_id, player_name, team_name, play_type,
                 possessions, points, ppp, freq_pct, updated)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, rows)
            conn.commit()

        if (i + 1) % 50 == 0:
            pct = 100 * (i + 1) / len(todo)
            stored = conn.execute("SELECT COUNT(*) FROM synergy_playtypes").fetchone()[0]
            players_done = conn.execute(
                "SELECT COUNT(DISTINCT synergy_id) FROM synergy_playtypes"
            ).fetchone()[0]
            print(f"  {i+1}/{len(todo)} ({pct:.1f}%) — {players_done} players, "
                  f"{stored} rows — {errors} errors")

    stored  = conn.execute("SELECT COUNT(*) FROM synergy_playtypes").fetchone()[0]
    p_done  = conn.execute("SELECT COUNT(DISTINCT synergy_id) FROM synergy_playtypes").fetchone()[0]
    print(f"\nDone. {p_done} players, {stored} play-type rows. Errors: {errors}")
    conn.close()


if __name__ == "__main__":
    # Quick sanity check before full run
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("=== TEST MODE: Karaban only ===")
        tok = asyncio.run(login())
        bearer_holder["token"] = tok
        bearer_holder["expires_at"] = time.time() + 570

        pid     = "636aaf8e12087ae7a21c9caf"
        team_id = "54457dd3300969b132fcfea2"
        name    = "Alex Karaban"

        clips_stats    = get_clips_playtypes(pid)
        screener_stats = get_screener_playtypes(pid, team_id)
        all_stats      = {**clips_stats, **screener_stats}

        total = sum(v["possessions"] for v in all_stats.values())
        print(f"\nKaraban play-type stats ({total} total possessions):")
        for pt, stats in sorted(all_stats.items(), key=lambda x: -x[1]["possessions"]):
            poss = stats["possessions"]
            pts  = stats["points"]
            ppp  = pts / poss if poss else 0
            freq = 100 * poss / total if total else 0
            print(f"  {pt:<20} {poss:>4} poss ({freq:5.1f}%)  {ppp:.2f} PPP  {pts:.0f} pts")
    else:
        main()
