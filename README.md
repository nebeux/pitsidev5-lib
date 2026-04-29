# pitline — VEX Robotics Scouting Library

A Python library for fetching, analyzing, and rating VEX Robotics Competition teams using the RobotEvents v2 API. All analytics are computed offline from match data — no extra API calls needed once you have the data.

---

## Installation

```bash
pip install pitlinev5
```

```python
from pitlinev5 import RobotEvents, EventAnalyzer, TeamAnalyzer
from pitlinev5 import export_to_csv, export_to_json, normalize_scores, batch_analyze, clear_cache
```

---

## Quickstart

```python
from pitlinev5 import RobotEvents, TeamAnalyzer, EventAnalyzer

re = RobotEvents(api_key="your_key")

# Fetch full season data for a team
data = re.fetch_team_data("13155U")

# Per-team analytics — all stats computed from the full season match history
ta = TeamAnalyzer("13155U", data)
print(ta.summary())

# Global ELO — accounts for every opponent's record across all events
event_matches = re.get_matches_for_team_events(data)
print(ta.elo(event_matches))

# Event-wide ratings (OPR, DPR, rankings)
matches = re.get_event_matches(event_id=12345)
ea      = EventAnalyzer(matches)
print(ea.full_rankings())
print(ea.win_probability(["13155U", "1234A"], ["5678B", "9999C"]))
```

---

## `RobotEvents`

HTTP client for the RobotEvents v2 API. Handles authentication, pagination, rate limiting, and local disk caching. Responses are cached to `.pitline_cache/` by default.

```python
re = RobotEvents(api_key, use_cache=True, cache_ttl=3600)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | required | Your RobotEvents API bearer token |
| `use_cache` | `bool` | `True` | Read/write cached responses to disk |
| `cache_ttl` | `int` | `3600` | Seconds before a cached response is considered stale |

---

### `re.get_team(team_number)`

Fetch public info for a single team by their human-readable number.

```python
team = re.get_team("13155U")
# {"id": 167783, "number": "13155U", "team_name": "...", ...}
```

| Parameter | Type | Description |
|---|---|---|
| `team_number` | `str` | Team number string, e.g. `"13155U"` or `"1234A"` |

**Returns:** `dict` of team info, or `None` if not found.

---

### `re.fetch_team_data(team, season?)`

Fetch a full season data bundle for one team — events, rankings, matches, skills, and awards. This is what you pass to `TeamAnalyzer`.

```python
data = re.fetch_team_data("13155U")
data = re.fetch_team_data(167783)          # internal ID also works
data = re.fetch_team_data("13155U", season="180")  # different season
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team` | `str \| int` | required | Team number (e.g. `"13155U"`) or internal RobotEvents integer ID. Digit-only strings are treated as internal IDs; strings with letters are looked up by team number. |
| `season` | `str` | current season | RobotEvents season ID |

**Returns:** `dict` with keys:

| Key | Description |
|---|---|
| `team_id` | Internal RobotEvents integer ID |
| `team_number` | Human-readable team number string |
| `events` | All events this season |
| `past_events` | Past events only, sorted newest first |
| `rankings` | Ranking entries per event |
| `matches` | All matches played this season |
| `skills` | Skills run records |
| `awards` | Awards received |

Returns `{}` if the team cannot be resolved.

---

### `re.fetch_all_season_teams(season?)`

Fetch every registered VRC team for a season.

```python
teams = re.fetch_all_season_teams()
```

**Returns:** List of team dicts.

---

### `re.get_event_matches(event_id, division_id?)`

Fetch all matches for a single event.

```python
matches = re.get_event_matches(12345)
matches = re.get_event_matches(12345, division_id=2)  # Worlds divisions
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `event_id` | `int` | required | RobotEvents integer event ID |
| `division_id` | `int` | `1` | Division within the event. Use `1` for most tournaments; higher for Signature Events or Worlds |

**Returns:** List of match dicts.

---

### `re.get_event_rankings(event_id)`

