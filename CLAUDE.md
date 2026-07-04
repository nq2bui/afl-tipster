# AFL Tipster — Project Context

## Overview

Single-file AFL tipping dashboard. All UI, data, and logic lives in `index.html`. A GitHub Actions cron job runs `update_simulator.py` to patch live data into that file.

## Files

- `index.html` — the entire app; hardcoded `TEAMS`, `PLAYER_IMGS`, and `FIXTURE` JS objects plus all rendering logic
- `update_simulator.py` — cron script that patches `index.html` with live injuries, SuperCoach ratings, and odds

## Data Sources

| Data | Source |
|------|--------|
| Injuries / managed players | AFL CFS API — `matchRoster/full/{providerId}` |
| SuperCoach ratings | `www.supercoach.com.au/2026/api/afl/classic/v1/players-cf` |
| Round detection | Squiggle API, with AFL API fallback (`compSeasonId=85`) |
| 2026 roster verification | SuperCoach player API (same endpoint above) |

## AFL CFS API Auth

```
POST https://api.afl.com.au/cfs/afl/WMCTok  →  {"token": "..."}
Header on subsequent requests: x-media-mis-token: <token>
```

- Token fetched fresh every run — never hardcode it
- Match provider IDs: `GET api.afl.com.au/cfs/afl/matches?competitionId=1&compSeasonId=85&roundNumber=N`
- Roster: `GET api.afl.com.au/cfs/afl/matchRoster/full/CD_M2026XXXXXXXX`

## Key Constants (update_simulator.py)

```python
COMP_SEASON_ID = 85        # 2026 AFL Premiership
CFS_API_BASE = "https://api.afl.com.au/cfs/afl"
```

## PLAYER_IMGS Format

```js
"TEAM:F. Surname": "https://s.afl.com.au/staticfile/AFL%20Tenant/AFL/Players/ChampIDImages/AFL/2026014/{feed_id}.png?im=Resize,width=200"
```

- `feed_id` = SuperCoach `feed_id` field = AFL ChampID (numeric part of `CD_I{number}`)
- Year folder: `2026014` for 2026 season

## TEAMS Structure

```js
TEAM: {
  name, color, baseRating, formMult, wins, losses, pct, rank,
  players: [ {n: "F. Surname", r: <sc_rating>, pos: "MID|FWD|DEF|RCK", out: bool} ]
}
```

## 2026 Roster (last verified 2026-03-26 via SuperCoach API)

> Re-verify monthly against the SuperCoach player API — trades/retirements
> are not auto-detected. Last check: 2026-03-26 (now 3+ months stale).

**Retired:** M. Crouch (ADE), J. Daniher (BRI), E. Curnow (CAR), G. Rohan (GEE), M. Schache (HAW), C. Lazzaro/J. Hoogaard (NTH), J. Finlayson/I. Nankervis (POR), B. Paton (STK), A. West (WCE)

**Traded:** O. Henry COL→GEE, J. Lukosius GCS→POR, H. Himmelberg/D. Rioli GWS→GCS, N. Haynes GWS→CAR, L. Bramble HAW→WBD, D. Houston POR→COL, J. Battle STK→HAW, T. Barrass WCE→HAW, C. Daniel WBD→NTH, C. Petracca MEL→GCS, C. Curnow CAR→SYD

**Name fix:** SYD `J. Papley` → `T. Papley` (Tom Papley)

## FIXTURE Data

`FIXTURE` in `index.html` (match list, venues, day/date labels) is hand-typed
and is **not** derived from `SQUIGGLE_RESULTS` (the live data patched in each
cron run) — the two can silently disagree. This happened for real: Round 17
had 6 of 9 matches showing the wrong day. `update_simulator.py` now runs
`validate_fixture_dates()` on every cron run, which diffs `FIXTURE`'s
day-of-week against Squiggle's actual kickoff dates and prints a `WARNING`
per mismatch to the Action log — check there before trusting a newly-typed
round's dates.

## Common Pitfalls

1. **Nested git repo** — `afl-tipster/` has its own `.git`. Always commit/push from inside `afl-tipster/`, never from the outer `PythonProject` repo.
2. **GitHub Actions cron** auto-patches `index.html` — always `git pull` before pushing manual edits to avoid merge conflicts.
3. **PLAYER_IMGS insertion** — the last existing entry already has a trailing comma. Do NOT prepend `,\n` to new entries or it creates a `,,` JS syntax error that breaks the page.