"""
AFL Footy Tipster — Weekly Auto-Updater
Fetches live AFL data from the free Squiggle API and uses Claude AI
to analyse the latest injury news, then patches index.html automatically.
"""

import requests
import json
import re
import os
import anthropic
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
SQUIGGLE_BASE = "https://api.squiggle.com.au/"
SQUIGGLE_HEADERS = {"User-Agent": "AFL-Tipster-Bot/1.0 (github.com/your-username/afl-tipster)"}
YEAR = 2026
AFL_INJURY_URL = "https://www.afl.com.au/matches/injury-list"

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
    now = datetime.utcnow()

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

def get_power_rankings():
    """Fetch Squiggle power rankings to adjust team ratings."""
    data = squiggle_get(f"standings;year={YEAR}")
    if not data or "standings" not in data:
        return {}
    rankings = {}
    # Use percentage as a proxy for current form adjustment
    for team in data["standings"]:
        short = TEAM_NAME_MAP.get(team.get("name", ""), None)
        if short:
            pct = team.get("percentage", 100)
            # Convert percentage to a form multiplier: 100% → 1.00, 120% → 1.06, 80% → 0.94
            mult = round(0.6 + (pct / 1000), 3)
            mult = max(0.82, min(1.18, mult))  # clamp between 0.82 and 1.18
            rankings[short] = mult
    return rankings

def fetch_injury_news():
    """Fetch AFL injury list page and extract raw text."""
    try:
        r = requests.get(AFL_INJURY_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; AFL-Tipster-Bot/1.0)"
        }, timeout=15)
        # Extract text between common injury sections
        text = r.text
        # Basic cleanup - strip HTML tags
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean).strip()
        # Grab the first 8000 chars which usually covers all clubs
        return clean[:8000]
    except Exception as e:
        print(f"Could not fetch injury page: {e}")
        return ""

def analyse_injuries_with_claude(injury_text, teams_data):
    """Use Claude to parse injury news and return structured updates."""
    if not injury_text:
        print("No injury text to analyse.")
        return {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY found. Skipping injury analysis.")
        return {}

    client = anthropic.Anthropic(api_key=api_key)

    team_names = "\n".join([f"- {short}: {data['name']}" for short, data in teams_data.items()])

    prompt = f"""You are an AFL injury analyst. I will give you the latest AFL injury news text, and a list of teams with their short codes.

Your job is to identify which named players are confirmed OUT or INJURED for this week based on the text.

Team codes:
{team_names}

Injury news text:
{injury_text}

Return ONLY a valid JSON object mapping team short codes to arrays of injured player surnames.
Only include players who are CONFIRMED OUT — not just doubtful or under observation.
Example format:
{{
  "SYD": ["Gulden", "Heeney"],
  "GWS": ["Taylor", "Kelly", "Daniels"],
  "NTH": ["Wardlaw"]
}}

If no confirmed injuries for a team, omit them. Return only the JSON, nothing else."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        # Clean up any markdown fences if present
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        print(f"Claude identified injuries for {len(parsed)} teams")
        return parsed
    except Exception as e:
        print(f"Claude injury analysis failed: {e}")
        return {}

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
    # Find the round selector default and update it
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

    # 3. Fetch and analyse injury news
    print("🤕 Fetching injury news from AFL.com.au...")
    injury_text = fetch_injury_news()

    print("🤖 Analysing injuries with Claude...")
    # Load teams data from the HTML to pass to Claude
    # We just pass the TEAM_NAME_MAP since we have full team names
    teams_for_claude = {k: {"name": v} for v, k in {
        "Adelaide": "ADE", "Brisbane Lions": "BRI", "Carlton": "CAR",
        "Collingwood": "COL", "Essendon": "ESS", "Fremantle": "FRE",
        "Geelong": "GEE", "Gold Coast": "GCS", "GWS Giants": "GWS",
        "Hawthorn": "HAW", "Melbourne": "MEL", "North Melbourne": "NTH",
        "Port Adelaide": "POR", "Richmond": "RIC", "St Kilda": "STK",
        "Sydney Swans": "SYD", "West Coast": "WCE", "Western Bulldogs": "WBD",
    }.items()}
    injured_map = analyse_injuries_with_claude(injury_text, teams_for_claude)

    # 4. Patch index.html
    print(f"\n📝 Patching index.html for Round {round_num}...")
    patch_index_html(round_num, round_games, standings, injured_map)

    print("\n✅ All done! Changes committed by GitHub Actions.\n")

if __name__ == "__main__":
    main()
