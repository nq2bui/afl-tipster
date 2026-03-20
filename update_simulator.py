"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from:
  1. FootyWire injury list (available all week — injuries + suspensions)
  2. SuperCoach public API (available after teams named Thu-Sat — confirmed outs)
Merges both sources and patches index.html automatically.
"""

import requests
import re
import statistics
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


def build_position_stats(players, min_games=5):
    """Compute mean and stdev of SC avg score per position from the full player pool.

    Uses all players with enough games to give reliable position benchmarks.
    These are used to normalise individual ratings against position peers.

    Returns {position: {"mean": float, "stdev": float}}
    """
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
                "stdev": statistics.pstdev(avgs) or 1.0,  # pstdev = full population
            }
            print(f"  Position stats  {pos}: mean={pos_stats[pos]['mean']:.1f}  "
                  f"stdev={pos_stats[pos]['stdev']:.1f}  n={len(avgs)}")
    return pos_stats


def sc_to_rating(avg, games, position, pos_stats, min_games=2):
    """Convert SC avg score to a 50–99 simulator rating using positional normalisation.

    A Z-score measures how far above/below the position average the player is.
    This means a DEF averaging 75 rates much higher than a MID averaging 75,
    because 75 is elite for a defender but below average for a midfielder.

      rating = clamp(74 + z * 10,  50, 99)

    74 = league-average player on our scale. Each standard deviation = 10 rating pts.
    Falls back to a raw linear mapping if position stats are unavailable.
    """
    if not avg or not games or games < min_games:
        return None

    # API uses "RUC" — normalise to match build_position_stats key
    pos_key = "RUC" if position in ("RUC", "RCK") else position
    stats = pos_stats.get(pos_key)
    if stats:
        z = (avg - stats["mean"]) / stats["stdev"]
        return max(50, min(99, round(74 + z * 10)))

    # Fallback: raw linear map  avg 40→50, 130→99
    t = max(0.0, min(1.0, (avg - 40.0) / 90.0))
    return max(50, min(99, round(50 + t * 49)))


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
        r'\d|indefinite|season|mid.season|plus|test', re.IGNORECASE
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

        # Count fitness test players (no longer skipped — marked OUT until confirmed fit)
        if returning.strip().lower() == "test":
            skipped_test += 1

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
          f"(includes {skipped_test} fitness-test players)")
    if total > 0:
        for team, names in sorted(injured_map.items()):
            print(f"    {team}: {', '.join(names)}")

    return injured_map


def fetch_supercoach_data():
    """Fetch player data from SuperCoach API in a single request.

    Returns a tuple:
      injuries_map: {team_code: [surnames]} — players confirmed OUT this week.
                    Only populated after teams named (Thu–Sat). Bye entries skipped.
      ratings_map:  {(team_code, surname): rating} — 50–99 positionally-normalised
                    rating derived from previous season avg score.
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
        return {}, {}

    # ── Pass 1: build position benchmarks from the full player pool ──
    print("  Computing position benchmarks...")
    pos_stats = build_position_stats(players)

    # ── Pass 2: injuries + normalised ratings ──
    injuries_map = {}
    ratings_map  = {}
    skipped_bye  = 0

    for p in players:
        sc_abbrev = p.get("team", {}).get("abbrev", "")
        team_code = SC_ABBREV_MAP.get(sc_abbrev)
        if not team_code:
            continue

        surname = p.get("last_name", "").strip()
        if not surname:
            continue

        # ── Injuries ──
        status = p.get("injury_suspension_status")
        if status:
            if status.lower() == "bye":
                skipped_bye += 1
            else:
                injuries_map.setdefault(team_code, [])
                if surname not in injuries_map[team_code]:
                    injuries_map[team_code].append(surname)

        # ── Positionally-normalised rating ──
        avg      = p.get("previous_average") or 0
        games    = p.get("previous_games") or 0
        position = (p.get("positions") or [{}])[0].get("position", "")

        rating = sc_to_rating(avg, games, position, pos_stats)
        if rating:
            ratings_map[(team_code, surname)] = rating

    inj_total = sum(len(v) for v in injuries_map.values())
    print(f"  SuperCoach: {inj_total} injuries confirmed, {len(ratings_map)} player ratings loaded"
          f" (skipped {skipped_bye} bye-week entries)")
    if inj_total > 0:
        for team, names in sorted(injuries_map.items()):
            print(f"    {team}: {', '.join(names)}")

    return injuries_map, ratings_map


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


