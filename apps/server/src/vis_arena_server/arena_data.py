"""Arena infrastructure data layer.

Serves real evaluation data when available (populated by arena_runner.py),
falling back to deterministic mock data during development.
"""
from __future__ import annotations

import json
import math
import random
from typing import Any

from .db import connect
from .settings import settings

# ---------------------------------------------------------------------------
# Participant roster (used for mock fallback; real data comes from DB)
# ---------------------------------------------------------------------------

MOCK_PARTICIPANTS = [
    {"id": "GPT-5.4", "name": "GPT 5.4", "color": "#4A9EEA"},
    {"id": "GPT-5", "name": "GPT 5", "color": "#E8A838"},
    {"id": "4o-mini", "name": "GPT 4o-mini", "color": "#E06040"},
    {"id": "Sonnet", "name": "Claude 3.5 Sonnet", "color": "#D46090"},
    {"id": "Gemini", "name": "Gemini 1.5 Pro", "color": "#30C878"},
    {"id": "Llama3", "name": "Llama 3 70B", "color": "#6AACDA"},
    {"id": "Mixtral", "name": "Mixtral 8x22B", "color": "#C8A828"},
]

# Colors for dynamically discovered real models
_MODEL_COLORS = ["#4A9EEA", "#E8A838", "#E06040", "#D46090", "#30C878", "#6AACDA", "#C8A828", "#8B66CC"]

NUM_INTERVALS = 12

# ---------------------------------------------------------------------------
# Real data from DB
# ---------------------------------------------------------------------------


def _get_real_participants() -> list[dict[str, str]] | None:
    """Return participant list if real arena data exists, else None."""
    # Import here to avoid circular imports at module load time
    from .arena_runner import ARENA_PARTICIPANTS
    with connect() as db:
        count = db.execute(
            "SELECT COUNT(*) as n FROM jobs WHERE arena_round IS NOT NULL AND status = 'succeeded'"
        ).fetchone()["n"]
        if count == 0:
            return None

        # Get distinct model IDs that have arena results
        rows = db.execute(
            """SELECT DISTINCT s.name as model_id
               FROM jobs j JOIN submissions s ON s.id = j.submission_id
               WHERE j.arena_round IS NOT NULL AND j.status = 'succeeded'
               ORDER BY s.name"""
        ).fetchall()
        model_ids = [r["model_id"] for r in rows]
        if not model_ids:
            return None

        # Build participant dicts, reusing configured colors where possible
        color_map = {p["id"]: p["color"] for p in ARENA_PARTICIPANTS}
        participants = []
        for i, mid in enumerate(model_ids):
            participants.append({
                "id": mid,
                "name": color_map.get(mid, mid),  # use model ID as name if not in known list
                "color": color_map.get(mid, _MODEL_COLORS[i % len(_MODEL_COLORS)]),
            })
        # Fix name: use the pretty name from ARENA_PARTICIPANTS
        name_map = {p["id"]: p["name"] for p in ARENA_PARTICIPANTS}
        for p in participants:
            p["name"] = name_map.get(p["id"], p["id"])
        return participants