Fetch final rankings for a single event.

```python
rankings = re.get_event_rankings(12345)
```

**Returns:** List of ranking dicts.

---

### `re.get_event_teams(event_id)`

Fetch all teams registered for a single event.

```python
teams = re.get_event_teams(12345)
```

**Returns:** List of team dicts.

---

### `re.get_event_skills(event_id)`

Fetch skills run results for a single event.

```python
skills = re.get_event_skills(12345)
```

**Returns:** List of skills run dicts.

---

### `re.get_matches_for_team_events(team_data, division_id?)`

Fetch all matches from every event a specific team attended, returned as a single flat chronologically-sorted list. This is the correct input for `TeamAnalyzer.elo()`.

Because it only fetches events the team actually competed at, it makes far fewer API calls than fetching the entire season (typically 5–15 requests instead of 1,800+).

```python
data          = re.fetch_team_data("13155U")
event_matches = re.get_matches_for_team_events(data)

ta = TeamAnalyzer("13155U", data)
print(ta.elo(event_matches))
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team_data` | `dict` | required | Dict returned by `fetch_team_data()` |
| `division_id` | `int` | `1` | Division number within each event |

**Returns:** Flat list of match dicts sorted by `started_at`.

---

### `re.batch_fetch_teams(teams, season?)`

Fetch season data for multiple teams in sequence.

```python
results = re.batch_fetch_teams(["13155U", "1234A", "392X"])
# {"13155U": {...}, "1234A": {...}, "392X": {...}}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `teams` | `list` | required | List of team number strings and/or internal integer IDs. Mixed lists are fine. |
| `season` | `str` | current season | RobotEvents season ID |

**Returns:** `dict` mapping each team identifier (as string) to its `fetch_team_data()` result.

---

## `EventAnalyzer`

Computes event-wide ratings from a list of match dicts. All computations are purely offline.

```python
matches = re.get_event_matches(event_id=12345)
ea      = EventAnalyzer(matches)
ea      = EventAnalyzer(matches, qual_only=False)  # include elim matches
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `matches` | `list` | required | List of match dicts in RobotEvents v2 format |
| `qual_only` | `bool` | `True` | When `True`, only qualification matches are used for all calculations |

---

### Ratings

#### `ea.opr()`

**Offensive Power Rating.** Estimates each team's individual scoring contribution using ordinary least squares across all qualification matches. OPR answers: *how many points does this team add to their alliance score per match?*

```python
opr = ea.opr()
# {"13155U": 65.4, "1234A": 58.2, ...}
```

**Returns:** `dict` mapping team number string to OPR float.

---

#### `ea.dpr()`

**Defensive Power Rating.** Points allowed per match. Computed by solving the OPR system using opponent alliance scores instead of the team's own score. Lower is better.

```python
dpr = ea.dpr()
```

**Returns:** `dict` mapping team number string to DPR float.

---

#### `ea.true_opr()`

**TrueOPR.** Offensive performance relative to field average. Computed as `OPR - mean(OPR)`. Positive means the team scores above the average team in the match pool; negative means below. A direct measure of offensive output vs. the field, not a defensive adjustment.

```python
true_opr = ea.true_opr()
```

**Returns:** `dict` mapping team number string to TrueOPR float.

---

#### `ea.true_dpr()`

**TrueDPR.** Defensive performance relative to field average. Positive means the team limits opponents more than the average team does.

```python
true_dpr = ea.true_dpr()
```

**Returns:** `dict` mapping team number string to TrueDPR float.

---

#### `ea.dsr()`

**Defensive Strength Rating.** Average OPR of all opponents faced. Higher means the team played against stronger competition. Same value as `strength_of_schedule()`.

```python
dsr = ea.dsr()
```

**Returns:** `dict` mapping team number string to DSR float.

---

#### `ea.epr()`

**Efficiency Power Rating.** OPR expressed as a fraction of the average match score. Represents each team's share of total points scored at the event.