def patch_index_html(round_num, round_games, standings, injured_map, ratings_map=None):
    """Read index.html, patch TEAMS formMult and player out flags, and write back."""
    with open("index.html", "r", encoding="utf-8") as f:
        html = f.read()

    # ── 1. Update formMult and ladder standings for each team ──
    for short, s in standings.items():
        # formMult
        new_mult = calculate_form_mult(standings, short)
        pattern = rf'({re.escape(short)}:{{[^[]*?formMult:)[\d\.]+'
        new_html = re.sub(pattern, rf'\g<1>{new_mult}', html, count=1)
        if new_html != html:
            html = new_html
            print(f"  Updated {short} formMult -> {new_mult}")
        # wins / losses / pct / rank (stored in TEAMS for the ladder table)
        header_pat = re.compile(rf'{re.escape(short)}:{{([^[]+)players:\[', re.DOTALL)
        m = header_pat.search(html)
        if m:
            hdr = m.group(1)
            hdr = re.sub(r'\bwins:\d+', f'wins:{s["wins"]}', hdr)
            hdr = re.sub(r'\blosses:\d+', f'losses:{s["losses"]}', hdr)
            hdr = re.sub(r'\bpct:[\d.]+', f'pct:{s["percentage"]}', hdr)
            hdr = re.sub(r'\brank:\d+', f'rank:{s["rank"]}', hdr)
            html = html[:m.start(1)] + hdr + html[m.end(1):]

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

    # ── 4. Update player ratings from SuperCoach prices / avg scores ──
    if ratings_map:
        updated = 0
        teams_with_ratings = set(k[0] for k in ratings_map)
        for short in teams_with_ratings:
            team_pat = re.compile(
                rf'{re.escape(short)}:{{[^[]+players:\[([^\]]*)\]',
                re.DOTALL
            )
            m = team_pat.search(html)
            if not m:
                print(f"  WARNING: Could not locate {short} players section for rating update")
                continue
            s_start, s_end = m.start(1), m.end(1)
            section = html[s_start:s_end]
            changed = False
            for (t_code, surname), rating in ratings_map.items():
                if t_code != short:
                    continue
                pat = rf'({{n:"[^"]*{re.escape(surname)}[^"]*",r:)\d+'
                new_section = re.sub(pat, rf'\g<1>{rating}', section, count=1)
                if new_section != section:
                    section = new_section
                    changed = True
                    updated += 1
                    print(f"  Rating {short} - {surname} -> {rating}")
            if changed:
                html = html[:s_start] + section + html[s_end:]
        print(f"  Updated {updated} player ratings from SuperCoach")

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

    # 4. SuperCoach — available after teams named (Thu-Sat); confirms final outs + ratings
    print("\nFetching SuperCoach player data (injuries + ratings)...")
    sc_map, ratings_map = fetch_supercoach_data()
    if not sc_map:
        print("  Teams not yet named (SuperCoach will update Thu-Sat)")
    if not ratings_map:
        print("  No SuperCoach ratings available — player ratings unchanged")

    # 5. Merge: FootyWire as base, SuperCoach adds confirmed outs on top
    injured_map = merge_injury_maps(fw_map, sc_map)
    total = sum(len(v) for v in injured_map.values())
    print(f"\nCombined: {total} players marked OUT across {len(injured_map)} teams")

    # 6. Patch index.html
    print(f"\nPatching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map, ratings_map)

    print("\nAll done! Changes committed by GitHub Actions.\n")


if __name__ == "__main__":
    main()
