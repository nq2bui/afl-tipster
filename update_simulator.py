"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from:
  1. AFL CFS API (api.afl.com.au) — official match rosters, outs (injured + managed)
  2. SuperCoach public API — player ratings only (previous season avg scores)
  3. Squiggle API — round detection, ladder standings
Patches index.html automatically.
"""

import json as _json
import requests
import re
import statistics
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
SQUIGGLE_BASE    = "https://api.squiggle.com.au/"
SQUIGGLE_HEADERS = {"User-Agent": "AFL-Tipster-Bot/1.0 (github.com/your-username/afl-tipster)"}
YEAR             = 2026
COMP_SEASON_ID   = 85  # 2026 Toyota AFL Premiership

AFL_API_BASE = "https://aflapi.afl.com.au/afl/v2"
CFS_API_BASE = "https://api.afl.com.au/cfs/afl"
CFS_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     "https://www.afl.com.au",
    "Referer":    "https://www.afl.com.au/",
}

# SuperCoach public API — no auth required (ratings only)
SUPERCOACH_URL = (
    "https://supercoach.com.au/2026/api/afl/classic/v1/players-cf"
    "?embed=positions,team"
)

# Map Squiggle team names → our short codes
SQUIGGLE_TEAM_MAP = {
    "Adelaide": "ADE", "Brisbane Lions": "BRI", "Carlton": "CAR",
    "Collingwood": "COL", "Essendon": "ESS", "Fremantle": "FRE",
    "Geelong": "GEE", "Gold Coast": "GCS", "Greater Western Sydney": "GWS",
    "GWS Giants": "GWS", "Hawthorn": "HAW", "Melbourne": "MEL",
    "North Melbourne": "NTH", "Port Adelaide": "POR", "Richmond": "RIC",
    "St Kilda": "STK", "Sydney": "SYD", "Sydney Swans": "SYD",
    "West Coast": "WCE", "Western Bulldogs": "WBD",
}

# AFL CFS API team names → our short codes
CFS_TEAM_MAP = {
    "Adelaide Crows": "ADE", "Brisbane Lions": "BRI",
    "Carlton": "CAR", "Carlton Blues": "CAR",
    "Collingwood": "COL", "Collingwood Magpies": "COL",
    "Essendon": "ESS", "Essendon Bombers": "ESS",
    "Fremantle": "FRE", "Fremantle Dockers": "FRE",
    "Geelong Cats": "GEE",
    "Gold Coast SUNS": "GCS", "Gold Coast Suns": "GCS",
    "GWS GIANTS": "GWS", "Greater Western Sydney": "GWS", "GWS Giants": "GWS",
    "Hawthorn": "HAW", "Hawthorn Hawks": "HAW",
    "Melbourne": "MEL", "Melbourne Demons": "MEL",
    "North Melbourne": "NTH", "North Melbourne Kangaroos": "NTH",
    "Port Adelaide": "POR", "Port Adelaide Power": "POR",
    "Richmond": "RIC", "Richmond Tigers": "RIC",
    "St Kilda": "STK", "St Kilda Saints": "STK",
    "Sydney Swans": "SYD", "Sydney": "SYD",
    "Western Bulldogs": "WBD",
    "West Coast Eagles": "WCE", "West Coast": "WCE",
    # Indigenous team names
    "Kuwarna": "ADE",
    "Walyalup": "FRE",
    "Euro-Yroke": "STK",
    "Waalitj Marawar": "WCE",
    "Yartapuulti": "POR",
    "Narrm": "MEL",
}

# SuperCoach abbreviations → our short codes (ratings only)
SC_ABBREV_MAP = {
    "ADE": "ADE", "BRL": "BRI", "CAR": "CAR", "COL": "COL",
    "ESS": "ESS", "FRE": "FRE", "GCS": "GCS", "GEE": "GEE",
    "GWS": "GWS", "HAW": "HAW", "MEL": "MEL", "NTH": "NTH",
    "PTA": "POR", "RIC": "RIC", "STK": "STK", "SYD": "SYD",
    "WBD": "WBD", "WCE": "WCE",
}


# ── SUPERCOACH RATINGS ───────────────────────────────────────────────────────

def build_position_stats(players, min_games=5):
    """Compute mean and stdev of SC avg score per position from the full player pool."""
    from collections import defaultdict
    buckets = defaultdict(list)
    for p in players:
        avg   = p.get("previous_average") or 0
        games = p.get("previous_games") or 0
        pos   = (p.get("positions") or [{}])[0].get("position", "")
        if avg and games >= min_games and pos in ("MID", "FWD", "DEF", "RUC"):
            buckets[pos].append(avg)

    pos_stats = {}
    for pos, avgs in buckets.items():
        if len(avgs) >= 5:
            pos_stats[pos] = {
                "mean":  statistics.mean(avgs),
                "stdev": statistics.pstdev(avgs) or 1.0,
            }
            print(f"  Position stats  {pos}: mean={pos_stats[pos]['mean']:.1f}  "
                  f"stdev={pos_stats[pos]['stdev']:.1f}  n={len(avgs)}")
    return pos_stats


def sc_to_rating(avg, games, position, pos_stats, min_games=2):
    """Convert SC avg score to a 50–99 simulator rating using positional normalisation."""
    if not avg or not games or games < min_games:
        return None

    pos_key = "RUC" if position in ("RUC", "RCK") else position
    stats = pos_stats.get(pos_key)
    if stats:
        z = (avg - stats["mean"]) / stats["stdev"]
        return max(50, min(99, round(74 + z * 10)))

    t = max(0.0, min(1.0, (avg - 40.0) / 90.0))
    return max(50, min(99, round(50 + t * 49)))


def fetch_supercoach_ratings():
    """Fetch player ratings from SuperCoach API.

    Returns {(team_code, surname): rating} — positionally-normalised 50–99 ratings
    derived from previous season avg scores. Injuries are NOT used from this source.
    """
    try:
        r = requests.get(SUPERCOACH_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }, timeout=15)
        r.raise_for_status()
        players = r.json()
    except Exception as e:
        print(f"  Could not fetch SuperCoach data: {e}")
        return {}

    print("  Computing position benchmarks...")
    pos_stats = build_position_stats(players)

    ratings_map = {}
    for p in players:
        sc_abbrev = p.get("team", {}).get("abbrev", "")
        team_code = SC_ABBREV_MAP.get(sc_abbrev)
        if not team_code:
            continue

        surname  = p.get("last_name", "").strip()
        avg      = p.get("previous_average") or 0
        games    = p.get("previous_games") or 0
        position = (p.get("positions") or [{}])[0].get("position", "")

        rating = sc_to_rating(avg, games, position, pos_stats)
        if rating and surname:
            ratings_map[(team_code, surname)] = rating

    print(f"  SuperCoach: {len(ratings_map)} player ratings loaded")
    return ratings_map


# ── AFL CFS API — PLAYER HEIGHTS ─────────────────────────────────────────────

def fetch_player_heights(html):
    """Fetch player heights (cm) from the AFL CFS playerProfile API.

    Extracts feed_ids from the PLAYER_IMGS block in index.html, maps them to
    tracked players in the TEAMS block, then fetches heightInCm from each
    player's profile.

    Returns {(team_code, surname): height_cm}
    """
    import time

    # Extract (team, name) -> feed_id from PLAYER_IMGS
    img_pattern = r'"([A-Z]{3}):([^"]+)":"[^"]*?/(\d+)\.png'
    img_map = {}
    for team, name, fid in re.findall(img_pattern, html):
        img_map[(team, name)] = fid

    # Extract tracked players from TEAMS
    team_pattern = r'([A-Z]{3}):\{name:"[^"]+"[^[]+players:\[([^\]]+)\]'
    player_pat = r'\{n:"([^"]+)",r:\d+,pos:"[A-Z]+",ht:\d+,out:(?:true|false)\}'

    players = []
    for team_code, players_str in re.findall(team_pattern, html):
        for m in re.finditer(player_pat, players_str):
            name = m.group(1)
            fid = img_map.get((team_code, name))
            if fid:
                players.append((team_code, name, fid))

    if not players:
        print("  No players found to fetch heights for")
        return {}

    # Get WMCTok
    token = get_wmctoken()
    if not token:
        print("  Cannot fetch heights without WMCTok")
        return {}

    headers = {**CFS_HEADERS, "x-media-mis-token": token}
    heights = {}

    for i, (team, name, fid) in enumerate(players):
        try:
            r = requests.get(f"{CFS_API_BASE}/playerProfile/CD_I{fid}",
                             headers=headers, timeout=10)
            if r.status_code == 200:
                ht = r.json().get("playerProfile", {}).get("basicStats", {}).get("heightInCm", 0)
                if ht:
                    surname = name.split(". ", 1)[-1] if ". " in name else name
                    heights[(team, surname)] = ht
        except Exception as e:
            pass
        if (i + 1) % 20 == 0:
            time.sleep(0.3)

    print(f"  Fetched heights for {len(heights)} players")
    return heights


# ── AFL CFS API — MATCH ROSTERS ──────────────────────────────────────────────

def get_wmctoken():
    """Fetch the WMCTok auth token from the AFL CFS API."""
    try:
        r = requests.post(f"{CFS_API_BASE}/WMCTok", headers=CFS_HEADERS, timeout=10)
        r.raise_for_status()
        token = r.json().get("token")
        if token:
            print(f"  WMCTok obtained successfully")
            return token
        print("  WMCTok response missing token field")
        return None
    except Exception as e:
        print(f"  Could not fetch WMCTok: {e}")
        return None


def get_match_provider_ids(round_num):
    """Fetch AFL match providerIds for the given round from the AFL API.

    Returns a list of strings like ['CD_M20260140301', 'CD_M20260140302', ...]
    """
    try:
        r = requests.get(
            f"{AFL_API_BASE}/matches",
            params={"competitionId": 1, "compSeasonId": COMP_SEASON_ID, "roundNumber": round_num},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        matches = r.json().get("matches", [])
        ids = [m["providerId"] for m in matches if m.get("providerId")]
        print(f"  Found {len(ids)} matches for round {round_num}")
        return ids
    except Exception as e:
        print(f"  Could not fetch match IDs from AFL API: {e}")
        return []


def fetch_afl_lineups(round_num):
    """Fetch official player outs for all matches in the round from the AFL CFS API.

    Uses the WMCTok auth token and the matchRoster/full endpoint.
    Captures both injured AND managed players — the source of truth for who is out.

    Returns {team_code: [surnames]}
    """
    token = get_wmctoken()
    if not token:
        print("  Cannot fetch lineups without WMCTok")
        return {}

    provider_ids = get_match_provider_ids(round_num)
    if not provider_ids:
        print("  No match IDs found — cannot fetch lineups")
        return {}

    headers = {**CFS_HEADERS, "x-media-mis-token": token}
    injured_map = {}
    total_outs = 0

    for pid in provider_ids:
        try:
            r = requests.get(f"{CFS_API_BASE}/matchRoster/full/{pid}", headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"  {pid}: HTTP {r.status_code} — skipping")
                continue

            roster = r.json().get("matchRoster", {})
            match_status = roster.get("status", "UNKNOWN")

            for side in ("homeTeam", "awayTeam"):
                team_data = roster.get(side, {})
                team_name = team_data.get("teamName", {}).get("teamName", "")
                code = CFS_TEAM_MAP.get(team_name)
                if not code:
                    print(f"  Unmapped team name: '{team_name}'")
                    continue

                outs = team_data.get("outs", [])
                for entry in outs:
                    surname = entry.get("player", {}).get("playerName", {}).get("surname", "")
                    reason  = entry.get("reason") or "Unknown"
                    if not surname:
                        continue
                    injured_map.setdefault(code, [])
                    if surname not in injured_map[code]:
                        injured_map[code].append(surname)
                        total_outs += 1
                        print(f"  OUT [{code}] {surname} — {reason}")

        except Exception as e:
            print(f"  Error fetching roster for {pid}: {e}")

    print(f"\n  AFL CFS API: {total_outs} players OUT across {len(injured_map)} teams")
    return injured_map


# ── SQUIGGLE ─────────────────────────────────────────────────────────────────

def squiggle_get(query_params):
    """Fetch data from the Squiggle API."""
    try:
        # Build URL directly — requests.params encodes semicolons/equals which Squiggle rejects
        url = f"{SQUIGGLE_BASE}?q={query_params}"
        r = requests.get(url, headers=SQUIGGLE_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Squiggle API error: {e}")
        return None


def get_current_round():
    """Determine the current/upcoming round from Squiggle, with AFL API fallback."""
    data = squiggle_get(f"games;year={YEAR}")
    if data and "games" in data:
        games    = data["games"]
        upcoming = [g for g in games if g.get("complete", 100) < 100]
        if upcoming:
            upcoming.sort(key=lambda g: g.get("date", "9999"))
            next_round  = upcoming[0].get("round")
            round_games = [g for g in upcoming if g.get("round") == next_round]
            print(f"Detected upcoming round: {next_round} ({len(round_games)} games)")
            return next_round, round_games

    # Squiggle unavailable — fall back to AFL API
    print("  Squiggle unavailable — falling back to AFL API for round detection...")
    try:
        r = requests.get(
            f"{AFL_API_BASE}/matches",
            params={"competitionId": 1, "compSeasonId": COMP_SEASON_ID, "pageSize": 50},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        matches  = r.json().get("matches", [])
        now      = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        upcoming = [m for m in matches if (m.get("status", "") not in ("CONCLUDED", "COMPLETE")
                                           and m.get("utcStartTime", "9999") >= now[:10])]
        if not upcoming:
            print("  No upcoming matches found via AFL API")
            return None, None
        upcoming.sort(key=lambda m: m.get("utcStartTime", "9999"))
        next_round = upcoming[0].get("round", {}).get("roundNumber")
        print(f"  AFL API: detected round {next_round}")
        return next_round, upcoming
    except Exception as e:
        print(f"  AFL API round detection failed: {e}")
        return None, None


def get_standings():
    """Fetch current ladder/standings from Squiggle."""
    data = squiggle_get(f"standings;year={YEAR}")
    if not data or "standings" not in data:
        return {}
    standings = {}
    for team in data["standings"]:
        short = SQUIGGLE_TEAM_MAP.get(team.get("name", ""), None)
        if short:
            standings[short] = {
                "wins":       team.get("wins", 0),
                "losses":     team.get("losses", 0),
                "percentage": round(team.get("percentage", 100), 1),
                "rank":       team.get("rank", 9),
            }
    return standings


# ── ODDS ─────────────────────────────────────────────────────────────────────

def fetch_sportsbet_odds():
    """Fetch H2H match-winner odds for upcoming AFL matches via The Odds API."""
    import os

    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        print("  ODDS_API_KEY not set — skipping odds fetch")
        return {}

    ODDS_TEAM_MAP = {
        "Adelaide Crows": "ADE", "Brisbane Lions": "BRI",
        "Carlton": "CAR", "Carlton Blues": "CAR",
        "Collingwood": "COL", "Collingwood Magpies": "COL",
        "Essendon": "ESS", "Essendon Bombers": "ESS",
        "Fremantle": "FRE", "Fremantle Dockers": "FRE",
        "Geelong Cats": "GEE", "Gold Coast Suns": "GCS",
        "Greater Western Sydney": "GWS", "GWS Giants": "GWS",
        "Greater Western Sydney Giants": "GWS",
        "Hawthorn": "HAW", "Hawthorn Hawks": "HAW",
        "Melbourne": "MEL", "Melbourne Demons": "MEL",
        "North Melbourne": "NTH", "North Melbourne Kangaroos": "NTH",
        "Port Adelaide": "POR", "Port Adelaide Power": "POR",
        "Richmond": "RIC", "Richmond Tigers": "RIC",
        "St Kilda": "STK", "St Kilda Saints": "STK",
        "Sydney Swans": "SYD", "Western Bulldogs": "WBD",
        "West Coast Eagles": "WCE",
    }

    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/aussierules_afl/odds/",
            params={"apiKey": api_key, "regions": "au", "markets": "h2h",
                    "bookmakers": "sportsbet", "oddsFormat": "decimal"},
            timeout=15,
        )
        r.raise_for_status()
        games = r.json()
    except Exception as e:
        print(f"  Odds API fetch error: {e}")
        return {}

    remaining = r.headers.get("x-requests-remaining", "?")
    print(f"  Odds API: {len(games)} games returned  (requests remaining: {remaining})")

    odds_map = {}
    for game in games:
        home_team  = game.get("home_team", "")
        away_team  = game.get("away_team", "")
        home_code  = ODDS_TEAM_MAP.get(home_team)
        away_code  = ODDS_TEAM_MAP.get(away_team)
        if not home_code or not away_code:
            if home_team or away_team:
                print(f"  Unmapped teams: '{home_team}' vs '{away_team}'")
            continue

        for bm in game.get("bookmakers", []):
            if bm.get("key") != "sportsbet":
                continue
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                h_price  = outcomes.get(home_team)
                a_price  = outcomes.get(away_team)
                if h_price and a_price:
                    key = f"{home_code}-{away_code}"
                    odds_map[key] = {"h": round(h_price, 2), "a": round(a_price, 2)}
                    print(f"  {home_code} ${h_price}  vs  {away_code} ${a_price}")
                break

    print(f"  Fetched Sportsbet H2H odds for {len(odds_map)} matches")
    return odds_map


# ── SQUIGGLE RESULTS (for browser-side accuracy tab) ─────────────────────────

def fetch_all_results():
    """Fetch game results for every round from Squiggle (completed + upcoming).

    Returns dict keyed by round string ("0" for Opening Round, "1", "2", etc.)
    with minimal game data needed by the accuracy tab and game times.
    Includes upcoming games (with date but no scores) so game times work.
    """
    data = squiggle_get(f"games;year={YEAR}")
    if not data or "games" not in data:
        print("  Could not fetch game results from Squiggle")
        return {}

    results = {}
    completed = 0
    for g in data["games"]:
        rd = str(g.get("round", ""))
        if not rd:
            continue
        if rd not in results:
            results[rd] = []
        entry = {
            "hteam": g.get("hteam", ""),
            "ateam": g.get("ateam", ""),
            "hscore": g.get("hscore", 0),
            "ascore": g.get("ascore", 0),
            "date": g.get("date", ""),
            "complete": g.get("complete", 0),
        }
        results[rd].append(entry)
        if g.get("complete", 0) == 100:
            completed += 1

    print(f"  Fetched {len(results)} rounds ({completed} completed games, {sum(len(v) for v in results.values()) - completed} upcoming)")
    return results


def fetch_h2h_results():
    """Fetch 5 years of completed game results for head-to-head records.

    Returns list of minimal game objects.
    """
    all_games = []
    for year in range(YEAR - 4, YEAR + 1):
        data = squiggle_get(f"games;year={year}")
        if not data or "games" not in data:
            continue
        for g in data["games"]:
            if g.get("complete", 0) != 100:
                continue
            all_games.append({
                "hteam": g.get("hteam", ""),
                "ateam": g.get("ateam", ""),
                "hscore": g.get("hscore", 0),
                "ascore": g.get("ascore", 0),
                "complete": 100,
            })
    print(f"  Fetched {len(all_games)} completed games across {YEAR-4}–{YEAR}")
    return all_games


def fetch_standings_data():
    """Fetch current ladder standings from Squiggle for browser-side display."""
    data = squiggle_get(f"standings;year={YEAR}")
    if not data or "standings" not in data:
        return []
    standings = []
    for t in data["standings"]:
        standings.append({
            "name": t.get("name", ""),
            "played": t.get("played", 0),
            "wins": t.get("wins", 0),
            "losses": t.get("losses", 0),
            "draws": t.get("draws", 0),
            "pts": t.get("pts", 0),
            "percentage": round(t.get("percentage", 100), 1),
            "rank": t.get("rank", 0),
            "for": t.get("for", 0),
            "against": t.get("against", 0),
        })
    print(f"  Fetched standings for {len(standings)} teams")
    return standings


# ── FIXTURE VALIDATION ───────────────────────────────────────────────────────

def validate_fixture_dates(html, squiggle_results):
    """Cross-check the hand-typed FIXTURE dates/days against live Squiggle data.

    FIXTURE's "venue" strings (e.g. "GMHBA · Thu 2 Jul") are hand-transcribed
    and can drift from the real schedule without anyone noticing until a user
    sees the wrong day on the dashboard (happened for Round 17 2026-07-04).
    Prints a warning per mismatch so it shows up in the Action run log.
    """
    round_pat = re.compile(
        r'"(\w+)":\{label:"[^"]*",dates:"[^"]*"(?:,byes:\[[^\]]*\])?,matches:\[(.*?)\]\},',
        re.DOTALL,
    )
    match_pat = re.compile(r'\{h:"(\w+)",a:"(\w+)",venue:"[^"]*?(\w{3}) (\d{1,2}) (\w{3})"')

    mismatches = 0
    for round_key, body in round_pat.findall(html):
        sq_key = "0" if round_key == "OR" else round_key
        games = squiggle_results.get(sq_key)
        if not games:
            continue

        # Skip rounds where Squiggle hasn't locked in real kickoff times yet —
        # provisional rounds list every game at (near-)identical placeholder
        # times, which would otherwise look like a wall of false positives.
        dates_seen = [g.get("date", "") for g in games if g.get("date")]
        if dates_seen and len(set(dates_seen)) <= max(1, len(dates_seen) // 2):
            continue

        actual_day_by_pair = {}
        for g in games:
            h = SQUIGGLE_TEAM_MAP.get(g.get("hteam") or "")
            a = SQUIGGLE_TEAM_MAP.get(g.get("ateam") or "")
            date_str = g.get("date", "")
            if not h or not a or not date_str:
                continue
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            actual_day_by_pair[(h, a)] = dt.strftime("%a")

        for h, a, day, day_num, month in match_pat.findall(body):
            expected_day = actual_day_by_pair.get((h, a))
            if expected_day and expected_day != day:
                print(f"  WARNING: FIXTURE[\"{round_key}\"] {h} v {a} says {day} {day_num} "
                      f"{month}, Squiggle has it on {expected_day} — check the venue string")
                mismatches += 1

    if mismatches:
        print(f"\n  ⚠ FIXTURE date validation: {mismatches} mismatch(es) found — see warnings above")
    else:
        print("  FIXTURE date validation: all matched rounds check out against Squiggle")


# ── PATCH index.html ─────────────────────────────────────────────────────────

def calculate_form_mult(standings, short):
    """Calculate a form multiplier from standings percentage."""
    if short not in standings:
        return 1.00
    pct    = standings[short].get("percentage", 100)
    wins   = standings[short].get("wins", 0)
    losses = standings[short].get("losses", 0)
    played = wins + losses
    if played == 0:
        return 1.00
    win_rate = wins / played
    base     = 0.88 + (win_rate * 0.24)
    pct_adj  = (pct - 100) * 0.001
    return round(max(0.82, min(1.18, base + pct_adj)), 3)


def patch_index_html(round_num, round_games, standings, injured_map, ratings_map=None, odds_map=None,
                     squiggle_results=None, h2h_games=None, standings_raw=None, heights_map=None):
    """Read index.html, patch TEAMS data and player out flags, then write back."""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # ── 0. Validate FIXTURE dates against live Squiggle data ──
    if squiggle_results:
        validate_fixture_dates(html, squiggle_results)

    # ── 1. Update formMult and ladder standings for each team ──
    for short, s in standings.items():
        new_mult = calculate_form_mult(standings, short)
        pattern  = rf'({re.escape(short)}:{{[^[]*?formMult:)[\d\.]+'
        new_html = re.sub(pattern, rf'\g<1>{new_mult}', html, count=1)
        if new_html != html:
            html = new_html
            print(f"  Updated {short} formMult -> {new_mult}")

        header_pat = re.compile(rf'{re.escape(short)}:{{([^[]+)players:\[', re.DOTALL)
        m = header_pat.search(html)
        if m:
            hdr = m.group(1)
            hdr = re.sub(r'\bwins:\d+',      f'wins:{s["wins"]}',        hdr)
            hdr = re.sub(r'\blosses:\d+',    f'losses:{s["losses"]}',    hdr)
            hdr = re.sub(r'\bpct:[\d.]+',    f'pct:{s["percentage"]}',   hdr)
            hdr = re.sub(r'\brank:\d+',      f'rank:{s["rank"]}',        hdr)
            html = html[:m.start(1)] + hdr + html[m.end(1):]

    # ── 2. Reset all player out flags to false ──
    html = re.sub(r'(,out:)true', r'\1false', html)
    print("  Reset all player out flags to false")

    # ── 3. Mark players as out:true from AFL CFS lineup data ──
    for short, surnames in injured_map.items():
        team_pat = re.compile(
            rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
            re.DOTALL
        )
        for surname in surnames:
            m = team_pat.search(html)
            if not m:
                print(f"  WARNING: Could not locate {short} players section")
                break
            s_start, s_end = m.start(1), m.end(1)
            section    = html[s_start:s_end]
            player_pat = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:\d+,pos:"[A-Z]+",ht:\d+,out:)false'
            new_section = re.sub(player_pat, r'\1true', section, count=1)
            if new_section != section:
                html = html[:s_start] + new_section + html[s_end:]
                print(f"  Marked {short} - {surname} as OUT")
            else:
                print(f"  (no roster match for {short}:{surname})")

    # ── 4. Update player ratings from SuperCoach ──
    if ratings_map:
        updated = 0
        for short in set(k[0] for k in ratings_map):
            team_pat = re.compile(
                rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
                re.DOTALL
            )
            m = team_pat.search(html)
            if not m:
                continue
            s_start, s_end = m.start(1), m.end(1)
            section = html[s_start:s_end]
            changed = False
            for (t_code, surname), rating in ratings_map.items():
                if t_code != short:
                    continue
                pat         = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:)\d+'
                new_section = re.sub(pat, rf'\g<1>{rating}', section, count=1)
                if new_section != section:
                    section = new_section
                    changed = True
                    updated += 1
            if changed:
                html = html[:s_start] + section + html[s_end:]
        print(f"  Updated {updated} player ratings from SuperCoach")

    # ── 4b. Update player heights from AFL CFS API ──
    if heights_map:
        updated = 0
        for short in set(k[0] for k in heights_map):
            team_pat = re.compile(
                rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
                re.DOTALL
            )
            m = team_pat.search(html)
            if not m:
                continue
            s_start, s_end = m.start(1), m.end(1)
            section = html[s_start:s_end]
            changed = False
            for (t_code, surname), height in heights_map.items():
                if t_code != short:
                    continue
                pat = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:\d+,pos:"[A-Z]+",ht:)\d+'
                new_section = re.sub(pat, rf'\g<1>{height}', section, count=1)
                if new_section != section:
                    section = new_section
                    changed = True
                    updated += 1
            if changed:
                html = html[:s_start] + section + html[s_end:]
        print(f"  Updated {updated} player heights from AFL CFS API")

    # ── 5. Patch Sportsbet odds ──
    if odds_map:
        odds_json = _json.dumps(odds_map, separators=(',', ':'))
        new_html  = re.sub(
            r'const SPORTSBET_ODDS = \{[^;]*\};',
            f'const SPORTSBET_ODDS = {odds_json};',
            html,
        )
        if new_html != html:
            html = new_html
            print(f"  Patched SPORTSBET_ODDS with {len(odds_map)} matches")
        else:
            print("  WARNING: SPORTSBET_ODDS pattern not found in index.html")

    # ── 6. Embed Squiggle game results for accuracy tab ──
    if squiggle_results is not None:
        results_json = _json.dumps(squiggle_results, separators=(',', ':'))
        marker = 'const SQUIGGLE_RESULTS = '
        idx = html.find(marker)
        if idx >= 0:
            end_idx = html.index(';', idx) + 1
            html = html[:idx] + f'{marker}{results_json};' + html[end_idx:]
            print(f"  Patched SQUIGGLE_RESULTS with {len(squiggle_results)} rounds")
        else:
            print("  WARNING: SQUIGGLE_RESULTS pattern not found in index.html")

    # ── 7. Embed head-to-head game history ──
    if h2h_games is not None:
        h2h_json = _json.dumps(h2h_games, separators=(',', ':'))
        marker = 'const SQUIGGLE_H2H = '
        idx = html.find(marker)
        if idx >= 0:
            end_idx = html.index(';', idx) + 1
            html = html[:idx] + f'{marker}{h2h_json};' + html[end_idx:]
            print(f"  Patched SQUIGGLE_H2H with {len(h2h_games)} games")
        else:
            print("  WARNING: SQUIGGLE_H2H pattern not found in index.html")

    # ── 8. Embed ladder standings ──
    if standings_raw is not None:
        standings_json = _json.dumps(standings_raw, separators=(',', ':'))
        marker = 'const SQUIGGLE_STANDINGS = '
        idx = html.find(marker)
        if idx >= 0:
            end_idx = html.index(';', idx) + 1
            html = html[:idx] + f'{marker}{standings_json};' + html[end_idx:]
            print(f"  Patched SQUIGGLE_STANDINGS with {len(standings_raw)} teams")
        else:
            print("  WARNING: SQUIGGLE_STANDINGS pattern not found in index.html")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("index.html updated successfully")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\nAFL Footy Tipster Auto-Updater - {datetime.now().strftime('%d %b %Y %H:%M')}\n")

    # 1. Get current round from Squiggle
    print("Fetching round data from Squiggle API...")
    round_num, round_games = get_current_round()
    if round_num is None:
        print("  Squiggle unavailable — skipping round/standings update")

    # 2. Get standings for form multipliers
    standings = {}
    if round_num is not None:
        print("Fetching ladder standings...")
        standings = get_standings()
        print(f"  Got standings for {len(standings)} teams")

    # 3. AFL CFS API — official match rosters (injured + managed)
    print("\nFetching match rosters from AFL CFS API...")
    injured_map = {}
    if round_num is not None:
        injured_map = fetch_afl_lineups(round_num)
    else:
        print("  Skipping lineup fetch — round unknown")

    # 4. SuperCoach — player ratings only
    print("\nFetching player ratings from SuperCoach...")
    ratings_map = fetch_supercoach_ratings()
    if not ratings_map:
        print("  No SuperCoach ratings available — player ratings unchanged")

    # 5. AFL CFS API — player heights
    print("\nFetching player heights from AFL CFS API...")
    with open("index.html", "r", encoding="utf-8") as f:
        current_html = f.read()
    heights_map = fetch_player_heights(current_html)

    # 6. Fetch Sportsbet odds
    print("\nFetching Sportsbet odds...")
    odds_map = fetch_sportsbet_odds()

    # 7. Fetch Squiggle data for browser (accuracy tab, game times, ladder, h2h)
    print("\nFetching Squiggle data for browser embedding...")
    squiggle_results = fetch_all_results()
    h2h_games = fetch_h2h_results()
    standings_raw = fetch_standings_data()

    # 8. Patch index.html
    print(f"\nPatching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map, ratings_map, odds_map,
                     squiggle_results, h2h_games, standings_raw, heights_map)

    print("\nAll done! Changes committed by GitHub Actions.\n")


if __name__ == "__main__":
    main()