def _build_real_data() -> dict[str, Any] | None:
    """Build full arena data from DB. Returns None if insufficient data."""
    participants = _get_real_participants()
    if not participants:
        return None

    pids = [p["id"] for p in participants]

    with connect() as db:
        rows = db.execute(
            """SELECT s.name as model_id, j.arena_round, j.result_json, j.arena_evaluators
               FROM jobs j JOIN submissions s ON s.id = j.submission_id
               WHERE j.arena_round IS NOT NULL AND j.status = 'succeeded'
               ORDER BY j.arena_round, s.name"""
        ).fetchall()

    if not rows:
        return None

    max_round = max(r["arena_round"] for r in rows)
    num_intervals = max_round

    # scores[model_id][round] = list of peer scores
    scores: dict[str, dict[int, list[float]]] = {pid: {} for pid in pids}
    # evaluator_scores[evaluator_id][round] = list of scores that evaluator gave
    evaluator_scores: dict[str, dict[int, list[float]]] = {}

    for row in rows:
        mid = row["model_id"]
        rnd = row["arena_round"]
        if mid not in scores:
            continue
        result = json.loads(row["result_json"] or "{}")
        score = float(result.get("score", 50))
        scores[mid].setdefault(rnd, []).append(score)

        # Track per-evaluator scores for leniency/consensus
        peer = json.loads(row["arena_evaluators"] or "{}")
        for eval_id, eval_score in peer.items():
            if eval_id not in evaluator_scores:
                evaluator_scores[eval_id] = {}
            evaluator_scores[eval_id].setdefault(rnd, []).append(float(eval_score))

    # Build trajectories: mean score per model per round
    trajectories: dict[str, list[float]] = {}
    submitted: dict[str, list[bool]] = {}
    for pid in pids:
        traj = []
        sub = []
        for rnd in range(1, num_intervals + 1):
            round_scores = scores[pid].get(rnd, [])
            if round_scores:
                traj.append(round(sum(round_scores) / len(round_scores), 2))
                sub.append(True)
            else:
                traj.append(traj[-1] if traj else 50.0)
                sub.append(False)
        trajectories[pid] = traj
        submitted[pid] = sub

    # Frontiers: running best score
    frontiers: dict[str, list[float]] = {}
    best_scores: dict[str, float] = {}
    for pid in pids:
        best = 0.0
        f = []
        for i, s in enumerate(trajectories[pid]):
            if submitted[pid][i]:
                best = max(best, s)
            f.append(round(best, 2))
        frontiers[pid] = f
        best_scores[pid] = max(trajectories[pid]) if trajectories[pid] else 0.0

    # Ranks per interval
    ranks: dict[str, list[int]] = {pid: [] for pid in pids}
    for t in range(num_intervals):
        scored = [(pid, frontiers[pid][t]) for pid in pids]
        scored.sort(key=lambda x: -x[1])
        for rank_idx, (pid, _) in enumerate(scored, 1):
            ranks[pid].append(rank_idx)

    # Pairwise win matrix (fraction of rounds where A's score > B's score)
    wins: dict[str, dict[str, float | None]] = {}
    for a in pids:
        wins[a] = {}
        for b in pids:
            if a == b:
                wins[a][b] = None
            else:
                a_wins = 0
                comparisons = 0
                for rnd in range(1, num_intervals + 1):
                    sa = scores[a].get(rnd, [])
                    sb = scores[b].get(rnd, [])
                    if sa and sb:
                        ma = sum(sa) / len(sa)
                        mb = sum(sb) / len(sb)
                        if ma > mb:
                            a_wins += 1
                        comparisons += 1
                wins[a][b] = round(a_wins / comparisons, 2) if comparisons > 0 else 0.5

    # Evaluator alignment: how consistent each evaluator is vs. crowd mean
    # alignment[evaluator] = mean(|eval_score - crowd_mean|) normalized to [0,1]
    alignment: dict[str, float] = {}
    for pid in pids:
        per_round_deviations = []
        for rnd in range(1, num_intervals + 1):
            round_scores_all = [
                sum(scores[other_pid].get(rnd, [50])) / max(1, len(scores[other_pid].get(rnd, [50])))
                for other_pid in pids if other_pid != pid
            ]
            if not round_scores_all:
                continue
            crowd_mean = sum(round_scores_all) / len(round_scores_all)
            model_score = sum(scores[pid].get(rnd, [crowd_mean])) / max(1, len(scores[pid].get(rnd, [crowd_mean])))
            per_round_deviations.append(abs(model_score - crowd_mean))
        # alignment = 1 - normalized_deviation (higher = more aligned)
        if per_round_deviations:
            avg_dev = sum(per_round_deviations) / len(per_round_deviations)
            alignment[pid] = round(max(0.3, 1.0 - avg_dev / 50.0), 3)
        else:
            alignment[pid] = 0.7

    # Leniency: difference between score given vs. crowd mean, per evaluator per round
    # We treat each model as both a participant and an evaluator
    # Leniency based on the evaluator's scores relative to consensus
    leniency: dict[str, list[float]] = {pid: [] for pid in pids}
    for pid in pids:
        for rnd in range(1, num_intervals + 1):
            eval_scores_given = evaluator_scores.get(pid, {}).get(rnd, [])
            if not eval_scores_given:
                leniency[pid].append(0.0)
                continue
            eval_mean = sum(eval_scores_given) / len(eval_scores_given)
            # Crowd mean: average of all scores given by all evaluators this round
            all_this_round = []
            for e in evaluator_scores.values():
                all_this_round.extend(e.get(rnd, []))
            crowd_mean = sum(all_this_round) / len(all_this_round) if all_this_round else 50.0
            leniency[pid].append(round(eval_mean - crowd_mean, 2))

    # Consensus: Spearman rank correlation with crowd ordering per round
    consensus: dict[str, list[float]] = {pid: [] for pid in pids}
    for pid in pids:
        for rnd in range(1, num_intervals + 1):
            # crowd ranking of models this round
            crowd_model_scores = []
            for other_pid in pids:
                rs = scores[other_pid].get(rnd, [])
                crowd_model_scores.append((other_pid, sum(rs) / len(rs) if rs else 0))
            crowd_order = {m: rank for rank, (m, _) in enumerate(
                sorted(crowd_model_scores, key=lambda x: -x[1])
            )}
            # this evaluator's ranking (based on scores given in peer_scores)
            eval_scores_per_model = {}
            for r in rows:
                if r["arena_round"] == rnd:
                    peer_raw = json.loads(r["arena_evaluators"] or "{}")
                    if pid in peer_raw:
                        eval_scores_per_model[r["model_id"]] = float(peer_raw[pid])

            if len(eval_scores_per_model) < 2:
                consensus[pid].append(0.7)
                continue

            eval_order = {m: rank for rank, (m, _) in enumerate(
                sorted(eval_scores_per_model.items(), key=lambda x: -x[1])
            )}
            # Spearman ρ on the intersection
            common = [m for m in crowd_order if m in eval_order]
            if len(common) < 2:
                consensus[pid].append(0.7)
                continue
            n = len(common)
            d2 = sum((crowd_order[m] - eval_order[m]) ** 2 for m in common)
            rho = 1 - (6 * d2) / (n * (n * n - 1))
            consensus[pid].append(round(min(1.0, max(0.0, rho)), 3))

    # Elo ratings
    elo: dict[str, list[float]] = {pid: [] for pid in pids}
    elo_current = {pid: 1200.0 for pid in pids}
    K = 32
    for rnd in range(1, num_intervals + 1):
        # Compute pairwise results for this round
        for a in pids:
            for b in pids:
                if a >= b:
                    continue
                sa_list = scores[a].get(rnd, [])
                sb_list = scores[b].get(rnd, [])
                if not sa_list or not sb_list:
                    continue
                sa = sum(sa_list) / len(sa_list)
                sb = sum(sb_list) / len(sb_list)
                ea = 1 / (1 + 10 ** ((elo_current[b] - elo_current[a]) / 400))
                eb = 1 - ea
                actual_a = 1.0 if sa > sb else (0.5 if sa == sb else 0.0)
                actual_b = 1.0 - actual_a
                elo_current[a] += K * (actual_a - ea)
                elo_current[b] += K * (actual_b - eb)
        for pid in pids:
            elo[pid].append(round(elo_current[pid], 1))

    return {
        "participants": participants,
        "num_intervals": num_intervals,
        "trajectories": trajectories,
        "submitted": submitted,
        "frontiers": frontiers,
        "best_scores": best_scores,
        "alignment": alignment,
        "wins": wins,
        "ranks": ranks,
        "leniency": leniency,
        "consensus": consensus,
        "elo": elo,
        "is_mock": False,
    }