```python
epr = ea.epr()
```

**Returns:** `dict` mapping team number string to EPR float.

---

#### `ea.hsf()`

**Human Skill Factor.** Each team's highest single-match alliance score. Reflects peak scoring potential.

```python
hsf = ea.hsf()
```

**Returns:** `dict` mapping team number string to HSF float.

---

#### `ea.apr()`

**Adjusted Power Rating.** Weighted blend: `0.6 × OPR + 0.25 × TrueOPR + 0.15 × EPR`. Used as the primary sort key in `full_rankings()`.

```python
apr = ea.apr()
```

**Returns:** `dict` mapping team number string to APR float.

---

#### `ea.elo_ratings(k?, initial?)`

**Event-scoped ELO.** Replays this event's qualification matches in chronological order. All teams start at `initial`. Good for within-event comparisons.

For season-wide ELO that accounts for opponent records at other events, use `TeamAnalyzer.elo()` instead.

```python
elo = ea.elo_ratings()
elo = ea.elo_ratings(k=16, initial=1200)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `k` | `float` | `32` | K-factor. Higher values make ratings respond faster to results. |
| `initial` | `float` | `1500` | Starting ELO for all teams |

**Returns:** `dict` mapping team number string to ELO float.

---

#### `ea.strength_of_schedule()`

Average OPR of all opponents faced. Alias for `dsr()`.

```python
sos = ea.strength_of_schedule()
```

**Returns:** `dict` mapping team number string to SOS float.

---

#### `ea.partner_strength()`

Average OPR of each team's alliance partners across all matches.

```python
ps = ea.partner_strength()
```

**Returns:** `dict` mapping team number string to average partner OPR float.

---

#### `ea.alliance_synergy()`

Ratio of actual alliance score to expected score (sum of partner OPRs). Values above `1.0` indicate the alliance consistently over-performs the sum of its parts.

```python
synergy = ea.alliance_synergy()
```

**Returns:** `dict` mapping team number string to synergy ratio float.

---

### Predictions

#### `ea.win_probability(red_teams, blue_teams)`

Predict win probability for a hypothetical match using ELO.

```python
p = ea.win_probability(["13155U", "1234A"], ["5678B", "9999C"])
# {"red": 0.6231, "blue": 0.3769}
```

| Parameter | Type | Description |
|---|---|---|
| `red_teams` | `list[str]` | Team number strings on the red alliance |
| `blue_teams` | `list[str]` | Team number strings on the blue alliance |

**Returns:** `dict` with keys `"red"` and `"blue"`, each a float probability between 0 and 1.

---

#### `ea.predict_score(red_teams, blue_teams)`

Predict expected alliance scores for a hypothetical match using OPR.

```python
scores = ea.predict_score(["13155U", "1234A"], ["5678B", "9999C"])
# {"red": 124.5, "blue": 108.3}
```

**Returns:** `dict` with keys `"red"` and `"blue"`, each a predicted score float.

---

#### `ea.simulate_tournament(alliance_pairings, n?)`

Monte Carlo tournament simulation. Simulates each match pairing `n` times using `win_probability()`.

```python
pairings = [
    (["13155U", "1234A"], ["5678B", "9999C"]),
    (["392X",   "7777A"], ["1111D", "2222B"]),
]
results = ea.simulate_tournament(pairings, n=5000)
# {"match_0_red": 0.612, "match_0_blue": 0.388, "match_1_red": 0.531, ...}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `alliance_pairings` | `list` | required | List of `([red_teams], [blue_teams])` tuples |
| `n` | `int` | `1000` | Number of simulations |

**Returns:** `dict` mapping `"match_N_red"` / `"match_N_blue"` to win fraction.

---

### Tables & Tools

#### `ea.full_rankings()`

Combined ranking table for all teams at the event, sorted by APR descending.

```python
rankings = ea.full_rankings()
for row in rankings:
    print(row["rank"], row["team"], row["apr"], row["opr"], row["elo"])
```

