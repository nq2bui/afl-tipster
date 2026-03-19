"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from:
  1. FootyWire injury list (available all week — injuries + suspensions)
  2. SuperCoach public API (available after teams named Thu-Sat — confirmed outs)
Merges both sources and patches index.html automatically.
"""

import requests
import re
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
SQUIGGLE_BASE = "https://api.squiggle.com.au/"
SQUIGGLE_HEADERS = {"User-Agent": "AFL-Tipster-Bot/1.0 (github.com/your-username/afl-tipster)"}
YEAR = 2026

FOOTYWIRE_INJURY_URL = "https://www.footywire.com/afl/footy/injury_list"

# SuperCoach public API — no auth required (updated when teams named Thu-Sat)
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

# FootyWire team full names → our short codes
FW_TEAM_MAP = {
    "Adelaide Crows": "ADE", "Brisbane Lions": "BRI", "Carlton Blues": "CAR",
    "Collingwood Magpies": "COL", "Essendon Bombers": "ESS", "Fremantle Dockers": "FRE",
    "Geelong Cats": "GEE", "Gold Coast Suns": "GCS", "GWS Giants": "GWS",
    "Hawthorn Hawks": "HAW", "Melbourne Demons": "MEL", "North Melbourne Kangaroos": "NTH",
    "Port Adelaide Power": "POR", "Richmond Tigers": "RIC", "St Kilda Saints": "STK",
    "Sydney Swans": "SYD", "West Coast Eagles": "WCE", "Western Bulldogs": "WBD",
}

# SuperCoach uses slightly different abbreviations — map to our codes
SC_ABBREV_MAP = {
    "ADE": "ADE", "BRL": "BRI", "CAR": "CAR", "COL": "COL",
    "ESS": "ESS", "FRE": "FRE", "GCS": "GCS", "GEE": "GEE",
    "GWS": "GWS", "HAW": "HAW", "MEL": "MEL", "NTH": "NTH",
    "PTA": "POR", "RIC": "RIC", "STK": "STK", "SYD": "SYD",
    "WBD": "WBD", "WCE": "WCE",
}


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
                "wins": team.get("wins", 0),
                "losses": team.get("losses", 0),
                "percentage": round(team.get("percentage", 100), 1),
                "rank": team.get("rank", 9),
            }
    return standings


def fetch_footywire_injuries():
    """Fetch injury/suspension list from FootyWire.

    Returns {team_code: [surnames]} for all players currently on the injury list,
    excluding fitness-test players (status 'Test') since they may still play.
    FootyWire updates continuously through the week.
    """
    try:
        r = requests.get(FOOTYWIRE_INJURY_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  Could not fetch FootyWire injury list: {e}")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    current_team = None
    injured_map = {}
    skipped_test = 0

    # Valid return values always contain a digit or a known keyword
    valid_return = re.compile(
        r'\d|indefinite|season|mid.season|plus', re.IGNORECASE
    )

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        texts = [re.sub(r'<[^>]+>', '', c).replace('\xa0', '').replace('&nbsp;', '').strip() for c in cells]
        texts = [t for t in texts if t]

        # Team header row — single cell containing a known team name
        if len(texts) == 1:
            for full_name, code in FW_TEAM_MAP.items():
                if full_name in texts[0]:
                    current_team = code
                    break
            continue

        # Player rows always have exactly 3 cells: Name, Injury, Returning
        if len(texts) != 3 or not current_team:
            continue

        name, _injury, returning = texts

        # Skip column header row
        if name.lower() == "player":
            continue

        # Skip fitness test players — uncertain whether they'll play
        if returning.strip().lower() == "test":
            skipped_test += 1
            continue

        # Validate returning looks like real injury data (contains digit or known keyword)
        if not valid_return.search(returning):
            continue

        surname = name.split()[-1] if name else ""
        # Validate surname is letters only (plus apostrophe/hyphen) — filters out nav junk
        if not surname or not re.match(r"^[A-Z][a-zA-Z'\-]+$", surname):
            continue

        injured_map.setdefault(current_team, [])
        if surname not in injured_map[current_team]:
            injured_map[current_team].append(surname)

    total = sum(len(v) for v in injured_map.values())
    print(f"  FootyWire: {total} players out across {len(injured_map)} teams "
          f"(skipped {skipped_test} fitness-test players)")
    if total > 0:
        for team, names in sorted(injured_map.items()):
            print(f"    {team}: {', '.join(names)}")

    return injured_map


def fetch_supercoach_injuries():
    """Fetch confirmed team selections from the SuperCoach public API.

    Returns {team_code: [surnames]} for players named out this week.
    Only populated after teams are announced (typically Thu-Sat).
    'Bye' entries are skipped — the whole team isn't playing, not injured.
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

    injured_map = {}
    skipped_bye = 0

    for p in players:
        status = p.get("injury_suspension_status")
        if not status:
            continue
        if status.lower() == "bye":
            skipped_bye += 1
            continue

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
    print(f"  SuperCoach: {total} players confirmed out across {len(injured_map)} teams "
          f"(skipped {skipped_bye} bye-week entries)")
    if total > 0:
        for team, names in sorted(injured_map.items()):
            print(f"    {team}: {', '.join(names)}")

    return injured_map


