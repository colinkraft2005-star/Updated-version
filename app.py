import streamlit as st
import pandas as pd
import requests
import sqlite3
import urllib.parse
import re
import math
import ssl
import urllib3
import time
from datetime import datetime

# ==========================================
# LOCAL MAC SSL OVERRIDE
# ==========================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

st.set_page_config(layout="wide")


# ==========================================
# DATABASE INIT
# ==========================================
def init_db():
    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS player_notes
                   (
                       player_name  TEXT PRIMARY KEY,
                       team_name    TEXT,
                       scout_name   TEXT,
                       priority_tier TEXT,
                       position     TEXT,
                       role         TEXT,
                       rumored_nil  TEXT,
                       personal_val TEXT,
                       agent        TEXT,
                       agency       TEXT,
                       photo_url    TEXT,
                       eval_date    TEXT,
                       notes        TEXT,
                       coach_notes  TEXT
                   )
                   ''')
    try:
        cursor.execute("ALTER TABLE player_notes ADD COLUMN coach_notes TEXT")
        conn.commit()
    except Exception:
        pass

    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS roster
                   (
                       id          INTEGER PRIMARY KEY AUTOINCREMENT,
                       player_name TEXT,
                       position    TEXT,
                       depth       INTEGER,
                       descriptor  TEXT,
                       bt_name     TEXT
                   )
                   ''')

    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS sr_stats_cache
                   (
                       player_name TEXT PRIMARY KEY,
                       team_name   TEXT,
                       gp          INTEGER,
                       gs          INTEGER,
                       mpg         REAL,
                       ppg         REAL,
                       rpg         REAL,
                       apg         REAL,
                       spg         REAL,
                       bpg         REAL,
                       tov         REAL,
                       total_ast   INTEGER,
                       total_tov   INTEGER,
                       fetched_at  TEXT
                   )
                   ''')
    conn.commit()
    conn.close()


def seed_roster_if_empty():
    """Pre-load the 26-27 UCLA roster on first run only."""
    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM roster")
    count = cursor.fetchone()[0]
    if count == 0:
        seed = [
            ("Trent Perry",      "PG", 1, "13 PPG / 59.5 TS%",            "Trent Perry"),
            ("Stink Robinson",   "PG", 2, "4.5% STL rate / 43.3% from 3", ""),
            ("Markell Alston",   "PG", 3, "Rs-Fr",                         ""),
            ("Jaylen Petty",     "CG", 1, "67 made 3s as FR / 10 PPG on a Top 15 team", "Jaylen Petty"),
            ("Eric Freeny",      "CG", 2, "Glue guy",                      ""),
            ("Gunars Grinvalds", "CG", 3, "Freshman",                      ""),
            ("OPEN",             "SF", 1, "Starting SF — TBD",             ""),
            ("Brandon Williams", "SF", 2, "Rs-Junior",                     "Brandon Williams"),
            ("JoJo Philon",      "SF", 3, "Freshman",                      ""),
            ("Eric Dailey Jr.",  "PF", 1, "12 PPG / 6 RPG",               "Eric Dailey Jr."),
            ("Sergej Macura",    "PF", 2, "Top 15 Rebounder in SEC",      "Sergej Macura"),
            ("Xavier Booker",    "C",  1, "43.3% 3PT% / 4th best Block rate in B1G", "Xavier Booker"),
            ("Filip Jovic",      "C",  2, "Top 10 O-Rebounder in SEC / 9.5 PPG last two months", "Filip Jovic"),
            ("Javonte Floyd",    "C",  3, "Freshman",                      ""),
        ]
        cursor.executemany(
            "INSERT INTO roster (player_name, position, depth, descriptor, bt_name) VALUES (?, ?, ?, ?, ?)",
            seed
        )
        conn.commit()
    conn.close()


init_db()
seed_roster_if_empty()


# ==========================================
# HEADSHOT FETCHER
# ==========================================
def fetch_sr_headshot_silent(player_name, team_name=""):
    cleaned_name = player_name.replace(".", "").replace(",", "")
    safe_name = urllib.parse.quote(cleaned_name)
    search_url = f"https://www.sports-reference.com/cbb/search/search.fcgi?search={safe_name}"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    img_pattern = r'src="(https://www.sports-reference.com/req/[^"]+/cbb/images/players/[^"]+\.jpg)"'
    suffix_words = ['jr', 'ii', 'iii', 'iv', 'v']
    name_parts = cleaned_name.lower().split()
    detected_suffix = name_parts[-1] if (name_parts and name_parts[-1] in suffix_words) else None

    def parse_html_for_image(html, current_url):
        match = re.search(img_pattern, html)
        if match:
            return match.group(1)
        if "/cbb/search/search.fcgi" in current_url:
            results = re.findall(r'href="(/cbb/players/([^"]+)\.html)"[^>]*>(.*?)<\/a>(.*?)(?:<\/div>|<li>|<tr|<td>)',
                                 html, re.IGNORECASE | re.DOTALL)
            if results:
                for link, slug, display_name, context in results:
                    if team_name and (team_name.lower() in context.lower() or team_name.lower() in display_name.lower()):
                        if detected_suffix and f"-{detected_suffix}" not in slug.lower():
                            continue
                        return fetch_profile_image(link)
                suffix_matches = []
                for link, slug, display_name, context in results:
                    if detected_suffix and f"-{detected_suffix}" in slug.lower():
                        suffix_matches.append(link)
                if suffix_matches:
                    return fetch_profile_image(suffix_matches[-1])
                try:
                    def extract_num(r):
                        num_match = re.search(r'-(\d+)$', r[1])
                        return int(num_match.group(1)) if num_match else 0
                    best_link = max(results, key=extract_num)[0]
                    return fetch_profile_image(best_link)
                except Exception:
                    return fetch_profile_image(results[0][0])
        return ""

    def fetch_profile_image(player_page_path):
        try:
            player_url = f"https://www.sports-reference.com{player_page_path}"
            player_response = requests.get(player_url, headers=headers, timeout=5, verify=False)
            img_match = re.search(img_pattern, player_response.text)
            return img_match.group(1) if img_match else ""
        except Exception:
            return ""

    try:
        response = requests.get(search_url, headers=headers, timeout=5, verify=False)
        img_url = parse_html_for_image(response.text, response.url)
        if img_url:
            return img_url
        if detected_suffix:
            base_name = " ".join(name_parts[:-1])
            fallback_url = f"https://www.sports-reference.com/cbb/search/search.fcgi?search={urllib.parse.quote(base_name)}"
            fallback_resp = requests.get(fallback_url, headers=headers, timeout=5, verify=False)
            img_url = parse_html_for_image(fallback_resp.text, fallback_resp.url)
            if img_url:
                return img_url
    except Exception:
        pass
    return ""


# ==========================================
# ESPN STATS FETCHER (season per game)
# ==========================================
def fetch_espn_stats(player_name, team_name=""):
    """
    Fetch per-game stats from ESPN using athlete statistics endpoint.
    Finds athlete ID by searching ESPN, then hits their stats page.
    Caches in SQLite after first fetch.
    """
    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT gp, gs, mpg, ppg, rpg, apg, spg, bpg, tov, total_ast, total_tov FROM sr_stats_cache WHERE player_name = ?",
        (player_name,)
    )
    cached = cursor.fetchone()
    conn.close()

    if cached and cached[0] and int(cached[0]) > 0:
        return {
            "gp": int(cached[0] or 0),
            "ppg": float(cached[3] or 0),
            "rpg": float(cached[4] or 0),
            "apg": float(cached[5] or 0),
            "spg": float(cached[6] or 0),
            "bpg": float(cached[7] or 0),
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.espn.com/"
    }

    def safe_float(val):
        try:
            return float(str(val).strip()) if val else 0.0
        except:
            return 0.0

    def safe_int(val):
        try:
            return int(float(str(val).strip())) if val else 0
        except:
            return 0

    try:
        # Step 1: Search ESPN for athlete
        search_url = f"https://site.api.espn.com/apis/search/v2?query={urllib.parse.quote(player_name)}&sport=basketball&league=mens-college-basketball&limit=5&type=player"
        resp = requests.get(search_url, headers=headers, timeout=8, verify=False)
        data = resp.json()

        athlete_id = None
        for result in data.get("results", []):
            for item in result.get("contents", result.get("items", [])):
                uid = item.get("athleteId", item.get("id", ""))
                if uid:
                    athlete_id = uid
                    break
            if athlete_id:
                break

        # Fallback: try site search
        if not athlete_id:
            search2 = f"https://site.api.espn.com/apis/common/v3/search?query={urllib.parse.quote(player_name)}&sport=basketball&league=mens-college-basketball&limit=5"
            resp2 = requests.get(search2, headers=headers, timeout=8, verify=False)
            d2 = resp2.json()
            for section in d2.get("results", []):
                for item in section.get("items", []):
                    uid = item.get("id", "")
                    if uid:
                        athlete_id = uid
                        break
                if athlete_id:
                    break

        if not athlete_id:
            return None

        # Step 2: Get stats
        stats_url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/athletes/{athlete_id}/statistics"
        stats_resp = requests.get(stats_url, headers=headers, timeout=8, verify=False)
        stats_data = stats_resp.json()

        gp = ppg = rpg = apg = spg = bpg = 0.0

        splits = stats_data.get("splits", {})
        categories = splits.get("categories", [])

        for cat in categories:
            names  = cat.get("names", [])
            values = cat.get("totals", cat.get("values", []))
            if not names or not values:
                continue
            stat_map = dict(zip(names, values))
            test_gp = safe_int(stat_map.get("gamesPlayed", stat_map.get("GP", 0)))
            if test_gp > 0:
                gp  = test_gp
                ppg = safe_float(stat_map.get("avgPoints", stat_map.get("PTS", 0)))
                rpg = safe_float(stat_map.get("avgRebounds", stat_map.get("REB", 0)))
                apg = safe_float(stat_map.get("avgAssists", stat_map.get("AST", 0)))
                spg = safe_float(stat_map.get("avgSteals", stat_map.get("STL", 0)))
                bpg = safe_float(stat_map.get("avgBlocks", stat_map.get("BLK", 0)))
                break

        if gp > 0:
            result = {"gp": int(gp), "ppg": ppg, "rpg": rpg, "apg": apg, "spg": spg, "bpg": bpg}
            # Cache it
            conn = sqlite3.connect('scouting_hub.db')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sr_stats_cache
                (player_name, team_name, gp, gs, mpg, ppg, rpg, apg, spg, bpg, tov, total_ast, total_tov, fetched_at)
                VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 0, 0, 0, ?)
                ON CONFLICT(player_name) DO UPDATE SET
                    gp=excluded.gp, ppg=excluded.ppg, rpg=excluded.rpg,
                    apg=excluded.apg, spg=excluded.spg, bpg=excluded.bpg,
                    fetched_at=excluded.fetched_at
            ''', (player_name, team_name, int(gp), ppg, rpg, apg, spg, bpg,
                  datetime.now().strftime("%Y-%m-%d")))
            conn.commit()
            conn.close()
            return result

    except Exception:
        pass

    return None
    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT gp, gs, mpg, ppg, rpg, apg, spg, bpg, tov, total_ast, total_tov FROM sr_stats_cache WHERE player_name = ?",
        (player_name,)
    )
    cached = cursor.fetchone()
    conn.close()

    if cached and cached[0] and cached[0] > 0:
        return {
            "gp": cached[0], "gs": cached[1],
            "total_pts": round(cached[3] * cached[0]) if cached[3] and cached[0] else 0,
            "total_reb": round(cached[4] * cached[0]) if cached[4] and cached[0] else 0,
            "total_ast": cached[9] or 0,
            "total_tov": cached[10] or 0,
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    def safe_int(val):
        try:
            return int(float(str(val).strip())) if val else 0
        except:
            return 0

    def safe_float(val):
        try:
            return float(str(val).strip()) if val else 0.0
        except:
            return 0.0

    try:
        # Search ESPN for player
        search_name = urllib.parse.quote(player_name)
        search_url = f"https://site.api.espn.com/apis/common/v3/search?query={search_name}&type=athlete&sport=basketball&league=mens-college-basketball&limit=5"
        resp = requests.get(search_url, headers=headers, timeout=8, verify=False)
        data = resp.json()

        athlete_id = None
        results = data.get("results", [])
        for result in results:
            for item in result.get("items", []):
                name = item.get("displayName", "")
                uid = item.get("id", "")
                if name and uid:
                    athlete_id = uid
                    break
            if athlete_id:
                break

        if not athlete_id:
            return None

        # Fetch stats
        stats_url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/athletes/{athlete_id}/statistics"
        stats_resp = requests.get(stats_url, headers=headers, timeout=8, verify=False)
        stats_data = stats_resp.json()

        # Parse the splits/categories
        gp = gs = total_pts = total_reb = total_ast = total_tov = 0

        splits = stats_data.get("splits", {})
        categories = splits.get("categories", [])

        for cat in categories:
            cat_name = cat.get("name", "").lower()
            if "total" in cat_name or "general" in cat_name or cat_name == "":
                names  = cat.get("names", [])
                totals = cat.get("totals", [])
                stat_map = dict(zip(names, totals))
                gp        = safe_int(stat_map.get("GP", stat_map.get("gamesPlayed", 0)))
                gs        = safe_int(stat_map.get("GS", stat_map.get("gamesStarted", 0)))
                total_pts = safe_int(stat_map.get("PTS", stat_map.get("points", 0)))
                total_reb = safe_int(stat_map.get("REB", stat_map.get("totalRebounds", 0)))
                total_ast = safe_int(stat_map.get("AST", stat_map.get("assists", 0)))
                total_tov = safe_int(stat_map.get("TO",  stat_map.get("turnovers", 0)))
                if gp > 0:
                    break

        result = {
            "gp": gp, "gs": gs,
            "total_pts": total_pts, "total_reb": total_reb,
            "total_ast": total_ast, "total_tov": total_tov,
        }

        # Cache it
        ppg = round(total_pts / gp, 1) if gp > 0 else 0.0
        rpg = round(total_reb / gp, 1) if gp > 0 else 0.0
        apg = round(total_ast / gp, 1) if gp > 0 else 0.0
        conn = sqlite3.connect('scouting_hub.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sr_stats_cache
            (player_name, team_name, gp, gs, mpg, ppg, rpg, apg, spg, bpg, tov, total_ast, total_tov, fetched_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 0, ?, ?, ?)
            ON CONFLICT(player_name) DO UPDATE SET
                gp=excluded.gp, gs=excluded.gs, ppg=excluded.ppg,
                rpg=excluded.rpg, apg=excluded.apg,
                total_ast=excluded.total_ast, total_tov=excluded.total_tov,
                fetched_at=excluded.fetched_at
        ''', (player_name, team_name, gp, gs, ppg, rpg, apg, total_ast, total_tov,
              datetime.now().strftime("%Y-%m-%d")))
        conn.commit()
        conn.close()
        return result

    except Exception:
        pass

    return {"gp": 0, "gs": 0, "total_pts": 0, "total_reb": 0, "total_ast": 0, "total_tov": 0}


# ==========================================
# BARTTORVIK FETCH
# ==========================================
def fetch_barttorvik_safe(top_filter=None, retries=3, delay_between_requests=4):
    base_url = 'https://barttorvik.com/getadvstats.php?year=2026&page=playerstat&json=1'
    url = base_url if top_filter is None else f"{base_url}&top={top_filter}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://barttorvik.com/"
    }

    def parse_raw(raw_data):
        def safe_float(row_list, idx):
            try:
                if idx < len(row_list) and row_list[idx] is not None and str(row_list[idx]).strip() != "":
                    return float(row_list[idx])
                return 0.0
            except (ValueError, TypeError, IndexError):
                return 0.0
        cleaned_rows = []
        for row in raw_data:
            if len(row) < 53:
                continue
            cleaned_rows.append({
                "PLAYER":     str(row[0]),
                "TEAM":       str(row[1]),
                "CONF":       str(row[2]),
                "MIN_PCT":    safe_float(row, 4),
                "ORTG":       safe_float(row, 5),
                "USG":        safe_float(row, 6),
                "EFG":        safe_float(row, 7),
                "TS":         safe_float(row, 8),
                "OR":         safe_float(row, 9),
                "DR":         safe_float(row, 10),
                "AST":        safe_float(row, 11),
                "TO":         safe_float(row, 12),
                "BLK":        safe_float(row, 22),
                "STL":        safe_float(row, 23),
                "FTR":        safe_float(row, 24),
                "TWO_P":      safe_float(row, 18) * 100,
                "THREE_P":    safe_float(row, 21) * 100,
                "THREE_PA":   safe_float(row, 65) if len(row) > 65 else 0.0,
                "CLASS":      str(row[25]) if len(row) > 25 else "",
                "HEIGHT":     str(row[26]) if len(row) > 26 else "",
                "TORVIK_POS": str(row[27]) if len(row) > 27 else "",
                "PRPG":       safe_float(row, 28),
                "BPM":        safe_float(row, 50),
                "OBPM":       safe_float(row, 51),
                "DBPM":       safe_float(row, 52),
                "GP":         int(float(row[3])) if len(row) > 3 and row[3] is not None else 0,
            })
        return pd.DataFrame(cleaned_rows) if cleaned_rows else None

    # Try cloudscraper FIRST (bypasses Cloudflare/bot detection)
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper()
        response = scraper.get(url)
        if response.text.strip():
            raw_data = response.json()
            if raw_data:
                return parse_raw(raw_data)
    except Exception:
        pass

    # Fallback: standard requests
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, verify=False, timeout=20)
            if response.text.strip():
                raw_data = response.json()
                if raw_data:
                    return parse_raw(raw_data)
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay_between_requests)

    return None


@st.cache_data(ttl=3600)
def load_all_data_v6():
    df = fetch_barttorvik_safe(top_filter=None)
    if df is None:
        return None
    # Fetch basicstat in same call and merge PPG/RPG/APG
    try:
        url2 = 'https://barttorvik.com/getadvstats.php?year=2026&page=basicstat&json=1'
        raw2 = None
        try:
            import cloudscraper
            sc2 = cloudscraper.create_scraper()
            r2 = sc2.get(url2)
            if r2.text.strip():
                raw2 = r2.json()
        except Exception:
            pass
        if not raw2:
            headers2 = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://barttorvik.com/"}
            try:
                r2 = requests.get(url2, headers=headers2, verify=False, timeout=20)
                if r2.text.strip():
                    raw2 = r2.json()
            except Exception:
                pass
        if raw2:
            basic_rows = []
            for row in raw2:
                try:
                    n = len(row)
                    basic_rows.append({
                        "PLAYER": str(row[0]),
                        "PPG": float(row[n-4]) if row[n-4] is not None else 0.0,
                        "RPG": float(row[n-8]) if row[n-8] is not None else 0.0,
                        "APG": float(row[n-7]) if row[n-7] is not None else 0.0,
                    })
                except:
                    continue
            df_b = pd.DataFrame(basic_rows).drop_duplicates(subset=["PLAYER"], keep="first")
            df = df.merge(df_b, on="PLAYER", how="left")
            df["PPG"] = df["PPG"].fillna(0.0)
            df["RPG"] = df["RPG"].fillna(0.0)
            df["APG"] = df["APG"].fillna(0.0)
    except:
        df["PPG"] = 0.0
        df["RPG"] = 0.0
        df["APG"] = 0.0
    return df




# ==========================================
# SEQUENTIAL DATA LOAD WITH PROGRESS BAR
# ==========================================
load_bar = st.progress(0, text="Loading full database...")
df_all = load_all_data_v6()


load_bar.progress(100, text="Database ready.")
time.sleep(0.4)
load_bar.empty()

failed = []
if df_all is None:    failed.append("All Games")


if failed:
    st.error(
        f"BartTorvik returned empty data for: **{', '.join(failed)}**\n\n"
        "This usually means your IP is temporarily rate-limited by the server. "
        "Try one of the following:\n"
        "- Wait 10-15 minutes and rerun\n"
        "- Switch to your phone hotspot and rerun\n"
        "- Connect to a VPN and rerun"
    )
    st.stop()

all_player_names = sorted(list(df_all["PLAYER"].unique()))

if "active_player" not in st.session_state:
    st.session_state.active_player = all_player_names[0]

# ==========================================
# HEADER
# ==========================================
head_col1, head_col2 = st.columns([1, 12])
with head_col1:
    st.image("https://cdn.freebiesupply.com/logos/large/2x/ucla-bruins-1-logo-png-transparent.png", width=55)
with head_col2:
    st.markdown("<h2 style='margin: 0; padding-top: 8px; color: #FFFFFF;'>UCLA Transfer Portal Database</h2>",
                unsafe_allow_html=True)
st.write("***")

tab_depth, tab5, tab_comp, tab2, tab3, tab4 = st.tabs([
    "Depth Chart",
    "Player Card",
    "Comp Results",
    "Portal Discovery Engine",
    "Front Office Target Board",
    "Big Board Print View",
])

# ==========================================
# TAB: DEPTH CHART
# ==========================================
with tab_depth:
    st.subheader("26-27 UCLA Bruins — Depth Chart")

    with st.expander("Edit Roster", expanded=False):
        st.caption(
            "Add, remove, or reorder players. **Position** must be one of PG / CG / SF / PF / C. "
            "**Depth** sets the stacking order (1 = starter). For stats to auto-link, **BT Name** must "
            "match the player's exact BartTorvik spelling — leave it blank for freshmen / walk-ons."
        )

        conn = sqlite3.connect('scouting_hub.db')
        roster_df = pd.read_sql_query(
            "SELECT player_name AS Player, position AS Pos, depth AS Depth, "
            "descriptor AS Descriptor, bt_name AS [BT Name] FROM roster ORDER BY position, depth",
            conn
        )
        conn.close()

        edited = st.data_editor(
            roster_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            column_config={
                "Pos": st.column_config.SelectboxColumn("Pos", options=["PG", "CG", "SF", "PF", "C"], required=True),
                "Depth": st.column_config.NumberColumn("Depth", min_value=1, max_value=10, step=1),
            },
            key="roster_editor"
        )

        if st.button("Save Roster Changes"):
            conn = sqlite3.connect('scouting_hub.db')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM roster")
            for _, r in edited.iterrows():
                pname = str(r["Player"]).strip() if pd.notna(r["Player"]) else ""
                if not pname:
                    continue
                cursor.execute(
                    "INSERT INTO roster (player_name, position, depth, descriptor, bt_name) VALUES (?, ?, ?, ?, ?)",
                    (
                        pname,
                        str(r["Pos"]) if pd.notna(r["Pos"]) else "PG",
                        int(r["Depth"]) if pd.notna(r["Depth"]) else 1,
                        str(r["Descriptor"]) if pd.notna(r["Descriptor"]) else "",
                        str(r["BT Name"]) if pd.notna(r["BT Name"]) else "",
                    )
                )
            conn.commit()
            conn.close()
            st.success("Roster updated.")
            st.rerun()

    conn = sqlite3.connect('scouting_hub.db')
    chart_df = pd.read_sql_query(
        "SELECT player_name, position, depth, descriptor, bt_name FROM roster ORDER BY depth",
        conn
    )
    conn.close()

    POSITIONS = [("PG", "Point Guard"), ("CG", "Combo Guard"), ("SF", "Small Forward"),
                 ("PF", "Power Forward"), ("C", "Center")]

    pos_cols = st.columns(5)

    for i, (pos_code, pos_label) in enumerate(POSITIONS):
        with pos_cols[i]:
            st.markdown(f"""
                <div style='background-color:#2774AE; color:white; font-weight:bold;
                            text-align:center; padding:8px; border-radius:6px; margin-bottom:12px;
                            font-size:13px; letter-spacing:0.5px;'>
                    {pos_code}<br><span style='font-size:9px; font-weight:400; opacity:0.85;'>{pos_label}</span>
                </div>
            """, unsafe_allow_html=True)

            group = chart_df[chart_df["position"] == pos_code].sort_values("depth")

            if group.empty:
                st.caption("No players assigned")
                continue

            for _, pl in group.iterrows():
                pname = pl["player_name"]
                descriptor = pl["descriptor"] if pl["descriptor"] else ""
                bt_name = pl["bt_name"] if pl["bt_name"] else ""
                is_open = pname.strip().upper() == "OPEN"
                is_starter = int(pl["depth"]) == 1

                if is_open:
                    st.markdown(
                        "<div style=\"border:2px dashed #FFD100;border-radius:8px;padding:14px 10px;"
                        "margin-bottom:10px;background-color:rgba(255,209,0,0.06);text-align:center;\">"
                        "<div style=\"font-size:13px;font-weight:bold;color:#FFD100;\">OPEN</div>"
                        "<div style=\"font-size:10px;color:#FFD100;opacity:0.85;margin-top:2px;\">" + descriptor + "</div>"
                        "</div>",
                        unsafe_allow_html=True
                    )
                    continue

                stat_line = ""
                if bt_name:
                    match = df_all[df_all["PLAYER"] == bt_name]
                    if not match.empty:
                        s = match.iloc[0]
                        stat_line = f"BPM {s['BPM']:.1f} · USG {s['USG']:.0f}% · eFG {s['EFG']:.0f}%"

                border = "#FFD100" if is_starter else "#CBD5E1"
                starter_badge = (
                    "<span style=\"font-size:8px;background:#FFD100;color:#0F172A;"
                    "font-weight:bold;padding:1px 5px;border-radius:3px;\">STARTER</span>"
                ) if is_starter else ""

                stat_html = (
                    "<div style=\"font-size:9.5px;color:#2774AE;font-weight:600;margin-top:3px;\">" + stat_line + "</div>"
                    if stat_line else ""
                )
                desc_html = (
                    "<div style=\"font-size:9.5px;color:#64748B;margin-top:2px;\">" + descriptor + "</div>"
                    if descriptor else ""
                )

                card_html = (
                    "<div style=\"border:1px solid " + border + ";border-left:4px solid " + border + ";border-radius:6px;"
                    "padding:9px 10px;margin-bottom:10px;background-color:#FFFFFF;"
                    "box-shadow:1px 1px 3px rgba(0,0,0,0.05);\">"
                    "<div style=\"display:flex;justify-content:space-between;align-items:center;\">"
                    "<span style=\"font-size:12.5px;font-weight:bold;color:#0F172A;\">" + pname + "</span>"
                    + starter_badge +
                    "</div>"
                    + stat_html + desc_html +
                    "</div>"
                )
                st.markdown(card_html, unsafe_allow_html=True)

                if bt_name and not df_all[df_all["PLAYER"] == bt_name].empty:
                    if st.button(f"View {pname}", key=f"depth_view_{pos_code}_{pname}",
                                 use_container_width=True):
                        st.session_state.active_player = bt_name
                        st.rerun()

    st.write("")
    st.caption("⭐ Yellow = projected starter · Dashed yellow = open slot · "
               "Returning/transfer players show live BartTorvik metrics; incoming freshmen show roster notes.")



# ==========================================
# SHARED POSITION DETECTION
# ==========================================
def detect_pos_group(torvik_pos, saved_pos, height_str, ast_rate):
    """Detect position group using Torvik position data first."""
    # Use Torvik position string (e.g. 'G', 'F', 'C', 'G/F', 'F/C')
    tp = str(torvik_pos).upper().strip()
    if tp and tp not in ["", "NONE", "NAN"]:
        if tp in ["PG", "SG", "G"]: return "G"
        if tp in ["SF", "PF", "F"]: return "F"  
        if tp in ["C"]: return "C"
        if "/" in tp:
            parts = tp.split("/")
            if parts[0] in ["G"]: return "G"
            if parts[0] in ["F"]: return "F"
            if parts[0] in ["C"]: return "C"
    # Fall back to saved position from DB
    if saved_pos:
        p = str(saved_pos).upper()
        if any(x in p for x in ["PG","CG","G"]): return "G"
        if any(x in p for x in ["PF","F","W","SF","WING"]): return "F"
        if "C" in p: return "C"
    # Last resort: height (handles 7'3" and 7-3 formats)
    try:
        ht = str(height_str).replace('"','').strip()
        if "'" in ht:
            parts = ht.split("'")
            inches = int(parts[0])*12 + (int(parts[1].strip()) if parts[1].strip().isdigit() else 0)
        elif "-" in ht:
            parts = ht.split("-")
            inches = int(parts[0])*12 + int(parts[1].strip())
        else:
            inches = 0
        if inches >= 82: return "C"
        elif inches >= 79: return "F"
        elif inches >= 75: return "G" if ast_rate > 20 else "F"
        else: return "G"
    except: return "G"

# ==========================================
# COMP ENGINE (top-level)
# ==========================================
def parse_ht(ht_str):
    try:
        s = str(ht_str).replace('"', '').strip()
        if "'" in s:
            parts = s.split("'")
            return int(parts[0].strip()) * 12 + (int(parts[1].strip()) if parts[1].strip().isdigit() else 0)
        if "-" in s:
            parts = s.split("-")
            return int(parts[0].strip()) * 12 + int(parts[1].strip())
        val = int(s)
        return val if val > 12 else val * 12
    except:
        return 78

def norm_dist(a, b, radius):
    try:
        return max(0.0, 1.0 - abs(float(a) - float(b)) / radius)
    except:
        return 0.0

def run_comps(target_row, all_df, pos_group, n=8):
    t_ht   = parse_ht(target_row["HEIGHT"])
    t_ortg = float(target_row.get("ORTG", 100))
    t_usg  = float(target_row.get("USG", 18))
    t_ts   = float(target_row.get("TS", 55))
    t_bpm  = float(target_row.get("BPM", 0))
    t_obpm = float(target_row.get("OBPM", 0))
    t_dbpm = float(target_row.get("DBPM", 0))
    t_ast  = float(target_row.get("AST", 15))
    t_to   = float(target_row.get("TO", 15))
    t_or   = float(target_row.get("OR", 5))
    t_dr   = float(target_row.get("DR", 15))
    t_blk  = float(target_row.get("BLK", 3))
    t_stl  = float(target_row.get("STL", 2))
    t_efg  = float(target_row.get("EFG", 50))
    t_3p   = float(target_row.get("THREE_P", 30))
    t_3pa  = float(target_row.get("THREE_PA", 5))
    t_min  = float(target_row.get("MIN_PCT", 50))

    base_w = {
        "ortg": 0.07, "usg": 0.07, "ts": 0.07, "efg": 0.06,
        "bpm": 0.07, "obpm": 0.05, "dbpm": 0.05,
        "ast": 0.07, "to": 0.05,
        "or": 0.05, "dr": 0.06,
        "blk": 0.05, "stl": 0.05,
        "3p": 0.05, "3pa": 0.04, "min": 0.04, "ht": 0.10
    }

    if pos_group == "G":
        base_w.update({"ortg":0.14,"ast":0.13,"to":0.10,"stl":0.10,"min":0.08,"3p":0.08,
                       "ts":0.07,"bpm":0.06,"ht":0.10,"usg":0.05,"efg":0.04,
                       "obpm":0.03,"dbpm":0.03,"or":0.02,"dr":0.03,"blk":0.02,"3pa":0.03})
    elif pos_group == "F":
        base_w.update({"bpm":0.14,"dbpm":0.10,"stl":0.10,"blk":0.10,"dr":0.10,"or":0.08,
                       "ht":0.10,"ts":0.06,"efg":0.04,"3p":0.05,"ast":0.04,"usg":0.04,
                       "ortg":0.04,"to":0.03,"obpm":0.04,"min":0.04,"3pa":0.02})
    elif pos_group == "C":
        base_w.update({"ortg":0.12,"or":0.12,"dr":0.12,"blk":0.10,"ast":0.08,"to":0.07,
                       "min":0.07,"ht":0.10,"bpm":0.06,"ts":0.05,"usg":0.04,"efg":0.03,
                       "stl":0.03,"dbpm":0.04,"obpm":0.03,"3p":0.02,"3pa":0.01})

    results = []
    target_name = str(target_row["PLAYER"])
    target_team = str(target_row["TEAM"])

    for _, row in all_df.iterrows():
        if str(row["PLAYER"]) == target_name and str(row["TEAM"]) == target_team:
            continue
        c_ht = parse_ht(row["HEIGHT"])
        if abs(t_ht - c_ht) > 5:
            continue
        scores = {
            "ortg": norm_dist(t_ortg, row.get("ORTG", 100), 15),
            "usg":  norm_dist(t_usg,  row.get("USG", 18),   10),
            "ts":   norm_dist(t_ts,   row.get("TS", 55),    12),
            "efg":  norm_dist(t_efg,  row.get("EFG", 50),   12),
            "bpm":  norm_dist(t_bpm,  row.get("BPM", 0),    8),
            "obpm": norm_dist(t_obpm, row.get("OBPM", 0),   8),
            "dbpm": norm_dist(t_dbpm, row.get("DBPM", 0),   6),
            "ast":  norm_dist(t_ast,  row.get("AST", 15),   12),
            "to":   norm_dist(t_to,   row.get("TO", 15),    10),
            "or":   norm_dist(t_or,   row.get("OR", 5),     8),
            "dr":   norm_dist(t_dr,   row.get("DR", 15),    10),
            "blk":  norm_dist(t_blk,  row.get("BLK", 3),    5),
            "stl":  norm_dist(t_stl,  row.get("STL", 2),    4),
            "3p":   norm_dist(t_3p,   row.get("THREE_P", 30), 15),
            "3pa":  norm_dist(t_3pa,  row.get("THREE_PA", 5), 8),
            "min":  norm_dist(t_min,  row.get("MIN_PCT", 50), 20),
            "ht":   norm_dist(t_ht,   c_ht, 4),
        }
        total = sum(scores[k] * base_w[k] for k in scores)
        results.append((total, row))

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:n]


# ==========================================
# TAB COMP: COMP RESULTS (active player)
# ==========================================
with tab_comp:
    st.subheader("Comp Results")

    active = st.session_state.active_player
    if not active or active not in df_all["PLAYER"].values:
        st.info("Select a player from the Player Card or Portal Discovery Engine tab to run comps.")
    else:
        comp_data = df_all[df_all["PLAYER"] == active].iloc[0]

        # Pull saved position
        conn = sqlite3.connect('scouting_hub.db')
        cursor = conn.cursor()
        cursor.execute("SELECT position FROM player_notes WHERE player_name = ?", (active,))
        comp_db = cursor.fetchone()
        conn.close()
        comp_saved_pos = comp_db[0] if comp_db and comp_db[0] else ""

        cr_pos = detect_pos_group(comp_data.get("TORVIK_POS",""), comp_saved_pos, comp_data.get("HEIGHT",""), comp_data.get("AST",0))

        # Player header
        st.markdown(f"**Running comps for: {active}**")
        cr1, cr2, cr3, cr4, cr5 = st.columns(5)
        cr1.metric("Team",   comp_data["TEAM"])
        cr2.metric("Conf",   comp_data["CONF"])
        cr3.metric("Class",  comp_data["CLASS"])
        cr4.metric("Height", comp_data["HEIGHT"])
        cr5.metric("BPM",    f"{comp_data['BPM']:.1f}")

        cr_pos_override = st.radio("Position group:", ["G", "F", "C"],
                                   index=["G", "F", "C"].index(cr_pos),
                                   horizontal=True, key="cr_pos_radio")
        cr_pos = cr_pos_override
        cr_n = st.slider("Comps to show:", 3, 15, 8, key="cr_n_slider")

        st.markdown("---")

        with st.spinner("Running comp analysis..."):
            cr_comps = run_comps(comp_data, df_all, cr_pos, n=cr_n)

        st.write(f"**Top {len(cr_comps)} comps ({cr_pos}) from {len(df_all):,} players, height ±5in:**")

        for match_score, match_row in cr_comps:
            pct = round(match_score * 100, 1)
            c_name = str(match_row.get("PLAYER", ""))
            c_team = str(match_row.get("TEAM", ""))
            c_conf = str(match_row.get("CONF", ""))
            c_ht   = str(match_row.get("HEIGHT", ""))
            c_cls  = str(match_row.get("CLASS", ""))
            c_bpm  = float(match_row.get("BPM", 0))
            c_ortg = float(match_row.get("ORTG", 0))
            c_usg  = float(match_row.get("USG", 0))
            c_ts   = float(match_row.get("TS", 0))
            c_3p   = float(match_row.get("THREE_P", 0))
            bar_color = "#2774AE" if pct >= 75 else "#F0B429" if pct >= 60 else "#DC2626"

            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e293b;border-left:4px solid {bar_color};"
                f"border-radius:8px;padding:14px 16px;margin-bottom:10px;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;'>"
                f"<div><div style='font-size:16px;font-weight:800;color:#FFFFFF;'>{c_name}</div>"
                f"<div style='font-size:11px;color:#64748b;margin-top:2px;'>{c_ht} &nbsp;·&nbsp; {c_cls} &nbsp;·&nbsp; {c_team} ({c_conf})</div></div>"
                f"<span style='font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;"
                f"background:{bar_color}22;color:{bar_color};border:1px solid {bar_color}55;'>{pct}% match</span>"
                f"</div>"
                f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:8px;'>"
                f"<div style='text-align:center;background:#1e293b;border-radius:4px;padding:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#fff;'>{c_bpm:.1f}</div>"
                f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;'>BPM</div></div>"
                f"<div style='text-align:center;background:#1e293b;border-radius:4px;padding:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#fff;'>{c_ortg:.0f}</div>"
                f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;'>ORTG</div></div>"
                f"<div style='text-align:center;background:#1e293b;border-radius:4px;padding:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#fff;'>{c_ts:.1f}%</div>"
                f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;'>TS%</div></div>"
                f"<div style='text-align:center;background:#1e293b;border-radius:4px;padding:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#fff;'>{c_usg:.1f}%</div>"
                f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;'>USG%</div></div>"
                f"<div style='text-align:center;background:#1e293b;border-radius:4px;padding:6px;'>"
                f"<div style='font-size:13px;font-weight:600;color:#fff;'>{c_3p:.1f}%</div>"
                f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;'>3P%</div></div>"
                f"</div>"
                f"<div style='height:4px;background:#1e293b;border-radius:2px;'>"
                f"<div style='height:100%;width:{min(pct,100)}%;background:{bar_color};border-radius:2px;'></div>"
                f"</div></div>",
                unsafe_allow_html=True
            )


# ==========================================
# TAB 2: PORTAL DISCOVERY ENGINE
# ==========================================
with tab2:
    st.subheader("Database Sifting & Portal Filtering")

    disc_base_df = df_all

    with st.expander("Advanced Database Filters", expanded=False):
        st.write("Adjust parameters to filter the active portal pool.")

        col_cat1, col_cat2, col_cat3 = st.columns(3)
        with col_cat1:
            conf_options = sorted(list(df_all["CONF"].unique()))
            selected_confs = st.multiselect("Filter by Conference:", conf_options)
        with col_cat2:
            team_options = sorted(list(df_all["TEAM"].unique()))
            selected_teams = st.multiselect("Filter by Program / Team:", team_options)
        with col_cat3:
            class_options = sorted(list(df_all["CLASS"].dropna().unique()))
            selected_classes = st.multiselect("Filter by Class / Eligibility:", class_options)

        st.write("**Statistical Range Bounds**")
        f1, f2, f3, f4 = st.columns(4)

        with f1:
            st.markdown("**Volume & Impact**")
            min_pct = st.slider("Min %",     0.0, 100.0, (0.0, 100.0), step=1.0)
            usg     = st.slider("Usage %",   0.0,  50.0, (0.0,  50.0), step=1.0)
            bpm     = st.slider("Box BPM",  -20.0, 30.0, (-20.0, 30.0), step=0.5)
            obpm    = st.slider("Off. BPM", -20.0, 30.0, (-20.0, 30.0), step=0.5)
            dbpm    = st.slider("Def. BPM", -20.0, 20.0, (-20.0, 20.0), step=0.5)

        with f2:
            st.markdown("**Efficiency & Scoring**")
            ortg  = st.slider("O-Rating", 0.0, 150.0, (0.0, 150.0), step=1.0)
            efg   = st.slider("eFG %",    0.0, 100.0, (0.0, 100.0), step=1.0)
            ts    = st.slider("TS %",     0.0, 100.0, (0.0, 100.0), step=1.0)
            two_p = st.slider("2P %",     0.0, 100.0, (0.0, 100.0), step=1.0)

        with f3:
            st.markdown("**Shooting & Frequency**")
            three_p     = st.slider("3P %",                0.0, 100.0, (0.0, 100.0), step=1.0)
            three_p_100 = st.slider("3PA/100",              0.0,  30.0, (0.0,  30.0), step=0.5)
            ftr         = st.slider("Free Throw Rate (FTR)", 0.0, 150.0, (0.0, 150.0), step=1.0)

        with f4:
            st.markdown("**Playmaking & Rebounding**")
            ast = st.slider("Ast %",   0.0,  60.0, (0.0,  60.0), step=1.0)
            tov = st.slider("TO %",    0.0, 100.0, (0.0, 100.0), step=1.0)
            orb = st.slider("O-Reb %", 0.0,  50.0, (0.0,  50.0), step=1.0)
            drb = st.slider("D-Reb %", 0.0,  50.0, (0.0,  50.0), step=1.0)
            blk = st.slider("Blk %",   0.0,  30.0, (0.0,  30.0), step=0.5)
            stl = st.slider("Stl %",   0.0,  15.0, (0.0,  15.0), step=0.5)

    filtered_df = disc_base_df.copy()

    if selected_confs:
        filtered_df = filtered_df[filtered_df["CONF"].isin(selected_confs)]
    if selected_teams:
        filtered_df = filtered_df[filtered_df["TEAM"].isin(selected_teams)]
    if selected_classes:
        filtered_df = filtered_df[filtered_df["CLASS"].isin(selected_classes)]

    filtered_df = filtered_df[
        (filtered_df["MIN_PCT"].between(min_pct[0], min_pct[1])) &
        (filtered_df["BPM"].between(bpm[0], bpm[1])) &
        (filtered_df["OBPM"].between(obpm[0], obpm[1])) &
        (filtered_df["DBPM"].between(dbpm[0], dbpm[1])) &
        (filtered_df["ORTG"].between(ortg[0], ortg[1])) &
        (filtered_df["USG"].between(usg[0], usg[1])) &
        (filtered_df["EFG"].between(efg[0], efg[1])) &
        (filtered_df["TS"].between(ts[0], ts[1])) &
        (filtered_df["OR"].between(orb[0], orb[1])) &
        (filtered_df["DR"].between(drb[0], drb[1])) &
        (filtered_df["AST"].between(ast[0], ast[1])) &
        (filtered_df["TO"].between(tov[0], tov[1])) &
        (filtered_df["BLK"].between(blk[0], blk[1])) &
        (filtered_df["STL"].between(stl[0], stl[1])) &
        (filtered_df["FTR"].between(ftr[0], ftr[1])) &
        (filtered_df["TWO_P"].between(two_p[0], two_p[1])) &
        (filtered_df["THREE_P"].between(three_p[0], three_p[1])) &
        (filtered_df["THREE_PA"].between(three_p_100[0], three_p_100[1]))
    ]

    filtered_df = filtered_df.sort_values(by="PRPG", ascending=False)

    ordered_cols = ["PLAYER", "TEAM", "CONF", "CLASS", "HEIGHT", "PRPG", "BPM", "MIN_PCT", "USG", "EFG"]
    remaining_cols = [c for c in filtered_df.columns if c not in ordered_cols]
    filtered_df = filtered_df[ordered_cols + remaining_cols]

    st.write(f"**Filter Results (All Games):** Found {len(filtered_df)} profiles matching criteria.")

    # ---- INLINE PREVIEW (shows when a row is clicked) ----
    if st.session_state.get("disc_selected_player") and st.session_state.disc_selected_player in df_all["PLAYER"].values:
        preview_player = st.session_state.disc_selected_player
        preview_data = df_all[df_all["PLAYER"] == preview_player].iloc[0]

        conn = sqlite3.connect('scouting_hub.db')
        cursor = conn.cursor()
        cursor.execute("SELECT position, photo_url, coach_notes FROM player_notes WHERE player_name = ?", (preview_player,))
        preview_db = cursor.fetchone()
        conn.close()

        preview_pos   = preview_db[0] if preview_db and preview_db[0] else ""
        preview_photo = preview_db[1] if preview_db and preview_db[1] else ""
        preview_notes = preview_db[2] if preview_db and preview_db[2] else ""

        if not preview_photo:
            preview_photo = fetch_sr_headshot_silent(preview_player, preview_data["TEAM"])
            if preview_photo:
                conn = sqlite3.connect('scouting_hub.db')
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO player_notes (player_name, team_name, photo_url)
                    VALUES (?, ?, ?)
                    ON CONFLICT(player_name) DO UPDATE SET photo_url=excluded.photo_url
                ''', (preview_player, preview_data["TEAM"], preview_photo))
                conn.commit()
                conn.close()

        with st.container(border=True):
            ph_col, pi_col = st.columns([1, 4])
            with ph_col:
                if preview_photo:
                    st.image(preview_photo, width=110)
                else:
                    st.markdown(
                        "<div style='width:110px;height:130px;background:#1e293b;border-radius:8px;"
                        "display:flex;align-items:center;justify-content:center;color:#64748b;font-size:11px;'>"
                        "No Photo</div>", unsafe_allow_html=True
                    )
            with pi_col:
                st.markdown(
                    f"<div style='font-size:22px;font-weight:900;color:#FFFFFF;margin-bottom:4px;'>{preview_player}</div>",
                    unsafe_allow_html=True
                )
                st.markdown(
                    f"<div style='font-size:12px;color:#94a3b8;'>"
                    f"{preview_data['TEAM']} &nbsp;·&nbsp; {preview_data['CONF']} &nbsp;·&nbsp; "
                    f"{preview_data['CLASS']} &nbsp;·&nbsp; {preview_data['HEIGHT']}</div>",
                    unsafe_allow_html=True
                )
# TAB 2 CONTINUED: dataframe + click handler
# ==========================================
    st.write(f"**Filter Results (All Games):** Found {len(filtered_df)} profiles matching criteria.")

    if st.session_state.get("disc_selected_player") and st.session_state.disc_selected_player in df_all["PLAYER"].values:
        preview_player = st.session_state.disc_selected_player
        preview_data = df_all[df_all["PLAYER"] == preview_player].iloc[0]
        conn = sqlite3.connect('scouting_hub.db')
        cursor = conn.cursor()
        cursor.execute("SELECT position, photo_url, coach_notes FROM player_notes WHERE player_name = ?", (preview_player,))
        preview_db = cursor.fetchone()
        conn.close()
        preview_pos   = preview_db[0] if preview_db and preview_db[0] else ""
        preview_photo = preview_db[1] if preview_db and preview_db[1] else ""
        preview_notes = preview_db[2] if preview_db and preview_db[2] else ""
        if not preview_photo:
            preview_photo = fetch_sr_headshot_silent(preview_player, preview_data["TEAM"])
        with st.container(border=True):
            ph_col, pi_col = st.columns([1, 4])
            with ph_col:
                if preview_photo:
                    st.image(preview_photo, width=110)
            with pi_col:
                st.markdown(f"<div style='font-size:20px;font-weight:900;color:#FFFFFF;'>{preview_player}</div>", unsafe_allow_html=True)
                st.markdown(f"<div style='font-size:12px;color:#94a3b8;'>{preview_data['TEAM']} · {preview_data['CONF']} · {preview_data['CLASS']} · {preview_data['HEIGHT']}</div>", unsafe_allow_html=True)
            st.write("")
            pc1, pc2, pc3, pc4, pc5, pc6 = st.columns(6)
            pc1.metric("PPG",  f"{preview_data.get('PPG', 0.0):.1f}")
            pc2.metric("RPG",  f"{preview_data.get('RPG', 0.0):.1f}")
            pc3.metric("APG",  f"{preview_data.get('APG', 0.0):.1f}")
            pc4.metric("STL%", f"{preview_data.get('STL', 0.0):.1f}%")
            pc5.metric("BLK%", f"{preview_data.get('BLK', 0.0):.1f}%")
            pc6.metric("GP",   int(preview_data.get('GP', 0)))

            pg = detect_pos_group(preview_data.get("TORVIK_POS",""), preview_pos, preview_data.get("HEIGHT",""), preview_data.get("AST",0))
            pg = st.radio("Position:", ["G","F","C"], index=["G","F","C"].index(pg), horizontal=True, key="disc_pos_radio_top")
            p_ortg = preview_data.get("ORTG",0.0); p_apg = preview_data.get("APG",0.0)
            p_to = preview_data.get("TO",0.0); p_stl = preview_data.get("STL",0.0)
            p_blk = preview_data.get("BLK",0.0); p_ast = preview_data.get("AST",0.0)
            p_or = preview_data.get("OR",0.0); p_dr = preview_data.get("DR",0.0)
            p_bpm = preview_data.get("BPM",0.0); p_min = preview_data.get("MIN_PCT",0.0)
            p_ato = round(p_ast / p_to, 2) if p_to and p_to > 0 else 0.0
            if pg == "G":
                g1,g2,g3,g4,g5 = st.columns(5)
                g1.metric("MIN%", f"{p_min:.1f}%"); g2.metric("ORTG", f"{p_ortg:.1f}")
                g3.metric("A/TO", f"{p_ato:.2f}"); g4.metric("TOV%", f"{p_to:.1f}%"); g5.metric("STL%", f"{p_stl:.1f}%")
            elif pg == "F":
                f1,f2,f3,f4 = st.columns(4)
                f1.metric("BPM", f"{p_bpm:.1f}"); f2.metric("BLK%", f"{p_blk:.1f}%")
                f3.metric("DREB%", f"{p_dr:.1f}%"); f4.metric("OREB%", f"{p_or:.1f}%")
            elif pg == "C":
                c1,c2,c3,c4,c5 = st.columns(5)
                c1.metric("ORTG", f"{p_ortg:.1f}"); c2.metric("OREB%", f"{p_or:.1f}%")
                c3.metric("DREB%", f"{p_dr:.1f}%"); c4.metric("TO%", f"{p_to:.1f}%"); c5.metric("BLK%", f"{p_blk:.1f}%")
            st.markdown("**Coach Notes**")
            disc_notes = st.text_area("Notes:", value=preview_notes, height=100, key="disc_coach_notes")
            if st.button("Save Notes", key="disc_save_notes", type="primary"):
                conn = sqlite3.connect('scouting_hub.db')
                cursor = conn.cursor()
                cursor.execute('''INSERT INTO player_notes (player_name, team_name, coach_notes)
                    VALUES (?, ?, ?) ON CONFLICT(player_name) DO UPDATE SET coach_notes=excluded.coach_notes''',
                    (preview_player, preview_data["TEAM"], disc_notes))
                conn.commit(); conn.close()
                st.success(f"Notes saved for {preview_player}."); st.rerun()

    event_discovery = st.dataframe(filtered_df, hide_index=True, on_select="rerun", selection_mode="single-row", height=650)
    if event_discovery.selection.rows:
        clicked_idx = event_discovery.selection.rows[0]
        clicked_player = filtered_df.iloc[clicked_idx]["PLAYER"]
        if st.session_state.get("disc_selected_player") != clicked_player:
            st.session_state.disc_selected_player = clicked_player
            st.session_state.active_player = clicked_player
            st.rerun()


# ==========================================
# TAB 3: FRONT OFFICE TARGET BOARD
# ==========================================
with tab3:
    st.subheader("Central Board Records")
    conn = sqlite3.connect('scouting_hub.db')
    db_df = pd.read_sql_query('''
        SELECT player_name AS PLAYER, team_name AS TEAM, position AS POS, role AS ROLE,
               agent AS AGENT, agency AS AGENCY, rumored_nil AS [RUMORED NIL],
               personal_val AS [OUR VALUE], eval_date AS [LOG DATE],
               scout_name AS SCOUT, notes AS NOTES, priority_tier AS TIER
        FROM player_notes
    ''', conn)
    conn.close()
    if db_df.empty:
        st.info("No targets currently logged.")
    else:
        for tier in ["High Priority", "Watchlist", "Pass"]:
            st.markdown(f"### {tier}")
            tier_filtered = db_df[db_df["TIER"] == tier]
            if tier_filtered.empty:
                st.write("*No targets assigned.*")
            else:
                event_board = st.dataframe(tier_filtered.drop(columns=["TIER"]), hide_index=True,
                                           on_select="rerun", selection_mode="single-row", key=f"board_{tier}")
                if event_board.selection.rows:
                    clicked_idx = event_board.selection.rows[0]
                    clicked_player = tier_filtered.iloc[clicked_idx]["PLAYER"]
                    if st.session_state.active_player != clicked_player:
                        st.session_state.active_player = clicked_player
                        st.rerun()


# ==========================================
# TAB 4: BIG BOARD PRINT VIEW
# ==========================================
with tab4:
    st.subheader("Staff Roster Print Layout")
    filter_tier = st.selectbox("Select Tier:", ["High Priority", "Watchlist", "All Records"])
    conn = sqlite3.connect('scouting_hub.db')
    if filter_tier == "All Records":
        board_data = pd.read_sql_query("SELECT * FROM player_notes", conn)
    else:
        board_data = pd.read_sql_query("SELECT * FROM player_notes WHERE priority_tier = ?", conn, params=(filter_tier,))
    conn.close()
    if board_data.empty:
        st.warning("No records match.")
    else:
        pos_columns = ["PG", "CG", "W", "F", "C"]
        st_cols = st.columns(5)
        for i, pos_group in enumerate(pos_columns):
            with st_cols[i]:
                st.markdown(f"<div style='background:#1E3A8A;color:white;font-weight:bold;text-align:center;padding:6px;border-radius:4px;margin-bottom:12px;'>{pos_group}</div>", unsafe_allow_html=True)
                group_players = board_data[board_data["position"] == pos_group]
                if group_players.empty:
                    st.caption("No targets")
                else:
                    for _, player in group_players.iterrows():
                        p_name = player["player_name"]
                        stat_match = df_all[df_all["PLAYER"] == p_name]
                        if not stat_match.empty:
                            s = stat_match.iloc[0]
                            stat_line = f"BPM: {s['BPM']:.1f} | USG: {s['USG']:.0f}% | eFG: {s['EFG']:.0f}%"
                            meta_line = f"{s['HEIGHT']} | {s['CLASS']}"
                        else:
                            stat_line = "No stats linked"; meta_line = "N/A"
                        photo = player["photo_url"] if player["photo_url"] else "https://via.placeholder.com/150"
                        role_label = player["role"] if player["role"] else "Unassigned"
                        team_name = player["team_name"]
                        st.markdown(
                            f"<div style='border:1px solid #CBD5E1;border-radius:6px;padding:10px;margin-bottom:12px;background:#FFFFFF;'>"
                            f"<div style='font-size:14px;font-weight:bold;color:#0F172A;'>{p_name}</div>"
                            f"<div style='font-size:11px;color:#475569;'>{team_name}</div>"
                            f"<div style='font-size:11px;color:#64748B;'>{meta_line}</div>"
                            f"<div style='font-size:10px;color:#1E40AF;margin-top:4px;'>🎯 {role_label}</div>"
                            f"<div style='font-size:9.5px;color:#475569;'>📊 {stat_line}</div>"
                            f"</div>", unsafe_allow_html=True)


# ==========================================
# TAB 5: PLAYER CARD
# ==========================================
with tab5:
    st.subheader("Player Card")

    card_idx = all_player_names.index(st.session_state.active_player)
    card_selected = st.selectbox("Select player:", all_player_names, index=card_idx, key="card_selector")
    if card_selected != st.session_state.active_player:
        st.session_state.active_player = card_selected

    card_player = card_selected
    card_data = df_all[df_all["PLAYER"] == card_player].iloc[0]

    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()
    cursor.execute("SELECT position, photo_url, coach_notes FROM player_notes WHERE player_name = ?", (card_player,))
    card_db = cursor.fetchone()
    conn.close()
    card_pos   = card_db[0] if card_db and card_db[0] else ""
    card_photo = card_db[1] if card_db and card_db[1] else ""
    saved_coach_notes = card_db[2] if card_db and card_db[2] else ""

    if not card_photo:
        card_photo = fetch_sr_headshot_silent(card_player, card_data["TEAM"])
        if card_photo:
            conn = sqlite3.connect('scouting_hub.db')
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO player_notes (player_name, team_name, photo_url)
                VALUES (?, ?, ?) ON CONFLICT(player_name) DO UPDATE SET photo_url=excluded.photo_url''',
                (card_player, card_data["TEAM"], card_photo))
            conn.commit(); conn.close()

    # Header
    st.markdown("---")
    col_img, col_info = st.columns([1, 5])
    with col_img:
        if card_photo:
            st.image(card_photo, width=130)
        else:
            st.info("No photo")
    with col_info:
        st.markdown(f"<div style='font-size:26px;font-weight:900;color:#FFFFFF;margin-bottom:4px;'>{card_player}</div>", unsafe_allow_html=True)
        st.markdown(f"<div style='font-size:13px;color:#94a3b8;'>{card_data['TEAM']} · {card_data['CONF']} · {card_data['CLASS']} · {card_data['HEIGHT']}</div>", unsafe_allow_html=True)

    st.markdown("---")

    # Core stats - all from Torvik
    st.markdown("**Core Stats**")

    gp      = int(card_data.get("GP", 0))
    ppg     = float(card_data.get("PPG", 0.0))
    rpg     = float(card_data.get("RPG", 0.0))
    apg     = float(card_data.get("APG", 0.0))
    stl_pct = float(card_data.get("STL", 0.0))
    blk_pct = float(card_data.get("BLK", 0.0))
    two      = card_data.get("TWO_P", 0.0)
    three    = card_data.get("THREE_P", 0.0)
    three_pa = card_data.get("THREE_PA", 0.0)
    min_pct  = card_data.get("MIN_PCT", 0.0)
    ts       = card_data.get("TS", 0.0)

    r1c1, r1c2, r1c3, r1c4, r1c5, r1c6 = st.columns(6)
    r1c1.metric("PPG",  f"{ppg:.1f}")
    r1c2.metric("RPG",  f"{rpg:.1f}")
    r1c3.metric("APG",  f"{apg:.1f}")
    r1c4.metric("STL%", f"{stl_pct:.1f}%")
    r1c5.metric("BLK%", f"{blk_pct:.1f}%")
    r1c6.metric("GP",   gp)

    r2c1, r2c2, r2c3, r2c4, r2c5 = st.columns(5)
    r2c1.metric("MIN%", f"{min_pct:.1f}%")
    r2c2.metric("TS%",  f"{ts:.1f}%")
    r2c3.metric("2PT%", f"{two:.1f}%")
    r2c4.metric("3PT%", f"{three:.1f}%")
    r2c5.metric("3PA",  f"{three_pa:.1f}")

    st.markdown("---")

    # Position-specific stats (no overlap with core stats above)
    auto_pos = detect_pos_group(card_data.get("TORVIK_POS",""), card_pos, card_data.get("HEIGHT",""), card_data.get("AST",0))
    pos_group = st.radio("Position group:", ["G","F","C"], index=["G","F","C"].index(auto_pos), horizontal=True, key="card_pos_group_radio")

    ortg    = card_data.get("ORTG", 0.0)
    to_pct  = card_data.get("TO", 0.0)
    stl_pct = card_data.get("STL", 0.0)
    blk_pct = card_data.get("BLK", 0.0)
    ast_pct = card_data.get("AST", 0.0)
    orb_pct = card_data.get("OR", 0.0)
    drb_pct = card_data.get("DR", 0.0)
    bpm_val = card_data.get("BPM", 0.0)
    ato     = round(ast_pct / to_pct, 2) if to_pct and to_pct > 0 else 0.0

    if pos_group == "G":
        st.markdown("**Guard Stats**")
        g1, g2, g3, g4, g5 = st.columns(5)
        g1.metric("MIN%", f"{min_pct:.1f}%"); g2.metric("ORTG", f"{ortg:.1f}")
        g3.metric("A/TO", f"{ato:.2f}"); g4.metric("TOV%", f"{to_pct:.1f}%"); g5.metric("STL%", f"{stl_pct:.1f}%")
    elif pos_group == "F":
        st.markdown("**Forward / Wing Stats**")
        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric("BPM", f"{bpm_val:.1f}"); f2.metric("STL%", f"{stl_pct:.1f}%")
        f3.metric("BLK%", f"{blk_pct:.1f}%"); f4.metric("DREB%", f"{drb_pct:.1f}%"); f5.metric("OREB%", f"{orb_pct:.1f}%")
    elif pos_group == "C":
        st.markdown("**Center Stats**")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("ORTG", f"{ortg:.1f}"); c2.metric("OREB%", f"{orb_pct:.1f}%")
        c3.metric("DREB%", f"{drb_pct:.1f}%"); c4.metric("TO%", f"{to_pct:.1f}%"); c5.metric("BLK%", f"{blk_pct:.1f}%")

    st.markdown("---")

    # Coach Notes
    st.markdown("**Coach Notes**")
    coach_notes_input = st.text_area("Notes:", value=saved_coach_notes, height=140,
        placeholder="Add intel, impressions, fit evaluation...", key="coach_notes_area")
    if st.button("Save Coach Notes", type="primary"):
        conn = sqlite3.connect('scouting_hub.db')
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO player_notes (player_name, team_name, coach_notes)
            VALUES (?, ?, ?) ON CONFLICT(player_name) DO UPDATE SET coach_notes=excluded.coach_notes''',
            (card_player, card_data["TEAM"], coach_notes_input))
        conn.commit(); conn.close()
        st.success(f"Notes saved for {card_player}.")
        st.rerun()

    st.markdown("<style>@media print { header, footer, [data-testid='stSidebar'], [data-testid='stToolbar'], .stTabs [role='tablist'], .stSelectbox, .stRadio, .stButton, .stTextArea, .stCaption { display: none !important; } [data-testid='stAppViewContainer'] { padding: 0 !important; } }</style>", unsafe_allow_html=True)
    st.caption("File → Print to print this card clean.")