Each row contains: `rank`, `team`, `opr`, `dpr`, `true_opr`, `epr`, `apr`, `elo`, `sos`, `partner_strength`.

**Returns:** List of dicts, one per team.

---

#### `ea.draft_recommendations(my_team, picked?)`

Suggest the best available alliance partners for a given team. Ranked by a score weighted 70% APR and 30% alliance synergy.

```python
picks = ea.draft_recommendations("13155U")
picks = ea.draft_recommendations("13155U", picked=["1234A", "392X"])

for p in picks[:3]:
    print(p["team"], p["draft_score"])
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `my_team` | `str` | required | Team number string of the picking team |
| `picked` | `list[str]` | `[]` | Teams already picked or unavailable |

**Returns:** List of dicts sorted by `draft_score` descending. Each dict contains `team`, `apr`, `synergy`, `draft_score`.

---

#### `ea.detect_upsets(threshold?)`

Find matches where the underdog won. A match is an upset when the winning alliance had a pre-match ELO win probability below `threshold`.

```python
upsets = ea.detect_upsets()
upsets = ea.detect_upsets(threshold=0.3)

for u in upsets:
    print(u["match"], u["winner"], u["win_probability"])
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `threshold` | `float` | `0.25` | Maximum win probability for the winner to count as an upset |

**Returns:** List of dicts sorted by `upset_magnitude` descending. Each dict contains `match`, `winner`, `win_probability`, `upset_magnitude`, `red_score`, `blue_score`.

---

## `TeamAnalyzer`

Per-team analytics computed from a team's full season match history. All advanced ratings (OPR, DPR, APR, etc.) are computed internally from `self.qual_matches`, which contains every match across every event from `fetch_team_data()` — no `EventAnalyzer` needed.

```python
data = re.fetch_team_data("13155U")
ta   = TeamAnalyzer("13155U", data)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team` | `str` | required | Team number string, e.g. `"13155U"` |
| `data` | `dict` | required | Dict returned by `RobotEvents.fetch_team_data()` |
| `event_analyzer` | `EventAnalyzer` | `None` | Unused by most methods — kept for legacy compatibility |

---

### Offline Metrics

These require no external data and are always available.

---

#### `ta.average_match_score()`

Average alliance score across all qualification matches.

```python
ta.average_match_score()  # 97.4
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.average_match_performance()`

Average score margin (our alliance score minus opponent score). Positive means the team generally outscores opponents.

```python
ta.average_match_performance()  # 14.2
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.wlt_record()`

Win / Loss / Tie record for all qualification matches.

```python
record = ta.wlt_record()
# {"wins": 25, "losses": 14, "ties": 4, "total": 43, "win_pct": 0.5814}
```

**Returns:** `dict` with keys `wins`, `losses`, `ties`, `total`, `win_pct`.

---

#### `ta.win_rate()`

Qualification match win percentage.

```python
ta.win_rate()  # 0.5814
```

**Returns:** `float` between 0 and 1.

---

#### `ta.autonomous_performance()`

Autonomous Win Point (AWP) statistics from ranking data. Uses the `autonomous_win_point` boolean field from the RobotEvents rankings endpoint.

```python
auton = ta.autonomous_performance()
# {"awp_events": 3, "events_with_rankings": 5, "awp_rate": 0.6}
```

**Returns:** `dict` with keys `awp_events`, `events_with_rankings`, `awp_rate`.

---

#### `ta.carry_power()`

Scoring share metric. Average fraction of total match points (our score + opponent score) that our alliance contributed. Values above `0.5` indicate the team consistently outscores opponents.

```python
ta.carry_power()  # 0.526
```

**Returns:** `float` between 0 and 1, or `0.0` if no matches.

---

#### `ta.defensive_power()`

Average score margin as a fraction of total points in the match. Positive means the team outscores opponents; higher means more dominant.

```python
ta.defensive_power()  # 0.073
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.robot_scoring_capability()`