# ---------------------------------------------------------------------------
# Mock data fallback (deterministic seed)
# ---------------------------------------------------------------------------

_RNG = random.Random(42)


def _gen_trajectory(base: float, drift: float, n: int) -> list[float]:
    vals = []
    v = base
    for _ in range(n):
        v += _RNG.gauss(drift, 1.8)
        vals.append(round(max(50, min(100, v)), 2))
    return vals


def _gen_submitted(n: int) -> list[bool]:
    return [_RNG.random() > 0.25 for _ in range(n)]


def _build_mock_data() -> dict[str, Any]:
    trajectories: dict[str, list[float]] = {}
    submitted: dict[str, list[bool]] = {}
    for p in MOCK_PARTICIPANTS:
        base = _RNG.uniform(65, 78)
        drift = _RNG.uniform(0.3, 1.2)
        trajectories[p["id"]] = _gen_trajectory(base, drift, NUM_INTERVALS)
        sub = _gen_submitted(NUM_INTERVALS)
        sub[0] = True
        submitted[p["id"]] = sub

    frontiers: dict[str, list[float]] = {}
    for pid, scores in trajectories.items():
        best = []
        running = 0.0
        for i, s in enumerate(scores):
            if submitted[pid][i]:
                running = max(running, s)
            best.append(round(running, 2))
        frontiers[pid] = best

    best_scores: dict[str, float] = {
        pid: max(scores) for pid, scores in trajectories.items()
    }
    alignment: dict[str, float] = {
        p["id"]: round(_RNG.uniform(0.4, 0.92), 3) for p in MOCK_PARTICIPANTS
    }
    wins: dict[str, dict[str, float | None]] = {}
    for p in MOCK_PARTICIPANTS:
        wins[p["id"]] = {}
        for q in MOCK_PARTICIPANTS:
            if p["id"] == q["id"]:
                wins[p["id"]][q["id"]] = None
            else:
                wins[p["id"]][q["id"]] = round(_RNG.uniform(0.2, 0.85), 2)

    ranks: dict[str, list[int]] = {}
    for t in range(NUM_INTERVALS):
        scored = [(pid, frontiers[pid][t]) for pid in frontiers]
        scored.sort(key=lambda x: -x[1])
        for rank_idx, (pid, _) in enumerate(scored, 1):
            ranks.setdefault(pid, []).append(rank_idx)

    leniency: dict[str, list[float]] = {
        p["id"]: [round(_RNG.gauss(0, 3.5), 2) for _ in range(NUM_INTERVALS)]
        for p in MOCK_PARTICIPANTS
    }
    consensus: dict[str, list[float]] = {
        p["id"]: [round(min(1, max(0, _RNG.gauss(0.65, 0.15))), 3) for _ in range(NUM_INTERVALS)]
        for p in MOCK_PARTICIPANTS
    }
    elo: dict[str, list[float]] = {}
    for p in MOCK_PARTICIPANTS:
        base_elo = 1200.0
        ratings = []
        for _ in range(NUM_INTERVALS):
            base_elo += _RNG.gauss(5, 20)
            ratings.append(round(base_elo, 1))
        elo[p["id"]] = ratings

    return {
        "participants": MOCK_PARTICIPANTS,
        "num_intervals": NUM_INTERVALS,
        "trajectories": trajectories,
        "submitted": submitted,
        "frontiers": frontiers,
        "best_scores": best_scores,
        "alignment": alignment,
        "wins": wins,
        "ranks": ranks,
        "leniency": leniency,
        "consensus": consensus,
        "elo": elo,
        "is_mock": True,
    }


