"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from the Squiggle API, the SuperCoach public API
(for weekly team selection status), and the AFL injury news page (for
longer-term injuries), then patches index.html automatically.
"""

import requests
import re
import os
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
SQUIGGLE_BASE = "https://api.squiggle.com.au/"
SQUIGGLE_HEADERS = {"User-Agent": "AFL-Tipster-Bot/1.0 (github.com/your-username/afl-tipster)"}
YEAR = 2026

# SuperCoach public API — no auth required
# Returns all players with injury_suspension_status field (updated when teams named Thu-Sat)
SUPERCOACH_URL = (
    "https://supercoach.com.au/2026/api/afl/classic/v1/players-cf"
    "?embed=notes,odds,player_stats,positions,team"
)

# AFL injury news page — static HTML with 18 tables of current season injuries
AFL_INJURY_NEWS_URL = "https://www.afl.com.au/news/injury-news"

# AFL injury page: image filename keyword → team short code (ordered: specific before general)
IMG_KEYWORD_MAP = [
    ("north-melbourne", "NTH"), ("port-adelaide",   "POR"),
    ("western-bulldog", "WBD"), ("west-coast",      "WCE"),
    ("gc-",             "GCS"), ("gold-coast",      "GCS"),
    ("gws",             "GWS"), ("stk-",            "STK"),
    ("st-kilda",        "STK"), ("collingwood",     "COL"),
    ("brisbane",        "BRI"), ("carlton",         "CAR"),
    ("essendon",        "ESS"), ("fremantle",       "FRE"),
    ("geelong",         "GEE"), ("hawthorn",        "HAW"),
    ("melbourne",       "MEL"), ("richmond",        "RIC"),
    ("sydney",          "SYD"), ("adelaide",        "ADE"),
]

# Alphabetical fallback when no banner image found for a team section
TEAM_ORDER_FALLBACK = [
    "ADE","BRI","CAR","COL","ESS","FRE","GEE","GCS","GWS",
    "HAW","MEL","NTH","POR","RIC","STK","SYD","WCE","WBD",
]

# Map Squiggle team names → our short codes
TEAM_NAME_MAP = {
    "Adelaide": "ADE", "Brisbane Lions": "BRI", "Carlton": "CAR",
    "Collingwood": "COL", "Essendon": "ESS", "Fremantle": "FRE",
    "Geelong": "GEE", "Gold Coast": "GCS", "Greater Western Sydney": "GWS",
    "GWS Giants": "GWS", "Hawthorn": "HAW", "Melbourne": "MEL",
    "North Melbourne": "NTH", "Port Adelaide": "POR", "Richmond": "RIC",
    "St Kilda": "STK", "Sydney": "SYD", "Sydney Swans": "SYD",
    "West Coast": "WCE", "Western Bulldogs": "WBD",
}

# SuperCoach uses slightly different abbreviations — map to our codes
SC_ABBREV_MAP = {
    "ADE": "ADE", "BRL": "BRI", "CAR": "CAR", "COL": "COL",
    "ESS": "ESS", "FRE": "FRE", "GCS": "GCS", "GEE": "GEE",
    "GWS": "GWS", "HAW": "HAW", "MEL": "MEL", "NTH": "NTH",
    "PTA": "POR", "RIC": "RIC", "STK": "STK", "SYD": "SYD",
    "WBD": "WBD", "WCE": "WCE",
}

# Statuses that mean a player is OUT this week (not just on the injury list)
OUT_STATUSES = {"injured", "suspension", "suspended", "omitted", "medical sub",
                "medical", "sub", "out", "rested", "personal", "illness"}


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

    # Find the earliest round with games not yet complete
    upcoming = [g for g in games if g.get("complete", 100) < 100]
    if not upcoming:
        print("No upcoming games found.")
        return None, None

    # Sort by date, pick the first upcoming round
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
        short = TEAM_NAME_MAP.get(team.get("name", ""), None)
        if short:
            standings[short] = {
                "wins": team.get("wins", 0),
                "losses": team.get("losses", 0),
                "percentage": round(team.get("percentage", 100), 1),
                "rank": team.get("rank", 9),
            }
    return standings


def fetch_supercoach_injuries():
    """Fetch player injury/suspension status from the SuperCoach public API.

    Returns {team_code: [surnames]} for players confirmed out this week.

    The SuperCoach API is updated when teams are named (typically Thu-Sat).
    Players with injury_suspension_status set to anything other than 'Bye'
    are treated as confirmed out for the current round.
    'Bye' means the whole team has a bye — we skip those since those teams
    simply have no match, and we don't want to mass-mark a full team as out.
    """
    try:
        r = requests.get(SUPERCOACH_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }, timeout=15)
        r.raise_for_status()
        players = r.json()
    except Exception as e:
        print(f"Could not fetch SuperCoach data: {e}")
        return {}

    injured_map = {}
    skipped_bye = 0

    for p in players:
        status = p.get("injury_suspension_status")
        if not status:
            continue

        # Skip bye — the whole team is just not playing that week
        if status.lower() == "bye":
            skipped_bye += 1
            continue

        # Any other status = player is out this week
        sc_abbrev = p.get("team", {}).get("abbrev", "")
        team_code = SC_ABBREV_MAP.get(sc_abbrev)
        if not team_code:
            continue

        surname = p.get("last_name", "").strip()
        if not surname:
            continue

        injured_map.setdefault(team_code, [])
        if surname not in injured_map[team_code]:
            injured_map[team_code].append(surname)

    total = sum(len(v) for v in injured_map.values())
    print(f"  SuperCoach: {total} players out across {len(injured_map)} teams "
          f"(skipped {skipped_bye} bye-week entries)")

    if total > 0:
        for team, names in sorted(injured_map.items()):
            print(f"    {team}: {', '.join(names)}")

    return injured_map


def _img_filename_to_code(filename):
    fn = filename.lower()
    for keyword, code in IMG_KEYWORD_MAP:
        if keyword in fn:
            return code
    return None


def fetch_afl_injury_news():
    """Scrape AFL injury news page for longer-term injuries.

    Returns {team_code: [surnames]} for all players on the injury list.
    Players listed as 'Test' (might play) are excluded.
    Used as a supplement to SuperCoach when weekly selections haven't been named.
    """
    try:
        r = requests.get(AFL_INJURY_NEWS_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  Could not fetch AFL injury news: {e}")
        return {}

    table_starts = [m.start() for m in re.finditer(r'<table', html)]
    if not table_starts:
        return {}

    img_pat = re.compile(r'photo-resources/[^"]+/([^/"?]+\.jpg)')
    injured_map = {}
    prev_table_end = 0

    for table_idx, tstart in enumerate(table_starts):
        preamble = html[prev_table_end:tstart]
        imgs = img_pat.findall(preamble)
        team_code = None
        for fn in reversed(imgs):
            team_code = _img_filename_to_code(fn)
            if team_code:
                break
        if not team_code:
            if table_idx < len(TEAM_ORDER_FALLBACK):
                team_code = TEAM_ORDER_FALLBACK[table_idx]

        tend = html.find('</table>', tstart) + len('</table>')
        prev_table_end = tend
        table_html = html[tstart:tend]
        tds = re.findall(r'<td[^>]*>([^<]+)</td>', table_html)

        if team_code:
            for i in range(0, len(tds) - 2, 3):
                player_name = tds[i].strip()
                return_status = tds[i + 2].strip()
                if return_status.lower() == 'test':
                    continue
                surname = player_name.split()[-1] if player_name else None
                if surname:
                    injured_map.setdefault(team_code, [])
                    if surname not in injured_map[team_code]:
                        injured_map[team_code].append(surname)

    total = sum(len(v) for v in injured_map.values())
    print(f"  AFL injury news: {total} players on injury list across {len(injured_map)} teams")
    return injured_map


def merge_injured_maps(*maps):
    """Merge multiple {team: [surnames]} dicts, deduplicating surnames per team."""
    merged = {}
    for m in maps:
        for team, surnames in m.items():
            merged.setdefault(team, [])
            for s in surnames:
                if s not in merged[team]:
                    merged[team].append(s)
    return merged


def calculate_form_mult(standings, short):
    """Calculate a form multiplier from standings percentage."""
    if short not in standings:
        return 1.00
    pct = standings[short].get("percentage", 100)
    wins = standings[short].get("wins", 0)
    losses = standings[short].get("losses", 0)
    played = wins + losses
    if played == 0:
        return 1.00
    win_rate = wins / played
    # Blend win rate and percentage into a multiplier
    base = 0.88 + (win_rate * 0.24)  # 0.88 (0% wins) to 1.12 (100% wins)
    pct_adj = (pct - 100) * 0.001    # slight percentage tweak
    return round(max(0.82, min(1.18, base + pct_adj)), 3)


def patch_index_html(round_num, round_games, standings, injured_map):
    """Read index.html, patch the TEAMS formMult and player out flags, and write back."""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # ── 1. Update formMult for each team based on live standings ──
    for short, standing in standings.items():
        new_mult = calculate_form_mult(standings, short)
        # Pattern: looks for formMult:X.XX inside the team's TEAMS block
        pattern = rf'({re.escape(short)}:{{[^}}]*?baseRating:\d+,formMult:)[\d\.]+'
        replacement = rf'\g<1>{new_mult}'
        new_html = re.sub(pattern, replacement, html, count=1)
        if new_html != html:
            html = new_html
            print(f"  Updated {short} formMult -> {new_mult}")

    # ── 2. Reset all player out flags to false first ──
    html = re.sub(r'(,out:)true', r'\1false', html)
    print("  Reset all player out flags to false")

    # ── 3. Set confirmed injuries to out:true (restricted to each team's section) ──
    # Pattern to locate each team's player array in the TEAMS constant
    # SHORT:{...players:[CONTENTS]} — player objects use {} not [], so first ] closes it
    for short, injured_surnames in injured_map.items():
        team_pat = re.compile(
            rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
            re.DOTALL
        )
        for surname in injured_surnames:
            # Re-search after each replacement (length may change: false->true is -1 char)
            m = team_pat.search(html)
            if not m:
                print(f"  WARNING: Could not locate {short} players section")
                break
            s_start, s_end = m.start(1), m.end(1)
            section = html[s_start:s_end]

            player_pat = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:\d+,pos:"[A-Z]+",out:)false'
            new_section = re.sub(player_pat, r'\1true', section, count=1)
            if new_section != section:
                html = html[:s_start] + new_section + html[s_end:]
                print(f"  Marked {short} - {surname} as OUT")
            else:
                print(f"  (no roster match for {short}:{surname})")

    # ── 4. Update the default selected round to the current upcoming round ──
    if round_num is not None:
        round_str = str(round_num) if round_num != 0 else "OR"
        pattern = r'(let currentRound = )"[^"]*"'
        new_html = re.sub(pattern, f'\\1"{round_str}"', html, count=1)
        if new_html != html:
            html = new_html
            print(f"  Set default round to {round_str}")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("index.html updated successfully")


def main():
    print(f"\nAFL Footy Tipster Auto-Updater - {datetime.now().strftime('%d %b %Y %H:%M')}\n")

    # 1. Get current round from Squiggle
    print("Fetching round data from Squiggle API...")
    round_num, round_games = get_current_round()
    if round_num is None:
        print("Could not determine current round. Exiting.")
        return

    # 2. Get standings for form multipliers
    print("Fetching ladder standings...")
    standings = get_standings()
    print(f"  Got standings for {len(standings)} teams")

    # 3. Fetch injuries from both SuperCoach (weekly selections) and AFL injury news (long-term)
    print("Fetching player status from SuperCoach API...")
    sc_injuries = fetch_supercoach_injuries()

    print("Fetching injury list from AFL injury news page...")
    afl_injuries = fetch_afl_injury_news()

    # Merge: SuperCoach takes priority but AFL injury news fills in long-term absences
    injured_map = merge_injured_maps(sc_injuries, afl_injuries)
    total = sum(len(v) for v in injured_map.values())
    print(f"  Combined: {total} players out across {len(injured_map)} teams")

    # 4. Patch index.html
    print(f"\nPatching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map)

    print("\nAll done! Changes committed by GitHub Actions.\n")


if __name__ == "__main__":
    main()
