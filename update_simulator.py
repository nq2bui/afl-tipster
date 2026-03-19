"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from the free Squiggle API and scrapes the AFL injury
news page to patch index.html automatically.
"""

import requests
import re
import os
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
SQUIGGLE_BASE = "https://api.squiggle.com.au/"
SQUIGGLE_HEADERS = {"User-Agent": "AFL-Tipster-Bot/1.0 (github.com/your-username/afl-tipster)"}
YEAR = 2026
AFL_INJURY_NEWS_URL = "https://www.afl.com.au/news/injury-news"

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

# Map AFL injury page image filename keywords → our short codes
# Each team section has a banner image whose filename contains a unique keyword.
# ORDER MATTERS: more specific keywords must come before substrings they contain
# (e.g. "north-melbourne" before "melbourne", "port-adelaide" before "adelaide").
# Fallback: expected AFL team order (alphabetical, as AFL.com.au lists them).
# Used when a team section has no detectable banner image.
TEAM_ORDER_FALLBACK = [
    "ADE","BRI","CAR","COL","ESS","FRE","GEE","GCS","GWS",
    "HAW","MEL","NTH","POR","RIC","STK","SYD","WCE","WBD",
]

IMG_KEYWORD_MAP = [
    ("north-melbourne", "NTH"),   # must precede "melbourne"
    ("port-adelaide",   "POR"),   # must precede "adelaide"
    ("western-bulldog", "WBD"),   # must precede "west-coast"
    ("west-coast",      "WCE"),
    ("gc-",             "GCS"),   # Gold Coast slug e.g. gc-strap-new-logo
    ("gold-coast",      "GCS"),
    ("gws",             "GWS"),
    ("stk-",            "STK"),   # St Kilda slug e.g. stk-strap-new-logo
    ("st-kilda",        "STK"),
    ("collingwood",     "COL"),
    ("brisbane",        "BRI"),
    ("carlton",         "CAR"),
    ("essendon",        "ESS"),
    ("fremantle",       "FRE"),
    ("geelong",         "GEE"),
    ("hawthorn",        "HAW"),
    ("melbourne",       "MEL"),
    ("richmond",        "RIC"),
    ("sydney",          "SYD"),
    ("adelaide",        "ADE"),   # last — only after port-adelaide already matched
]

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

def _img_filename_to_code(filename):
    """Map a banner image filename to a team short code."""
    fn = filename.lower()
    for keyword, code in IMG_KEYWORD_MAP:
        if keyword in fn:
            return code
    return None

def fetch_afl_injuries():
    """Scrape AFL injury news page and return {team_code: [surnames]}.

    The page at afl.com.au/news/injury-news serves static HTML containing
    18 injury tables (one per club). Each table is preceded by a club banner
    image whose filename encodes the team name.

    Players with return status 'Test' are excluded (they may play).
    All other listed players are treated as confirmed out.
    """
    try:
        r = requests.get(AFL_INJURY_NEWS_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"Could not fetch AFL injury news page: {e}")
        return {}

    # Locate all <table> start positions
    table_starts = [m.start() for m in re.finditer(r'<table', html)]
    if not table_starts:
        print("No tables found on AFL injury news page.")
        return {}

    # Pattern to extract image filenames from photo-resources URLs
    img_pat = re.compile(r'photo-resources/[^"]+/([^/"?]+\.jpg)')

    injured_map = {}
    prev_table_end = 0  # track end of previous table to limit image search scope

    for table_idx, tstart in enumerate(table_starts):
        # Determine team from the banner image that appears BETWEEN the previous
        # table's end and this table's start (isolates each team's own section).
        preamble = html[prev_table_end:tstart]
        imgs = img_pat.findall(preamble)
        team_code = None
        for fn in reversed(imgs):
            team_code = _img_filename_to_code(fn)
            if team_code:
                break

        if not team_code:
            # Fallback: use sequential position (page lists teams alphabetically)
            if table_idx < len(TEAM_ORDER_FALLBACK):
                team_code = TEAM_ORDER_FALLBACK[table_idx]
                print(f"  (no banner image for table {table_idx + 1}, assuming {team_code} by position)")
            else:
                print(f"  WARNING: Could not identify team for table {table_idx + 1}")
                continue

        # Extract this table's HTML
        tend = html.find('</table>', tstart) + len('</table>')
        prev_table_end = tend  # advance marker past this table
        table_html = html[tstart:tend]

        # Parse all <td> text values (skip <th> header row)
        tds = re.findall(r'<td[^>]*>([^<]+)</td>', table_html)

        # Table columns: Player | Injury | Return (repeating groups of 3)
        out_players = []
        for i in range(0, len(tds) - 2, 3):
            player_name = tds[i].strip()
            return_status = tds[i + 2].strip()

            # Skip players listed as 'Test' — they may play this week
            if return_status.lower() == 'test':
                continue

            # Use the last word as the surname
            surname = player_name.split()[-1] if player_name else None
            if surname:
                out_players.append(surname)

        if out_players:
            injured_map[team_code] = out_players
            print(f"  {team_code}: {len(out_players)} out — {', '.join(out_players)}")

    total = sum(len(v) for v in injured_map.values())
    print(f"  Total: {total} players confirmed out across {len(injured_map)} teams")
    return injured_map

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
            print(f"  Updated {short} formMult → {new_mult}")

    # ── 2. Reset all player out flags to false first ──
    html = re.sub(r'(,out:)true', r'\1false', html)
    print("  Reset all player out flags to false")

    # ── 3. Set confirmed injuries to out:true ──
    for short, injured_surnames in injured_map.items():
        for surname in injured_surnames:
            # Match player name containing this surname and set their out flag
            # Pattern: {n:"Firstname Surname",r:XX,pos:"XXX",out:false}
            pattern = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:\d+,pos:"[A-Z]+",out:)false'
            new_html = re.sub(pattern, r'\1true', html, count=1)
            if new_html != html:
                html = new_html
                print(f"  Marked {short} - {surname} as OUT")
            else:
                print(f"  WARNING: Could not find player matching '{surname}' for {short}")

    # ── 4. Update the default selected round to the current upcoming round ──
    round_str = str(round_num) if round_num != 0 else "OR"
    pattern = r'(let currentRound = )"[^"]*"'
    new_html = re.sub(pattern, f'\\1"{round_str}"', html, count=1)
    if new_html != html:
        html = new_html
        print(f"  Set default round to {round_str}")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ index.html updated successfully")

def main():
    print(f"\n🏉 AFL Footy Tipster Auto-Updater — {datetime.now().strftime('%d %b %Y %H:%M')}\n")

    # 1. Get current round from Squiggle
    print("📡 Fetching round data from Squiggle API...")
    round_num, round_games = get_current_round()
    if round_num is None:
        print("Could not determine current round. Exiting.")
        return

    # 2. Get standings for form multipliers
    print("📊 Fetching ladder standings...")
    standings = get_standings()
    print(f"  Got standings for {len(standings)} teams")

    # 3. Scrape injury list from AFL injury news page
    print("🤕 Fetching injury list from AFL.com.au/news/injury-news...")
    injured_map = fetch_afl_injuries()

    # 4. Patch index.html
    print(f"\n📝 Patching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map)

    print("\n✅ All done! Changes committed by GitHub Actions.\n")

if __name__ == "__main__":
    main()