The 90th-percentile alliance score across all qualification matches. Represents peak scoring potential — what the team can do when performing near their best.

```python
ta.robot_scoring_capability()  # 133.0
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.alliance_independence()`

How consistently the team performs regardless of partner quality. Uses the inverse coefficient of variation of score margins. Higher values (closer to `1.0`) indicate the team performs similarly regardless of who their partner is.

```python
ta.alliance_independence()  # 0.71
```

**Returns:** `float` between 0 and 1. Returns `0.5` with fewer than 4 matches.

---

#### `ta.alliance_luck()`

How much better or worse than average the team's partners were, expressed as a z-score (standard deviations from field-average OPR among all teams in the match pool). Positive = lucky draw; negative = unlucky.

```python
luck = ta.alliance_luck()
# {"luck_score": 0.43, "avg_partner_opr": 52.1}
```

**Returns:** `dict` with keys `luck_score` (float) and `avg_partner_opr` (float).

---

#### `ta.alliance_luck_score()`

Convenience wrapper returning just the z-score from `alliance_luck()`.

```python
ta.alliance_luck_score()  # 0.43
```

**Returns:** `float`.

---

#### `ta.consistency_rating()`

Score consistency across all qualification matches. Computed as `1 - (std_dev / mean)` of alliance scores. `1.0` means perfectly consistent; values near `0` are highly variable.

```python
ta.consistency_rating()  # 0.262
```

**Returns:** `float` between 0 and 1.

---

#### `ta.peak_performance_score()`

Highest single alliance score recorded across all qualification matches.

```python
ta.peak_performance_score()  # 133.0
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.clutch_performance()`

Win rate in close matches, defined as matches with a score margin of 10 points or fewer.

```python
ta.clutch_performance()  # 0.6
```

**Returns:** `float` between 0 and 1. Returns `0.5` if no close matches were played.

---

### Advanced Ratings

All advanced ratings are computed from the team's full season match history (`self.qual_matches`), which spans all events. No `EventAnalyzer` is required.

---

#### `ta.opr()`

OPR computed from all of this team's matches across all events.

```python
ta.opr()  # 65.4
```

**Returns:** `float`, or `None` if fewer than 2 matches.

---

#### `ta.dpr()`

DPR computed from all of this team's matches across all events. Points allowed per match averaged across the full season.

```python
ta.dpr()  # 34.7
```

**Returns:** `float`, or `None` if fewer than 2 matches.

---

#### `ta.true_opr()`

TrueOPR computed from all matches across all events. `OPR - mean(OPR)` — offensive performance vs. field average.

```python
ta.true_opr()  # 30.7
```

**Returns:** `float`, or `None` if OPR or DPR is unavailable.

---

#### `ta.true_dpr()`

TrueDPR computed from all matches across all events. How much better the team limits opponents compared to the average DPR of all teams in their match history.

```python
ta.true_dpr()  # 15.4
```

**Returns:** `float`, or `None` if DPR is unavailable.

---

#### `ta.dsr()`

DSR computed from all matches across all events. Average OPR of every opponent faced across the full season. Alias for `sos()`.

```python
ta.dsr()  # 44.9
```

**Returns:** `float`, or `None` if no matches.

---

#### `ta.epr()`

EPR computed from all matches across all events. OPR expressed as a fraction of the average alliance score across all matches this team has played.

```python
ta.epr()  # 0.654
```

**Returns:** `float`, or `None` if OPR is unavailable.

---

#### `ta.hsf()`

Human Skill Factor computed from all matches across all events. The highest single alliance score recorded across every qualification match this team has ever played.

```python
ta.hsf()  # 133.0
```

**Returns:** `float`, or `0.0` if no matches.

---

#### `ta.apr()`

APR computed from all matches across all events. Weighted blend: `0.6 × OPR + 0.25 × TrueOPR + 0.15 × EPR`.

```python
ta.apr()  # 47.0
```

**Returns:** `float`, or `None` if any component is unavailable.

---