def merge_injury_maps(base, override):
    """Merge two {team: [surnames]} dicts. Override adds to or replaces base entries."""
    merged = {team: list(names) for team, names in base.items()}
    for team, names in override.items():
        merged.setdefault(team, [])
        for name in names:
            if name not in merged[team]:
                merged[team].append(name)
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
    base = 0.88 + (win_rate * 0.24)
    pct_adj = (pct - 100) * 0.001
    return round(max(0.82, min(1.18, base + pct_adj)), 3)


def patch_index_html(round_num, round_games, standings, injured_map):
    """Read index.html, patch TEAMS formMult and player out flags, and write back."""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # ── 1. Update formMult for each team based on live standings ──
    for short, standing in standings.items():
        new_mult = calculate_form_mult(standings, short)
        pattern = rf'({re.escape(short)}:{{[^}}]*?baseRating:\d+,formMult:)[\d\.]+'
        new_html = re.sub(pattern, rf'\g<1>{new_mult}', html, count=1)
        if new_html != html:
            html = new_html
            print(f"  Updated {short} formMult -> {new_mult}")

    # ── 2. Reset all player out flags to false ──
    html = re.sub(r'(,out:)true', r'\1false', html)
    print("  Reset all player out flags to false")

    # ── 3. Mark injured/suspended players as out:true (team-restricted) ──
    for short, injured_surnames in injured_map.items():
        team_pat = re.compile(
            rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
            re.DOTALL
        )
        for surname in injured_surnames:
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

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("index.html updated successfully")


def main():
    print(f"\nAFL Footy Tipster Auto-Updater - {datetime.now().strftime('%d %b %Y %H:%M')}\n")

    # 1. Get current round from Squiggle (optional — continues if unavailable)
    print("Fetching round data from Squiggle API...")
    round_num, round_games = get_current_round()
    if round_num is None:
        print("  Squiggle unavailable — skipping round/standings update")

    # 2. Get standings for form multipliers (skip if Squiggle is down)
    standings = {}
    if round_num is not None:
        print("Fetching ladder standings...")
        standings = get_standings()
        print(f"  Got standings for {len(standings)} teams")

    # 3. FootyWire — available all week (injuries + suspensions)
    print("\nFetching injury list from FootyWire...")
    fw_map = fetch_footywire_injuries()
    if not fw_map:
        print("  No FootyWire data available")

    # 4. SuperCoach — available after teams named (Thu-Sat); confirms final outs
    print("\nFetching confirmed team selections from SuperCoach...")
    sc_map = fetch_supercoach_injuries()
    if not sc_map:
        print("  Teams not yet named (SuperCoach will update Thu-Sat)")

    # 5. Merge: FootyWire as base, SuperCoach adds confirmed outs on top
    injured_map = merge_injury_maps(fw_map, sc_map)
    total = sum(len(v) for v in injured_map.values())
    print(f"\nCombined: {total} players marked OUT across {len(injured_map)} teams")

    # 6. Patch index.html
    print(f"\nPatching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map)

    print("\nAll done! Changes committed by GitHub Actions.\n")


if __name__ == "__main__":
    main()
