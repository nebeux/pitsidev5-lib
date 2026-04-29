import time
import math
import json
import csv
import random
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional

import requests

CURRENT_SEASON = "197"
BASE_URL       = "https://www.robotevents.com/api/v2"
REQUEST_DELAY  = 0.5
CACHE_DIR      = Path(".pitline_cache")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("pitline")


def _cache_path(path: str, params: dict) -> Path:
    key = hashlib.md5(f"{path}{json.dumps(params or {}, sort_keys=True)}".encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _get_all(path: str, headers: dict, params: dict = None,
             retries: int = 3, use_cache: bool = True, cache_ttl: int = 3600) -> list:
    cf = _cache_path(path, params or {})
    if use_cache and cf.exists() and (time.time() - cf.stat().st_mtime) < cache_ttl:
        return json.loads(cf.read_text())

    all_data, page = [], 1
    while True:
        for attempt in range(retries):
            try:
                r = requests.get(
                    f"{BASE_URL}{path}",
                    headers=headers,
                    params={**(params or {}), "per_page": 250, "page": page},
                    timeout=15,
                )
                if r.status_code in (429, 403):
                    wait = 10 * (attempt + 1)
                    log.warning("Rate limited on %s — waiting %ds", path, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except requests.HTTPError as e:
                if attempt == retries - 1:
                    log.error("HTTP error on %s page %d: %s", path, page, e)
                    return all_data
                time.sleep(5)
            except Exception as e:
                log.error("Error on %s page %d: %s", path, page, e)
                return all_data
        else:
            return all_data

        all_data.extend(data.get("data", []))
        meta = data.get("meta", {})
        if meta.get("current_page", 1) >= meta.get("last_page", 1):
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(all_data))

    return all_data


def clear_cache():
    """Delete all locally cached API responses."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
        log.info("Cache cleared.")


def _team_key(team_obj) -> str:
    if isinstance(team_obj, dict):
        return str(team_obj.get("name") or team_obj.get("number") or team_obj.get("id") or "")
    return str(team_obj)


def _alliance_teams(alliance: dict) -> list:
    out = []
    for t in alliance.get("teams", []):
        inner = t.get("team", t)
        k = _team_key(inner)
        if k:
            out.append(k)
    return out


def _alliance_score(alliance: dict) -> float:
    return float(alliance.get("score", 0) or 0)


def _get_alliances(match: dict) -> dict:
    raw = match.get("alliances", [])
    if isinstance(raw, dict):
        return raw
    return {a["color"]: a for a in raw if "color" in a}


def _find_team_sides(team_key: str, match: dict):
    alliances = _get_alliances(match)
    for side, opp_side in [("red", "blue"), ("blue", "red")]:
        ally = alliances.get(side, {})
        if team_key in _alliance_teams(ally):
            return ally, alliances.get(opp_side, {})
    return None


def _is_qual(match: dict) -> bool:
    return match.get("round") in (2, "Qualification", "qual", "qualifier")


class RobotEvents:
    """
    HTTP client for the RobotEvents v2 API.

    Handles authentication, pagination, rate limiting, and local disk caching.
    All responses are cached to `.pitside_cache/` by default. Set `use_cache=False`
    to always fetch fresh data, or call `clear_cache()` to wipe cached files.

    Args:
        api_key:   Your RobotEvents API bearer token.
        use_cache: Whether to read/write cached API responses. Default True.
        cache_ttl: How long cached responses are considered fresh, in seconds. Default 3600.
    """

    def __init__(self, api_key: str, use_cache: bool = True, cache_ttl: int = 3600):
        self.api_key   = api_key
        self.use_cache = use_cache
        self.cache_ttl = cache_ttl
        self._headers  = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> list:
        return _get_all(path, self._headers, params,
                        use_cache=self.use_cache, cache_ttl=self.cache_ttl)

    def get_team(self, team_number: str) -> Optional[dict]:
        """
        Fetch public team info by team number string.

        Args:
            team_number: The human-readable team number, e.g. ``"13155U"`` or ``"1234A"``.

        Returns:
            A dict of team info from RobotEvents, or ``None`` if not found.
        """
        results = self._get("/teams", {"number[]": team_number, "program[]": 1})
        return results[0] if results else None

    def _resolve_team_id(self, team) -> Optional[int]:
        team_str = str(team).strip()
        if team_str.isdigit():
            return int(team_str)
        info = self.get_team(team_str)
        if info is None:
            log.error("Could not find team '%s' on RobotEvents.", team_str)
            return None
        internal_id = info.get("id")
        log.info("Resolved '%s' -> internal ID %s", team_str, internal_id)
        return int(internal_id)

    def fetch_all_season_teams(self, season: str = CURRENT_SEASON) -> list:
        """
        Fetch every registered VRC team for a given season.

        Args:
            season: RobotEvents season ID string. Defaults to the current season.

        Returns:
            List of team dicts.
        """
        log.info("Fetching all VRC teams for season %s...", season)
        teams = self._get("/teams", {"program[]": 1, "season[]": season, "registered": True})
        log.info("Found %d teams.", len(teams))
        return teams

    def fetch_team_data(self, team, season: str = CURRENT_SEASON) -> dict:
        """
        Fetch a full season data bundle for one team.

        Retrieves events, rankings, matches, skills, and awards in a single call,
        then returns them as a dict ready to pass into ``TeamAnalyzer``.

        Args:
            team:   Team number string (e.g. ``"13155U"``) or internal RobotEvents
                    integer ID. Strings containing only digits are treated as
                    internal IDs; strings with letters are looked up by team number.
            season: RobotEvents season ID string. Defaults to the current season.

        Returns:
            Dict with keys: ``team_id``, ``team_number``, ``events``, ``past_events``,
            ``rankings``, ``matches``, ``skills``, ``awards``.
            Returns an empty dict if the team cannot be resolved.
        """
        team_id = self._resolve_team_id(team)
        if team_id is None:
            return {}

        log.info("Fetching data for team '%s' (ID %d, season %s)...", team, team_id, season)
        p        = {"season[]": season}
        events   = self._get(f"/teams/{team_id}/events",   p);  time.sleep(REQUEST_DELAY)
        rankings = self._get(f"/teams/{team_id}/rankings", p);  time.sleep(REQUEST_DELAY)
        matches  = self._get(f"/teams/{team_id}/matches",  p);  time.sleep(REQUEST_DELAY)
        skills   = self._get(f"/teams/{team_id}/skills",   p);  time.sleep(REQUEST_DELAY)
        awards   = self._get(f"/teams/{team_id}/awards",   p);  time.sleep(REQUEST_DELAY)

        now = datetime.now(timezone.utc).isoformat()
        past_events = sorted(
            [e for e in events if e.get("start") and e["start"] <= now],
            key=lambda e: e["start"], reverse=True,
        )
        return {
            "team_id":     team_id,
            "team_number": str(team) if not str(team).isdigit() else None,
            "events":      events,
            "past_events": past_events,
            "rankings":    rankings,
            "matches":     matches,
            "skills":      skills,
            "awards":      awards,
        }

    def get_event_matches(self, event_id: int, division_id: int = 1) -> list:
        """
        Fetch all matches for a single event.

        Args:
            event_id:    The RobotEvents integer event ID.
            division_id: Division number within the event. ``1`` covers most
                         regular tournaments. Use higher values for Signature
                         Events or Worlds which have multiple divisions.

        Returns:
            List of match dicts.
        """
        return self._get(f"/events/{event_id}/divisions/{division_id}/matches")

    def get_event_rankings(self, event_id: int) -> list:
        """
        Fetch final rankings for a single event.

        Args:
            event_id: The RobotEvents integer event ID.

        Returns:
            List of ranking dicts.
        """
        return self._get(f"/events/{event_id}/rankings")

    def get_event_teams(self, event_id: int) -> list:
        """
        Fetch all teams registered for a single event.

        Args:
            event_id: The RobotEvents integer event ID.

        Returns:
            List of team dicts.
        """
        return self._get(f"/events/{event_id}/teams")

    def get_event_skills(self, event_id: int) -> list:
        """
        Fetch skills results for a single event.

        Args:
            event_id: The RobotEvents integer event ID.

        Returns:
            List of skills run dicts.
        """
        return self._get(f"/events/{event_id}/skills")

    def get_matches_for_team_events(self, team_data: dict,
                                    division_id: int = 1) -> list:
        """
        Fetch all matches from every event a specific team attended, then return
        them as a single flat chronologically-sorted list.

        This is the correct input for ``TeamAnalyzer.elo()``. Because only the
        events that team actually competed at are fetched, it makes far fewer API
        calls than fetching the entire season — typically 5–15 requests instead
        of 1,800+.

        Args:
            team_data:   The dict returned by ``fetch_team_data()``. The
                         ``past_events`` key is used to determine which events
                         to fetch.
            division_id: Division number within each event. Default ``1``.

        Returns:
            Flat list of all match dicts, sorted by ``started_at``.

        Example::

            data              = re.fetch_team_data("13155U")
            all_event_matches = re.get_matches_for_team_events(data)
            ta                = TeamAnalyzer("13155U", data)
            print(ta.elo(all_event_matches))
        """
        past_events = team_data.get("past_events") or team_data.get("events") or []
        all_matches = []

        for i, event in enumerate(past_events):
            eid = event.get("id")
            if not eid:
                continue
            log.info("Fetching matches for event %d/%d (id=%d)...", i + 1, len(past_events), eid)
            matches = self.get_event_matches(eid, division_id)
            all_matches.extend(matches)
            time.sleep(REQUEST_DELAY)

        all_matches.sort(key=lambda m: m.get("started_at") or m.get("scheduled") or "")
        log.info("Fetched %d total matches across %d events.", len(all_matches), len(past_events))
        return all_matches

    def batch_fetch_teams(self, teams: list, season: str = CURRENT_SEASON) -> dict:
        """
        Fetch season data for multiple teams in sequence.

        Args:
            teams:  List of team number strings and/or internal integer IDs.
                    Mixed lists are fine, e.g. ``["13155U", "1234A", 167783]``.
            season: RobotEvents season ID string. Defaults to the current season.

        Returns:
            Dict mapping each input team identifier (as a string) to its
            ``fetch_team_data()`` result dict.
        """
        results = {}
        for i, team in enumerate(teams):
            log.info("Batch fetch %d/%d: '%s'", i + 1, len(teams), team)
            data = self.fetch_team_data(team, season)
            if data:
                results[str(team)] = data
            time.sleep(REQUEST_DELAY)
        return results


class EventAnalyzer:
    """
    Compute event-wide ratings from a list of match dicts.

    All computations are purely offline. Pass in whatever matches you have —
    either from ``RobotEvents.get_event_matches()`` or hand-crafted for testing.

    Ratings computed:

    - **OPR** — Offensive Power Rating (least-squares estimated scoring contribution)
    - **DPR** — Defensive Power Rating (points allowed per match)
    - **TrueOPR** — OPR minus DPR
    - **TrueDPR** — how much a team limits opponents vs. field average
    - **DSR** — Defensive Strength Rating (average OPR of opponents faced)
    - **EPR** — Efficiency Power Rating (OPR as fraction of average match score)
    - **HSF** — High Score Factor (peak single-match alliance score)
    - **APR** — Adjusted Power Rating (weighted blend of OPR, TrueOPR, EPR)
    - **ELO** — event-scoped ELO replayed chronologically

    Args:
        matches:    List of match dicts in RobotEvents v2 format.
        qual_only:  If ``True`` (default), only qualification matches are used.

    Example::

        matches = re.get_event_matches(event_id=12345)
        ea      = EventAnalyzer(matches)

        print(ea.opr())
        print(ea.full_rankings())
        print(ea.win_probability(["13155U", "1234A"], ["5678B", "9999C"]))
    """

    def __init__(self, matches: list, qual_only: bool = True):
        self.all_matches = matches
        self.matches     = [m for m in matches if _is_qual(m)] if qual_only else matches
        self._teams      = self._extract_teams()
        self._opr_cache  = None
        self._dpr_cache  = None
        self._elo_cache  = None

    def _extract_teams(self) -> list:
        teams = set()
        for m in self.matches:
            for side in ("red", "blue"):
                for tk in _alliance_teams(_get_alliances(m).get(side, {})):
                    teams.add(tk)
        return sorted(teams)

    def _solve_lls(self, score_fn) -> dict:
        teams = self._teams
        if not teams:
            return {}

        idx    = {t: i for i, t in enumerate(teams)}
        n      = len(teams)
        rows_A = []
        rows_b = []

        for m in self.matches:
            for side in ("red", "blue"):
                ally    = _get_alliances(m).get(side, {})
                score   = score_fn(ally)
                row     = [0.0] * n
                members = _alliance_teams(ally)
                if not members:
                    continue
                for tk in members:
                    if tk in idx:
                        row[idx[tk]] = 1.0
                rows_A.append(row)
                rows_b.append(score)

        if not rows_A:
            return {}

        A, b = rows_A, rows_b
        AtA  = [[0.0] * n for _ in range(n)]
        Atb  = [0.0] * n

        for row_i, row in enumerate(A):
            for j, val in enumerate(row):
                Atb[j] += val * b[row_i]
                for k, val2 in enumerate(row):
                    AtA[j][k] += val * val2

        aug = [AtA[i][:] + [Atb[i]] for i in range(n)]
        for col in range(n):
            max_row       = max(range(col, n), key=lambda r: abs(aug[r][col]))
            aug[col], aug[max_row] = aug[max_row], aug[col]
            if abs(aug[col][col]) < 1e-12:
                continue
            pivot = aug[col][col]
            for row in range(n):
                if row != col:
                    factor = aug[row][col] / pivot
                    for k in range(n + 1):
                        aug[row][k] -= factor * aug[col][k]
            for k in range(n + 1):
                aug[col][k] /= pivot

        return {teams[i]: round(aug[i][n], 4) for i in range(n)}

    def opr(self) -> dict:
        """
        Offensive Power Rating.

        Estimates each team's individual contribution to their alliance's score
        using ordinary least squares across all qualification matches.

        Returns:
            Dict mapping team number string to OPR float.
        """
        if self._opr_cache is None:
            self._opr_cache = self._solve_lls(lambda a: _alliance_score(a))
        return self._opr_cache

    def dpr(self) -> dict:
        """
        Defensive Power Rating.

        Estimated points allowed per match. Computed by solving OPR on the
        opponent's score rather than the alliance's own score. Lower is better.

        Returns:
            Dict mapping team number string to DPR float.
        """
        if self._dpr_cache is None:
            flipped = []
            for m in self.matches:
                red          = _get_alliances(m).get("red",  {})
                blue         = _get_alliances(m).get("blue", {})
                flipped_red  = {**red,  "score": _alliance_score(blue)}
                flipped_blue = {**blue, "score": _alliance_score(red)}
                flipped.append({**m, "alliances": {"red": flipped_red, "blue": flipped_blue}})
            orig, self.matches = self.matches, flipped
            self._dpr_cache    = self._solve_lls(lambda a: _alliance_score(a))
            self.matches       = orig
        return self._dpr_cache

    def true_opr(self) -> dict:
        """
        TrueOPR — offensive performance relative to field average.

        Computed as ``OPR - mean(OPR)``. Positive means the team scores more
        than the average team in this match pool; negative means below average.
        A direct measure of offensive output vs. the field.

        Returns:
            Dict mapping team number string to TrueOPR float.
        """
        o    = self.opr()
        vals = list(o.values())
        avg  = sum(vals) / len(vals) if vals else 0
        return {t: round(o.get(t, 0) - avg, 4) for t in self._teams}

    def true_dpr(self) -> dict:
        """
        TrueDPR — defensive performance relative to field average.

        Positive means the team limits opponents more than average.

        Returns:
            Dict mapping team number string to TrueDPR float.
        """
        d   = self.dpr()
        avg = sum(d.values()) / len(d) if d else 0
        return {t: round(avg - d.get(t, 0), 4) for t in self._teams}

    def dsr(self) -> dict:
        """
        Defensive Strength Rating.

        Average OPR of all opponents faced. Higher means the team played
        against stronger competition.

        Returns:
            Dict mapping team number string to DSR float.
        """
        opr      = self.opr()
        opp_oprs = defaultdict(list)
        for m in self.matches:
            red_teams  = _alliance_teams(_get_alliances(m).get("red",  {}))
            blue_teams = _alliance_teams(_get_alliances(m).get("blue", {}))
            for t in red_teams:
                for o in blue_teams:
                    opp_oprs[t].append(opr.get(o, 0))
            for t in blue_teams:
                for o in red_teams:
                    opp_oprs[t].append(opr.get(o, 0))
        return {t: round(sum(v) / len(v), 4) if v else 0.0 for t, v in opp_oprs.items()}

    def epr(self) -> dict:
        """
        Efficiency Power Rating.

        OPR expressed as a fraction of the average match score. Represents
        each team's share of total points scored at the event.

        Returns:
            Dict mapping team number string to EPR float.
        """
        o          = self.opr()
        all_scores = [
            _alliance_score(_get_alliances(m).get(s, {}))
            for m in self.matches for s in ("red", "blue")
        ]
        avg_score = sum(all_scores) / len(all_scores) if all_scores else 1
        return {t: round(v / avg_score, 4) for t, v in o.items()}

    def hsf(self) -> dict:
        """
        Human Skill Factor.

        Each team's highest single-match alliance score across the event.
        Reflects peak scoring potential — the best the team has looked on
        the field in a single match.

        Returns:
            Dict mapping team number string to HSF float.
        """
        best = defaultdict(float)
        for m in self.matches:
            for side in ("red", "blue"):
                ally  = _get_alliances(m).get(side, {})
                score = _alliance_score(ally)
                for t in _alliance_teams(ally):
                    best[t] = max(best[t], score)
        return {t: best.get(t, 0.0) for t in self._teams}

    def apr(self) -> dict:
        """
        Adjusted Power Rating.

        Weighted blend: ``0.6 * OPR + 0.25 * TrueOPR + 0.15 * EPR``.
        Used as the primary sort key in ``full_rankings()``.

        Returns:
            Dict mapping team number string to APR float.
        """
        o, to, ep = self.opr(), self.true_opr(), self.epr()
        return {
            t: round(0.6 * o.get(t, 0) + 0.25 * to.get(t, 0) + 0.15 * ep.get(t, 0), 4)
            for t in self._teams
        }

    def elo_ratings(self, k: float = 32, initial: float = 1500.0) -> dict:
        """
        Event-scoped ELO ratings.

        Replays this event's qualification matches in chronological order.
        All teams start at ``initial``. Good for within-event comparisons.

        For season-wide ELO that correctly accounts for opponent records at
        other events, use ``TeamAnalyzer.elo()`` instead.

        Args:
            k:       ELO K-factor. Higher values make ratings change faster. Default 32.
            initial: Starting ELO for all teams. Default 1500.

        Returns:
            Dict mapping team number string to ELO float.
        """
        if self._elo_cache is not None:
            return self._elo_cache

        elo     = defaultdict(lambda: initial)
        ordered = sorted(self.matches,
                         key=lambda m: m.get("started_at") or m.get("scheduled") or "")

        for m in ordered:
            alliances = _get_alliances(m)
            red       = _alliance_teams(alliances.get("red",  {}))
            blue      = _alliance_teams(alliances.get("blue", {}))
            if not red or not blue:
                continue

            r_score = _alliance_score(alliances.get("red",  {}))
            b_score = _alliance_score(alliances.get("blue", {}))
            r_avg   = sum(elo[t] for t in red)  / len(red)
            b_avg   = sum(elo[t] for t in blue) / len(blue)
            exp_r   = 1 / (1 + 10 ** ((b_avg - r_avg) / 400))
            exp_b   = 1 - exp_r

            if r_score > b_score:   s_r, s_b = 1.0, 0.0
            elif r_score < b_score: s_r, s_b = 0.0, 1.0
            else:                   s_r, s_b = 0.5, 0.5

            for t in red:  elo[t] += k * (s_r - exp_r)
            for t in blue: elo[t] += k * (s_b - exp_b)

        self._elo_cache = {t: round(elo[t], 2) for t in self._teams}
        return self._elo_cache

    def strength_of_schedule(self) -> dict:
        """
        Strength of Schedule.

        Alias for ``dsr()``. Returns the average OPR of all opponents faced.

        Returns:
            Dict mapping team number string to SOS float.
        """
        return self.dsr()

    def partner_strength(self) -> dict:
        """
        Average OPR of each team's alliance partners across all matches.

        Returns:
            Dict mapping team number string to average partner OPR float.
        """
        opr          = self.opr()
        partner_oprs = defaultdict(list)
        for m in self.matches:
            for side in ("red", "blue"):
                members = _alliance_teams(_get_alliances(m).get(side, {}))
                for t in members:
                    for p in members:
                        if p != t:
                            partner_oprs[t].append(opr.get(p, 0))
        return {t: round(sum(v) / len(v), 4) if v else 0.0 for t, v in partner_oprs.items()}

    def alliance_synergy(self) -> dict:
        """
        Alliance synergy score.

        Ratio of actual alliance score to expected score (sum of partner OPRs).
        Values above 1.0 indicate the alliance consistently over-performs the
        sum of its parts.

        Returns:
            Dict mapping team number string to synergy ratio float.
        """
        opr     = self.opr()
        synergy = defaultdict(list)
        for m in self.matches:
            for side in ("red", "blue"):
                ally     = _get_alliances(m).get(side, {})
                members  = _alliance_teams(ally)
                expected = sum(opr.get(t, 0) for t in members)
                actual   = _alliance_score(ally)
                if expected > 0:
                    ratio = actual / expected
                    for t in members:
                        synergy[t].append(ratio)
        return {t: round(sum(v) / len(v), 4) if v else 1.0 for t, v in synergy.items()}

    def win_probability(self, red_teams: list, blue_teams: list) -> dict:
        """
        Predict win probability for a hypothetical match using ELO.

        Args:
            red_teams:  List of team number strings on the red alliance.
            blue_teams: List of team number strings on the blue alliance.

        Returns:
            Dict with keys ``"red"`` and ``"blue"``, each a probability float (0–1).
        """
        elo   = self.elo_ratings()
        r_avg = sum(elo.get(t, 1500) for t in red_teams)  / max(len(red_teams),  1)
        b_avg = sum(elo.get(t, 1500) for t in blue_teams) / max(len(blue_teams), 1)
        p_red = 1 / (1 + 10 ** ((b_avg - r_avg) / 400))
        return {"red": round(p_red, 4), "blue": round(1 - p_red, 4)}

    def predict_score(self, red_teams: list, blue_teams: list) -> dict:
        """
        Predict expected alliance scores for a hypothetical match using OPR.

        Args:
            red_teams:  List of team number strings on the red alliance.
            blue_teams: List of team number strings on the blue alliance.

        Returns:
            Dict with keys ``"red"`` and ``"blue"``, each a predicted score float.
        """
        opr        = self.opr()
        red_score  = sum(opr.get(t, 0) for t in red_teams)
        blue_score = sum(opr.get(t, 0) for t in blue_teams)
        return {"red": round(red_score, 2), "blue": round(blue_score, 2)}

    def simulate_tournament(self, alliance_pairings: list, n: int = 1000) -> dict:
        """
        Monte Carlo tournament simulation.

        Simulates each match pairing ``n`` times using ``win_probability()``
        and returns the fraction of simulations each alliance won.

        Args:
            alliance_pairings: List of ``([red_teams], [blue_teams])`` tuples.
            n:                 Number of simulations to run. Default 1000.

        Returns:
            Dict mapping ``"match_N_red"`` / ``"match_N_blue"`` to win fraction.
        """
        wins = defaultdict(int)
        for _ in range(n):
            for i, (red, blue) in enumerate(alliance_pairings):
                p      = self.win_probability(red, blue)
                winner = "red" if random.random() < p["red"] else "blue"
                wins[f"match_{i}_{winner}"] += 1
        return {k: round(v / n, 4) for k, v in wins.items()}

    def full_rankings(self) -> list:
        """
        Combined ranking table for all teams at the event, sorted by APR.

        Each row includes: ``rank``, ``team``, ``opr``, ``dpr``, ``true_opr``,
        ``epr``, ``apr``, ``elo``, ``sos``, ``partner_strength``.

        Returns:
            List of dicts, one per team, sorted by APR descending.
        """
        opr  = self.opr()
        dpr  = self.dpr()
        topr = self.true_opr()
        epr  = self.epr()
        apr  = self.apr()
        elo  = self.elo_ratings()
        sos  = self.strength_of_schedule()
        ps   = self.partner_strength()

        rows = []
        for t in self._teams:
            rows.append({
                "team":             t,
                "opr":              opr.get(t,  0),
                "dpr":              dpr.get(t,  0),
                "true_opr":         topr.get(t, 0),
                "epr":              epr.get(t,  0),
                "apr":              apr.get(t,  0),
                "elo":              elo.get(t,  1500),
                "sos":              sos.get(t,  0),
                "partner_strength": ps.get(t,   0),
            })
        rows.sort(key=lambda r: r["apr"], reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return rows

    def draft_recommendations(self, my_team: str, picked: list = None) -> list:
        """
        Suggest the best available alliance partners for a given team.

        Partners are ranked by a draft score weighted 70% APR and 30% alliance
        synergy. Teams in the ``picked`` list are excluded.

        Args:
            my_team: Team number string of the picking team.
            picked:  List of team number strings already picked or unavailable.

        Returns:
            List of candidate dicts sorted by ``draft_score`` descending.
            Each dict contains ``team``, ``apr``, ``synergy``, ``draft_score``.
        """
        picked  = set(picked or [])
        picked.add(my_team)
        apr     = self.apr()
        synergy = self.alliance_synergy()

        candidates = []
        for t in self._teams:
            if t in picked:
                continue
            score = apr.get(t, 0) * 0.7 + synergy.get(t, 1.0) * 10 * 0.3
            candidates.append({
                "team":        t,
                "apr":         apr.get(t,     0),
                "synergy":     synergy.get(t, 1.0),
                "draft_score": round(score,   4),
            })
        candidates.sort(key=lambda x: x["draft_score"], reverse=True)
        return candidates

    def detect_upsets(self, threshold: float = 0.25) -> list:
        """
        Find matches where the underdog won.

        A match is an upset when the winning alliance had a pre-match ELO-based
        win probability below ``threshold``.

        Args:
            threshold: Maximum win probability for the winner to qualify as an
                       upset. Default 0.25.

        Returns:
            List of upset dicts sorted by ``upset_magnitude`` descending.
            Each dict contains ``match``, ``winner``, ``win_probability``,
            ``upset_magnitude``, ``red_score``, ``blue_score``.
        """
        upsets = []
        for m in self.matches:
            red    = _alliance_teams(_get_alliances(m).get("red",  {}))
            blue   = _alliance_teams(_get_alliances(m).get("blue", {}))
            if not red or not blue:
                continue
            p      = self.win_probability(red, blue)
            r_s    = _alliance_score(_get_alliances(m).get("red",  {}))
            b_s    = _alliance_score(_get_alliances(m).get("blue", {}))
            winner = "red" if r_s > b_s else ("blue" if b_s > r_s else None)
            if winner and p[winner] < threshold:
                upsets.append({
                    "match":           m.get("name") or m.get("id"),
                    "winner":          winner,
                    "win_probability": p[winner],
                    "upset_magnitude": round(1 - p[winner], 4),
                    "red_score":       r_s,
                    "blue_score":      b_s,
                })
        upsets.sort(key=lambda x: x["upset_magnitude"], reverse=True)
        return upsets


class TeamAnalyzer:
    """
    Per-team analytics computed from a team's match history.

    Offline metrics (win/loss, scoring, consistency, etc.) are always available.
    Advanced ratings (OPR, DPR, APR, etc.) require an ``EventAnalyzer`` to be
    passed in. Season-wide ELO requires a flat list of all event matches passed
    to ``elo()``.

    Args:
        team:           Team number string, e.g. ``"13155U"``.
        data:           Dict returned by ``RobotEvents.fetch_team_data()``.
        event_analyzer: Optional ``EventAnalyzer`` instance built from the
                        event matches. Enables OPR, DPR, APR, SOS, and
                        alliance luck in ``summary()``.

    Example — offline only::

        data = re.fetch_team_data("13155U")
        ta   = TeamAnalyzer("13155U", data)
        print(ta.summary())

    Example — with event ratings::

        ea = EventAnalyzer(re.get_event_matches(event_id=12345))
        ta = TeamAnalyzer("13155U", data, event_analyzer=ea)
        print(ta.summary())

    Example — with global ELO::

        event_matches = re.get_matches_for_team_events(data)
        print(ta.elo(event_matches))
    """

    def __init__(self, team: str, data: dict, event_analyzer: EventAnalyzer = None):
        self.team         = str(team)
        self.data         = data
        self.matches      = data.get("matches", [])
        self.rankings     = data.get("rankings", [])
        self.skills       = data.get("skills",   [])
        self.events       = data.get("events",   [])
        self.awards       = data.get("awards",   [])
        self.ea           = event_analyzer
        self.qual_matches = [m for m in self.matches if _is_qual(m)]

    def _parsed_matches(self) -> list:
        out = []
        for m in self.qual_matches:
            sides = _find_team_sides(self.team, m)
            if sides is None:
                continue
            our, opp  = sides
            our_score = _alliance_score(our)
            opp_score = _alliance_score(opp)
            result    = "W" if our_score > opp_score else ("L" if our_score < opp_score else "T")
            out.append({
                "our_score":    our_score,
                "opp_score":    opp_score,
                "result":       result,
                "margin":       our_score - opp_score,
                "our_alliance": our,
                "opp_alliance": opp,
                "match":        m,
                "event_id":     (m.get("event") or {}).get("id"),
            })
        return out

    def average_match_score(self) -> float:
        """
        Average alliance score across all qualification matches.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        return round(sum(p["our_score"] for p in pm) / len(pm), 2)

    def average_match_performance(self) -> float:
        """
        Average score margin (our alliance score minus opponent score).

        Positive means the team generally outscores opponents.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        return round(sum(p["margin"] for p in pm) / len(pm), 2)

    def wlt_record(self) -> dict:
        """
        Win / Loss / Tie record for qualification matches.

        Returns:
            Dict with keys ``wins``, ``losses``, ``ties``, ``total``, ``win_pct``.
        """
        pm    = self._parsed_matches()
        w     = sum(1 for p in pm if p["result"] == "W")
        l     = sum(1 for p in pm if p["result"] == "L")
        t     = sum(1 for p in pm if p["result"] == "T")
        total = len(pm)
        return {"wins": w, "losses": l, "ties": t, "total": total,
                "win_pct": round(w / total, 4) if total else 0.0}

    def win_rate(self) -> float:
        """
        Qualification match win percentage as a float between 0 and 1.
        """
        return self.wlt_record()["win_pct"]

    def autonomous_performance(self) -> dict:
        """
        Autonomous Win Point (AWP) statistics from ranking data.

        Uses the ``autonomous_win_point`` boolean field from the RobotEvents
        rankings endpoint. Falls back gracefully if the field is absent.

        Returns:
            Dict with keys ``awp_events``, ``events_with_rankings``, ``awp_rate``.
        """
        awp_events   = 0
        total_events = 0
        for rk in self.rankings:
            total_events += 1
            if rk.get("autonomous_win_point"):
                awp_events += 1
        return {
            "awp_events":           awp_events,
            "events_with_rankings": total_events,
            "awp_rate":             round(awp_events / total_events, 4) if total_events else 0.0,
        }

    def carry_power(self) -> float:
        """
        Scoring share metric.

        Average fraction of total match points (our score + opponent score)
        that our alliance contributed. Values above 0.5 indicate the team
        consistently outscores opponents.

        Returns:
            Float between 0 and 1, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        ratios = []
        for p in pm:
            total = p["our_score"] + p["opp_score"]
            if total > 0:
                ratios.append(p["our_score"] / total)
        return round(sum(ratios) / len(ratios), 4) if ratios else 0.0

    def defensive_power(self) -> float:
        """
        Defensive dominance metric.

        Average score margin as a fraction of total points in the match.
        Positive means the team outscores opponents; higher is more dominant.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        margins   = [p["margin"] for p in pm]
        totals    = [p["our_score"] + p["opp_score"] for p in pm]
        avg_total = sum(totals) / len(totals) if totals else 1
        return round(sum(margins) / len(margins) / avg_total, 4) if avg_total else 0.0

    def robot_scoring_capability(self) -> float:
        """
        Peak scoring potential.

        The 90th-percentile alliance score across all qualification matches,
        representing what the team can do when performing near their best.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        scores = sorted(p["our_score"] for p in pm)
        idx    = min(int(len(scores) * 0.9), len(scores) - 1)
        return float(scores[idx])

    def alliance_independence(self) -> float:
        """
        Consistency of performance regardless of partner quality.

        Uses the inverse coefficient of variation of score margins.
        Higher values (closer to 1.0) indicate the team performs similarly
        regardless of who their partner is.

        Returns:
            Float between 0 and 1. Returns ``0.5`` with fewer than 4 matches.
        """
        pm = self._parsed_matches()
        if len(pm) < 4: return 0.5
        margins = [p["margin"] for p in pm]
        mean    = sum(margins) / len(margins)
        std     = math.sqrt(sum((x - mean) ** 2 for x in margins) / len(margins))
        cov     = std / (abs(mean) + 1)
        return round(max(0.0, min(1.0, 1 / (1 + cov))), 4)

    def alliance_luck(self) -> dict:
        """
        Alliance partner luck score.

        Measures how much better or worse than average the team's partners
        were, expressed as a z-score (standard deviations from field-average
        OPR). Positive = lucky draw; negative = unlucky draw.

        Requires an ``EventAnalyzer`` to be passed into the constructor.

        Returns:
            Dict with keys ``luck_score`` (float) and ``avg_partner_opr`` (float).
            If no ``EventAnalyzer`` is set, also includes a ``note`` key.
        """
        opr = self._all_matches_analyzer().opr()
        pm  = self._parsed_matches()
        if not pm:
            return {"luck_score": 0.0, "avg_partner_opr": 0.0}

        partner_oprs = []
        for p in pm:
            for t in _alliance_teams(p["our_alliance"]):
                if t != self.team:
                    partner_oprs.append(opr.get(t, 0))

        if not partner_oprs:
            return {"luck_score": 0.0, "avg_partner_opr": 0.0}

        all_oprs  = [v for v in opr.values() if v is not None]
        field_avg = sum(all_oprs) / len(all_oprs) if all_oprs else 0
        field_std = math.sqrt(sum((v - field_avg) ** 2 for v in all_oprs) / len(all_oprs)) if all_oprs else 1
        avg_part  = sum(partner_oprs) / len(partner_oprs)
        luck      = round((avg_part - field_avg) / (field_std + 1e-9), 4)
        return {"luck_score": luck, "avg_partner_opr": round(avg_part, 2)}

    def alliance_luck_score(self) -> float:
        """
        Convenience wrapper returning just the luck z-score from ``alliance_luck()``.
        """
        return self.alliance_luck().get("luck_score", 0.0)

    def _all_matches_analyzer(self) -> "EventAnalyzer":
        """Internal EventAnalyzer built from all of this team's matches across all events."""
        if not hasattr(self, "_ama_cache") or self._ama_cache is None:
            self._ama_cache = EventAnalyzer(self.qual_matches, qual_only=False)
        return self._ama_cache

    def opr(self) -> Optional[float]:
        """
        OPR computed from all of this team's matches across all events.

        Uses the full season match history from ``fetch_team_data()`` rather than
        a single event, so the rating reflects cumulative season performance.
        """
        return self._all_matches_analyzer().opr().get(self.team)

    def dpr(self) -> Optional[float]:
        """
        DPR computed from all of this team's matches across all events.

        Points allowed per match averaged across the full season.
        """
        return self._all_matches_analyzer().dpr().get(self.team)

    def true_opr(self) -> Optional[float]:
        """
        TrueOPR computed from all of this team's matches across all events.

        Computed as ``OPR - mean(OPR)`` across all teams in the full season
        match pool. Positive means above-average offensive output; negative
        means below. A direct measure of offensive performance vs. the field.
        """
        ama  = self._all_matches_analyzer()
        o    = ama.opr()
        mine = o.get(self.team)
        if mine is None:
            return None
        vals = list(o.values())
        avg  = sum(vals) / len(vals) if vals else 0
        return round(mine - avg, 4)

    def true_dpr(self) -> Optional[float]:
        """
        TrueDPR computed from all of this team's matches across all events.

        How much better the team limits opponents compared to the average DPR
        of all teams in their match history.
        """
        ama     = self._all_matches_analyzer()
        all_dpr = ama.dpr()
        d       = all_dpr.get(self.team)
        if d is None or not all_dpr:
            return None
        avg = sum(all_dpr.values()) / len(all_dpr)
        return round(avg - d, 4)

    def epr(self) -> Optional[float]:
        """
        EPR computed from all of this team's matches across all events.

        OPR expressed as a fraction of the average alliance score across all
        matches this team has played across the full season.
        """
        o = self.opr()
        if o is None:
            return None
        pm = self._parsed_matches()
        if not pm:
            return None
        all_scores = [p["our_score"] for p in pm] + [p["opp_score"] for p in pm]
        avg_score  = sum(all_scores) / len(all_scores) if all_scores else 1
        return round(o / avg_score, 4) if avg_score else None

    def hsf(self) -> float:
        """
        Human Skill Factor computed from all of this team's matches across all events.

        The highest single alliance score recorded across every qualification
        match this team has played across the full season.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm:
            return 0.0
        return float(max(p["our_score"] for p in pm))

    def sos(self) -> Optional[float]:
        """
        Strength of Schedule computed from all of this team's matches across all events.

        Average OPR of every opponent faced across the full season, using OPRs
        derived from the same full-season match pool.
        """
        opr = self._all_matches_analyzer().opr()
        pm  = self._parsed_matches()
        if not pm:
            return None
        opp_oprs = []
        for p in pm:
            for t in _alliance_teams(p["opp_alliance"]):
                opp_oprs.append(opr.get(t, 0))
        return round(sum(opp_oprs) / len(opp_oprs), 4) if opp_oprs else None

    def dsr(self) -> Optional[float]:
        """
        DSR computed from all of this team's matches across all events.

        Alias for ``sos()``. Returns the average OPR of all opponents faced
        across the full season.
        """
        return self.sos()

    def apr(self) -> Optional[float]:
        """
        APR computed from all of this team's matches across all events.

        Weighted blend: ``0.6 * OPR + 0.25 * TrueOPR + 0.15 * EPR``, all
        derived from the full season match history.
        """
        o  = self.opr()
        to = self.true_opr()
        ep = self.epr()
        if o is None or to is None or ep is None:
            return None
        return round(0.6 * o + 0.25 * to + 0.15 * ep, 4)

    def partner_strength_avg(self) -> Optional[float]:
        """
        Average OPR of this team's alliance partners across all matches and events.

        Partner OPRs are derived from the full season match pool.
        """
        opr          = self._all_matches_analyzer().opr()
        pm           = self._parsed_matches()
        partner_oprs = []
        for p in pm:
            for t in _alliance_teams(p["our_alliance"]):
                if t != self.team:
                    partner_oprs.append(opr.get(t, 0))
        return round(sum(partner_oprs) / len(partner_oprs), 4) if partner_oprs else None

    def elo(self, all_event_matches: list, k: float = 32,
            initial: float = 1500.0) -> float:
        """
        Season-wide ELO rating computed from the full match pool.

        All matches across all events are replayed chronologically in a single
        shared ELO pool. This means opponents who dominated earlier events will
        already have elevated ELOs when you face them — beating them is worth
        proportionally more than beating a team with no prior record.

        The correct input is the output of
        ``RobotEvents.get_matches_for_team_events(data)``, which fetches only
        the events your team attended (typically 5–15 requests rather than
        1,800+). You can also pass a broader pool such as all regional matches
        if you want opponent ELOs seeded from events outside your own schedule.

        Args:
            all_event_matches: Flat list of match dicts from one or more events,
                               sorted chronologically (the helper method returns
                               them pre-sorted).
            k:                 ELO K-factor. Default 32.
            initial:           Starting ELO for every team. Default 1500.

        Returns:
            This team's final ELO as a float.

        Example::

            data              = re.fetch_team_data("13155U")
            all_event_matches = re.get_matches_for_team_events(data)
            ta                = TeamAnalyzer("13155U", data)
            print(ta.elo(all_event_matches))
        """
        ordered = sorted(
            [m for m in all_event_matches if _is_qual(m)],
            key=lambda m: m.get("started_at") or m.get("scheduled") or "",
        )

        elo = defaultdict(lambda: initial)

        for m in ordered:
            alliances = _get_alliances(m)
            red       = _alliance_teams(alliances.get("red",  {}))
            blue      = _alliance_teams(alliances.get("blue", {}))
            if not red or not blue:
                continue

            r_score = _alliance_score(alliances.get("red",  {}))
            b_score = _alliance_score(alliances.get("blue", {}))
            r_avg   = sum(elo[t] for t in red)  / len(red)
            b_avg   = sum(elo[t] for t in blue) / len(blue)
            exp_r   = 1 / (1 + 10 ** ((b_avg - r_avg) / 400))
            exp_b   = 1 - exp_r

            if r_score > b_score:   s_r, s_b = 1.0, 0.0
            elif r_score < b_score: s_r, s_b = 0.0, 1.0
            else:                   s_r, s_b = 0.5, 0.5

            for t in red:  elo[t] += k * (s_r - exp_r)
            for t in blue: elo[t] += k * (s_b - exp_b)

        return round(elo[self.team], 2)

    def match_history_by_event(self) -> dict:
        """
        Group qualification matches by event ID.

        Returns:
            Dict mapping event ID string to list of match dicts.
        """
        by_event = defaultdict(list)
        for m in self.qual_matches:
            eid = str((m.get("event") or {}).get("id") or "unknown")
            by_event[eid].append(m)
        return dict(by_event)

    def event_performance_summary(self) -> list:
        """
        Per-event performance breakdown.

        Returns:
            List of dicts, one per event, each with keys ``event_id``,
            ``matches``, ``wins``, ``losses``, ``ties``, ``avg_score``,
            ``max_score``, ``min_score``.
        """
        by_event  = self.match_history_by_event()
        summaries = []
        for eid, matches in by_event.items():
            scores, results = [], []
            for m in matches:
                sides = _find_team_sides(self.team, m)
                if sides is None: continue
                our, opp = sides
                our_s    = _alliance_score(our)
                opp_s    = _alliance_score(opp)
                scores.append(our_s)
                results.append("W" if our_s > opp_s else ("L" if our_s < opp_s else "T"))
            if not scores: continue
            summaries.append({
                "event_id":  eid,
                "matches":   len(scores),
                "wins":      results.count("W"),
                "losses":    results.count("L"),
                "ties":      results.count("T"),
                "avg_score": round(sum(scores) / len(scores), 2),
                "max_score": max(scores),
                "min_score": min(scores),
            })
        return summaries

    def score_progression(self) -> list:
        """
        Chronological list of match scores showing the team's scoring trend.

        Returns:
            List of dicts with keys ``match_num``, ``match_name``,
            ``our_score``, ``opp_score``, ``event_id``.
        """
        ordered     = sorted(self.qual_matches,
                             key=lambda m: m.get("started_at") or m.get("scheduled") or "")
        progression = []
        for i, m in enumerate(ordered):
            sides = _find_team_sides(self.team, m)
            if sides is None: continue
            our, opp = sides
            progression.append({
                "match_num":  i + 1,
                "match_name": m.get("name") or m.get("id"),
                "our_score":  _alliance_score(our),
                "opp_score":  _alliance_score(opp),
                "event_id":   (m.get("event") or {}).get("id"),
            })
        return progression

    def consistency_rating(self) -> float:
        """
        Score consistency across qualification matches.

        Computed as ``1 - (std_dev / mean)`` of alliance scores.
        ``1.0`` means perfectly consistent; values near ``0`` are highly variable.

        Returns:
            Float between 0 and 1.
        """
        pm = self._parsed_matches()
        if len(pm) < 2: return 1.0
        scores = [p["our_score"] for p in pm]
        mean   = sum(scores) / len(scores)
        if mean == 0: return 1.0
        std    = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
        return round(max(0.0, 1 - std / mean), 4)

    def peak_performance_score(self) -> float:
        """
        Highest single alliance score recorded across all qualification matches.

        Returns:
            Float, or ``0.0`` if no matches.
        """
        pm = self._parsed_matches()
        if not pm: return 0.0
        return float(max(p["our_score"] for p in pm))

    def clutch_performance(self) -> float:
        """
        Win rate in close matches (score margin of 10 points or fewer).

        Returns:
            Float between 0 and 1. Returns ``0.5`` if no close matches were played.
        """
        pm    = self._parsed_matches()
        close = [p for p in pm if abs(p["margin"]) <= 10]
        if not close: return 0.5
        wins  = sum(1 for p in close if p["result"] == "W")
        return round(wins / len(close), 4)

    def opponent_strength_avg(self) -> Optional[float]:
        """
        Average OPR of opponents faced. Alias for ``sos()``.
        Requires an ``EventAnalyzer`` to be attached.
        """
        return self.sos()

    def summary(self) -> dict:
        """
        All computable metrics in a single dict.

        Advanced ratings (``opr``, ``dpr``, ``apr``, etc.) are ``None`` unless an
        ``EventAnalyzer`` was passed into the constructor. ELO is not included
        here — call ``ta.elo(all_event_matches)`` separately.

        Returns:
            Dict with keys: ``team``, ``avg_match_score``, ``avg_match_performance``,
            ``wins``, ``losses``, ``ties``, ``win_pct``, ``awp_rate``, ``carry_power``,
            ``defensive_power``, ``robot_scoring_cap``, ``alliance_independence``,
            ``alliance_luck``, ``consistency``, ``peak_score``, ``clutch_performance``,
            ``opr``, ``dpr``, ``true_opr``, ``true_dpr``, ``dsr``, ``epr``, ``hsf``,
            ``apr``, ``sos``.
        """
        wlt   = self.wlt_record()
        auton = self.autonomous_performance()
        luck  = self.alliance_luck()

        return {
            "team":                  self.team,
            "avg_match_score":       self.average_match_score(),
            "avg_match_performance": self.average_match_performance(),
            "wins":                  wlt["wins"],
            "losses":                wlt["losses"],
            "ties":                  wlt["ties"],
            "win_pct":               wlt["win_pct"],
            "awp_rate":              auton["awp_rate"],
            "carry_power":           self.carry_power(),
            "defensive_power":       self.defensive_power(),
            "robot_scoring_cap":     self.robot_scoring_capability(),
            "alliance_independence": self.alliance_independence(),
            "alliance_luck":         luck.get("luck_score", 0.0),
            "consistency":           self.consistency_rating(),
            "peak_score":            self.peak_performance_score(),
            "clutch_performance":    self.clutch_performance(),
            "opr":                   self.opr(),
            "dpr":                   self.dpr(),
            "true_opr":              self.true_opr(),
            "true_dpr":              self.true_dpr(),
            "dsr":                   self.dsr(),
            "epr":                   self.epr(),
            "hsf":                   self.hsf(),
            "apr":                   self.apr(),
            "sos":                   self.sos(),
        }


def export_to_csv(data: list, filepath: str):
    """
    Export a list of dicts to a CSV file.

    Args:
        data:     List of dicts with uniform keys, e.g. from ``ea.full_rankings()``.
        filepath: Destination file path. Parent directories are created if needed.
    """
    if not data:
        log.warning("No data to export.")
        return
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    log.info("Exported %d rows to %s", len(data), filepath)


def export_to_json(data, filepath: str):
    """
    Export any JSON-serializable object to a file.

    Args:
        data:     Any JSON-serializable Python object.
        filepath: Destination file path. Parent directories are created if needed.
    """
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Exported to %s", filepath)


def normalize_scores(data: list, key: str) -> list:
    """
    Add a min-max normalized field to each row in a list of dicts.

    Adds a new key ``"{key}_normalized"`` with values scaled to 0–1.

    Args:
        data: List of dicts, each containing the field named ``key``.
        key:  The field to normalize.

    Returns:
        The same list with ``"{key}_normalized"`` added to each row.
    """
    vals = [r[key] for r in data if r.get(key) is not None]
    if not vals: return data
    lo, hi = min(vals), max(vals)
    span   = hi - lo or 1
    for r in data:
        r[f"{key}_normalized"] = round((r.get(key, lo) - lo) / span, 4)
    return data


def batch_analyze(team_data_map: dict, event_matches: list = None,
                  all_event_matches: list = None) -> list:
    """
    Run ``TeamAnalyzer.summary()`` and optionally ELO for multiple teams at once.

    Args:
        team_data_map:     Dict mapping team number string to ``fetch_team_data()`` result.
        event_matches:     Match list for a single event. When provided, an
                           ``EventAnalyzer`` is created and attached to each
                           ``TeamAnalyzer``, enabling OPR, DPR, APR, etc.
        all_event_matches: Flat match list from all attended events. When
                           provided, ELO is computed per team and added to
                           each summary dict as ``"elo"``.

    Returns:
        List of summary dicts sorted by APR descending (falls back to
        ``avg_match_score`` if APR is unavailable).
    """
    ea      = EventAnalyzer(event_matches) if event_matches else None
    results = []
    for team_key, data in team_data_map.items():
        ta      = TeamAnalyzer(team_key, data, event_analyzer=ea)
        summary = ta.summary()
        if all_event_matches:
            summary["elo"] = ta.elo(all_event_matches)
        results.append(summary)
    results.sort(key=lambda r: r.get("apr") or r.get("avg_match_score") or 0, reverse=True)
    return results