#### `ta.sos()`

Strength of Schedule computed from all matches across all events. Average OPR of every opponent faced across the full season.

```python
ta.sos()  # 44.9
```

**Returns:** `float`, or `None` if no matches.

---

#### `ta.partner_strength_avg()`

Average OPR of this team's alliance partners across all matches and events.

```python
ta.partner_strength_avg()  # 51.3
```

**Returns:** `float`, or `None` if no partner data.

---

#### `ta.opponent_strength_avg()`

Alias for `sos()`.

```python
ta.opponent_strength_avg()  # 44.9
```

---

### Global ELO

#### `ta.elo(all_event_matches, k?, initial?)`

Season-wide ELO rating computed from the full match pool.

All matches across all events are replayed chronologically in a single shared ELO pool. Opponents who dominated earlier events will already have elevated ELOs when you face them — beating them is worth proportionally more than beating a team with no prior record.

```python
event_matches = re.get_matches_for_team_events(data)
elo           = ta.elo(event_matches)
# 1623.4
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `all_event_matches` | `list` | required | Flat list of match dicts from one or more events, sorted chronologically. Use `re.get_matches_for_team_events(data)` to get this. |
| `k` | `float` | `32` | ELO K-factor |
| `initial` | `float` | `1500` | Starting ELO for every team |

**Returns:** `float` — this team's final ELO.

> **Note:** ELO is intentionally excluded from `summary()` because it requires the separate `all_event_matches` fetch. Compute it separately and add it to the summary dict if needed:
> ```python
> s = ta.summary()
> s["elo"] = ta.elo(event_matches)
> ```

---

### Match & Event Analysis

#### `ta.match_history_by_event()`

Group qualification matches by event ID.

```python
by_event = ta.match_history_by_event()
# {"12345": [match, match, ...], "67890": [...]}
```

**Returns:** `dict` mapping event ID string to list of match dicts.

---

#### `ta.event_performance_summary()`

Per-event performance breakdown across all events attended.

```python
summaries = ta.event_performance_summary()
for s in summaries:
    print(s["event_id"], s["wins"], s["losses"], s["avg_score"])
```

Each dict contains: `event_id`, `matches`, `wins`, `losses`, `ties`, `avg_score`, `max_score`, `min_score`.

**Returns:** List of dicts, one per event.

---

#### `ta.score_progression()`

Chronological list of match scores, showing the team's scoring trend over the season.

```python
progression = ta.score_progression()
for p in progression:
    print(p["match_num"], p["our_score"], p["opp_score"])