_MOCK: dict[str, Any] = _build_mock_data()


def _get_data() -> dict[str, Any]:
    """Return real data from DB if available, otherwise mock data."""
    try:
        real = _build_real_data()
        if real is not None:
            return real
    except Exception:
        pass
    return _MOCK


# ---------------------------------------------------------------------------
# Public API (called from FastAPI routes)
# ---------------------------------------------------------------------------


def get_arena_overview() -> dict[str, Any]:
    data = _get_data()
    participants = data["participants"]
    submitted = data["submitted"]
    return {
        "total_participants": len(participants),
        "total_intervals": data["num_intervals"],
        "total_submissions": sum(
            sum(1 for s in submitted[p["id"]] if s) for p in participants
        ),
        "datasets": ["Medicine", "Business", "Climate"],
        "is_mock": data["is_mock"],
    }


def get_arena_leaderboard() -> dict[str, Any]:
    data = _get_data()
    ranked = sorted(data["participants"], key=lambda p: -data["best_scores"][p["id"]])
    items = []
    for rank, p in enumerate(ranked, 1):
        items.append({
            "rank": rank,
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "score": data["best_scores"][p["id"]],
        })
    return {"items": items, "is_mock": data["is_mock"]}


def get_frontier_data() -> dict[str, Any]:
    data = _get_data()
    return {
        "participants": data["participants"],
        "num_intervals": data["num_intervals"],
        "trajectories": data["trajectories"],
        "submitted": data["submitted"],
        "frontiers": data["frontiers"],
        "is_mock": data["is_mock"],
    }


def get_scatter_data() -> dict[str, Any]:
    data = _get_data()
    points = []
    for p in data["participants"]:
        points.append({
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "quality": data["best_scores"][p["id"]],
            "alignment": data["alignment"][p["id"]],
        })
    return {"points": points, "is_mock": data["is_mock"]}


def get_wins_data() -> dict[str, Any]:
    data = _get_data()
    ordered = sorted(data["participants"], key=lambda p: -sum(
        v for v in data["wins"][p["id"]].values() if v is not None
    ))
    return {
        "participants": [{"id": p["id"], "name": p["name"], "color": p["color"]} for p in ordered],
        "matrix": {p["id"]: data["wins"][p["id"]] for p in ordered},
        "is_mock": data["is_mock"],
    }


def get_analytics_data() -> dict[str, Any]:
    data = _get_data()
    return {
        "participants": data["participants"],
        "num_intervals": data["num_intervals"],
        "ranks": data["ranks"],
        "leniency": data["leniency"],
        "consensus": data["consensus"],
        "elo": data["elo"],
        "is_mock": data["is_mock"],
    }
