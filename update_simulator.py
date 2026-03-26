"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from:
  1. AFL CFS API (api.afl.com.au) — official match rosters, outs (injured + managed)
  2. SuperCoach public API — player ratings only (previous season avg scores)
  3. Squiggle API — round detection, ladder standings
Patches index.html automatically.
"""

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
        r = requests.get(SQUIGGLE_BASE, params={"q": query_params}, headers=SQUIGGLE_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Squiggle API error: {e}")
        return None


def get_current_round():
    """Determine the current/upcoming round from Squiggle."""
    data = squiggle_get(f"games;year={YEAR}")
    if not data or "games" not in data:
        print("Could not fetch games from Squiggle.")
        return None, None

    games = data["games"]
    upcoming = [g for g in games if g.get("complete", 100) < 100]
    if not upcoming:
        print("No upcoming games found.")
        return None, None

    upcoming.sort(key=lambda g: g.get("date", "9999"))
    next_round = upcoming[0].get("round")
    round_games = [g for g in upcoming if g.get("round") == next_round]
    print(f"Detected upcoming round: {next_round} ({len(round_games)} games)")
    return next_round, round_games


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


def patch_index_html(round_num, round_games, standings, injured_map, ratings_map=None, odds_map=None):
    """Read index.html, patch TEAMS data and player out flags, then write back."""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

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
            player_pat = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:\d+,pos:"[A-Z]+",out:)false'
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

    # ── 5. Patch Sportsbet odds ──
    if odds_map:
        import json as _json
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

    # 5. Fetch Sportsbet odds
    print("\nFetching Sportsbet odds...")
    odds_map = fetch_sportsbet_odds()

    # 6. Patch index.html
    print(f"\nPatching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map, ratings_map, odds_map)

    print("\nAll done! Changes committed by GitHub Actions.\n")


if __name__ == "__main__":
    main()