```

Each dict contains: `match_num`, `match_name`, `our_score`, `opp_score`, `event_id`.

**Returns:** List of dicts in chronological order.

---

#### `ta.summary()`

All computable metrics in a single dict. ELO is not included — call `ta.elo()` separately.

```python
s = ta.summary()
print(s["team"], s["opr"], s["win_pct"], s["apr"])
```

**Returns:** `dict` with the following keys:

| Key | Type | Description |
|---|---|---|
| `team` | `str` | Team number |
| `avg_match_score` | `float` | Average alliance score |
| `avg_match_performance` | `float` | Average score margin |
| `wins` | `int` | Qualification wins |
| `losses` | `int` | Qualification losses |
| `ties` | `int` | Qualification ties |
| `win_pct` | `float` | Win percentage |
| `awp_rate` | `float` | Autonomous win point rate |
| `carry_power` | `float` | Scoring share (0–1) |
| `defensive_power` | `float` | Margin as fraction of total score |
| `robot_scoring_cap` | `float` | 90th-percentile alliance score |
| `alliance_independence` | `float` | Consistency regardless of partner (0–1) |
| `alliance_luck` | `float` | Partner quality z-score |
| `consistency` | `float` | Score consistency (0–1) |
| `peak_score` | `float` | Highest alliance score ever |
| `clutch_performance` | `float` | Win rate in close matches |
| `opr` | `float \| None` | Offensive Power Rating |
| `dpr` | `float \| None` | Defensive Power Rating |
| `true_opr` | `float \| None` | OPR minus DPR |
| `true_dpr` | `float \| None` | Defensive performance vs. average |
| `dsr` | `float \| None` | Defensive Strength Rating |
| `epr` | `float \| None` | Efficiency Power Rating |
| `hsf` | `float` | Human Skill Factor |
| `apr` | `float \| None` | Adjusted Power Rating |
| `sos` | `float \| None` | Strength of Schedule |

---

## Utility Functions

### `clear_cache()`

Delete all locally cached API responses from `.pitline_cache/`.

```python
clear_cache()
```

---

### `export_to_csv(data, filepath)`

Export a list of dicts to a CSV file. Parent directories are created automatically.

```python
export_to_csv(ea.full_rankings(), "output/rankings.csv")
export_to_csv([ta.summary()],     "output/team.csv")
```

| Parameter | Type | Description |
|---|---|---|
| `data` | `list[dict]` | List of dicts with uniform keys |
| `filepath` | `str` | Destination path |

---

### `export_to_json(data, filepath)`

Export any JSON-serializable object to a file.

```python
export_to_json(ta.summary(),          "output/team.json")
export_to_json(ea.full_rankings(),    "output/rankings.json")
export_to_json(ta.score_progression(), "output/scores.json")
```

| Parameter | Type | Description |
|---|---|---|
| `data` | `any` | Any JSON-serializable object |
| `filepath` | `str` | Destination path |

---

### `normalize_scores(data, key)`

Add a min-max normalized field (`0`–`1`) to each row in a list of dicts. Adds a new key `"{key}_normalized"` alongside the original.

```python
rankings = ea.full_rankings()
rankings = normalize_scores(rankings, "opr")
rankings = normalize_scores(rankings, "apr")
# each row now also has "opr_normalized" and "apr_normalized"
```

| Parameter | Type | Description |
|---|---|---|
| `data` | `list[dict]` | List of dicts each containing `key` |
| `key` | `str` | Field to normalize |

**Returns:** The same list with `"{key}_normalized"` added to each row.

---

### `batch_analyze(team_data_map, event_matches?, all_event_matches?)`

Run `TeamAnalyzer.summary()` and optionally ELO for multiple teams at once. Returns results sorted by APR descending.

```python
teams    = re.batch_fetch_teams(["13155U", "1234A", "392X"])
matches  = re.get_event_matches(12345)
ev_m     = re.get_matches_for_team_events(teams["13155U"])

results  = batch_analyze(teams)
results  = batch_analyze(teams, event_matches=matches)
results  = batch_analyze(teams, event_matches=matches, all_event_matches=ev_m)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team_data_map` | `dict` | required | `{team_number: fetch_team_data() result}` |
| `event_matches` | `list` | `None` | When provided, used to build an `EventAnalyzer` shared across all teams |
| `all_event_matches` | `list` | `None` | When provided, `"elo"` is computed per team and added to each summary |

**Returns:** List of summary dicts sorted by APR descending.

---

## Rating Glossary

| Rating | Full Name | Description |
|---|---|---|
| OPR | Offensive Power Rating | Estimated individual scoring contribution via least squares |
| DPR | Defensive Power Rating | Estimated points allowed per match |
| TrueOPR | True Offensive Power Rating | OPR minus field-average OPR — offensive output vs. the field |
| TrueDPR | True Defensive Power Rating | How much better a team limits opponents vs. field average |
| DSR | Defensive Strength Rating | Average OPR of opponents faced |
| EPR | Efficiency Power Rating | OPR as a fraction of average match score |
| HSF | Human Skill Factor | Peak single-match alliance score |
| APR | Adjusted Power Rating | Weighted blend of OPR, TrueOPR, and EPR |
| SOS | Strength of Schedule | Average OPR of all opponents faced (same as DSR) |
| ELO | ELO Rating | Skill rating updated per match based on expected vs. actual result |