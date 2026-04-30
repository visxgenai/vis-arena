"""Arena infrastructure data layer.

Serves mock/seed data for the seven Vis Arena Infra charts and the agent
leaderboard.  In production, this data would come from the evaluation
pipeline writing results into S3 and the database.  For now it returns
deterministic mock data so the frontend can be developed immediately.
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

from .settings import settings

# ---------------------------------------------------------------------------
# Participant roster
# ---------------------------------------------------------------------------

PARTICIPANTS = [
    {"id": "GPT-5.4", "name": "GPT 5.4", "color": "#4A9EEA"},
    {"id": "GPT-5", "name": "GPT 5", "color": "#E8A838"},
    {"id": "4o-mini", "name": "GPT 4o-mini", "color": "#E06040"},
    {"id": "Sonnet", "name": "Claude 3.5 Sonnet", "color": "#D46090"},
    {"id": "Gemini", "name": "Gemini 1.5 Pro", "color": "#30C878"},
    {"id": "Llama3", "name": "Llama 3 70B", "color": "#6AACDA"},
    {"id": "Mixtral", "name": "Mixtral 8x22B", "color": "#C8A828"},
]

NUM_INTERVALS = 12

# ---------------------------------------------------------------------------
# Deterministic seed
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


# ---------------------------------------------------------------------------
# Pre-computed mock data
# ---------------------------------------------------------------------------

def _build_mock_data() -> dict[str, Any]:
    trajectories: dict[str, list[float]] = {}
    submitted: dict[str, list[bool]] = {}
    for p in PARTICIPANTS:
        base = _RNG.uniform(65, 78)
        drift = _RNG.uniform(0.3, 1.2)
        trajectories[p["id"]] = _gen_trajectory(base, drift, NUM_INTERVALS)
        sub = _gen_submitted(NUM_INTERVALS)
        sub[0] = True  # always submit first interval
        submitted[p["id"]] = sub

    # Frontier: running max per participant
    frontiers: dict[str, list[float]] = {}
    for pid, scores in trajectories.items():
        best = []
        running = 0.0
        for i, s in enumerate(scores):
            if submitted[pid][i]:
                running = max(running, s)
            best.append(round(running, 2))
        frontiers[pid] = best

    # Best score per participant
    best_scores: dict[str, float] = {
        pid: max(scores) for pid, scores in trajectories.items()
    }

    # Evaluator alignment
    alignment: dict[str, float] = {
        p["id"]: round(_RNG.uniform(0.4, 0.92), 3) for p in PARTICIPANTS
    }

    # Pairwise win matrix
    wins: dict[str, dict[str, float]] = {}
    for p in PARTICIPANTS:
        wins[p["id"]] = {}
        for q in PARTICIPANTS:
            if p["id"] == q["id"]:
                wins[p["id"]][q["id"]] = None  # type: ignore[assignment]
            else:
                wins[p["id"]][q["id"]] = round(_RNG.uniform(0.2, 0.85), 2)

    # Rank over time (lower = better rank)
    ranks: dict[str, list[int]] = {}
    for t in range(NUM_INTERVALS):
        scored = [(pid, frontiers[pid][t]) for pid in frontiers]
        scored.sort(key=lambda x: -x[1])
        for rank_idx, (pid, _) in enumerate(scored, 1):
            ranks.setdefault(pid, []).append(rank_idx)

    # Leniency drift
    leniency: dict[str, list[float]] = {
        p["id"]: [round(_RNG.gauss(0, 3.5), 2) for _ in range(NUM_INTERVALS)]
        for p in PARTICIPANTS
    }

    # Consensus agreement (Spearman ρ)
    consensus: dict[str, list[float]] = {
        p["id"]: [round(min(1, max(0, _RNG.gauss(0.65, 0.15))), 3) for _ in range(NUM_INTERVALS)]
        for p in PARTICIPANTS
    }

    # Elo ratings
    elo: dict[str, list[float]] = {}
    for p in PARTICIPANTS:
        base_elo = 1200.0
        ratings = []
        for _ in range(NUM_INTERVALS):
            base_elo += _RNG.gauss(5, 20)
            ratings.append(round(base_elo, 1))
        elo[p["id"]] = ratings

    return {
        "participants": PARTICIPANTS,
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
    }


_MOCK = _build_mock_data()


# ---------------------------------------------------------------------------
# Public API (called from FastAPI routes)
# ---------------------------------------------------------------------------


def get_arena_overview() -> dict[str, Any]:
    """Return summary statistics for the arena header."""
    return {
        "total_participants": len(PARTICIPANTS),
        "total_intervals": NUM_INTERVALS,
        "total_submissions": sum(
            sum(1 for s in _MOCK["submitted"][p["id"]] if s) for p in PARTICIPANTS
        ),
        "datasets": ["Medicine", "Business", "Climate"],
    }


def get_arena_leaderboard() -> dict[str, Any]:
    """Ranked list by best score across all intervals."""
    ranked = sorted(PARTICIPANTS, key=lambda p: -_MOCK["best_scores"][p["id"]])
    items = []
    for rank, p in enumerate(ranked, 1):
        items.append({
            "rank": rank,
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "score": _MOCK["best_scores"][p["id"]],
        })
    return {"items": items}


def get_frontier_data() -> dict[str, Any]:
    """Peer-graded frontier chart data."""
    return {
        "participants": PARTICIPANTS,
        "num_intervals": NUM_INTERVALS,
        "trajectories": _MOCK["trajectories"],
        "submitted": _MOCK["submitted"],
        "frontiers": _MOCK["frontiers"],
    }


def get_scatter_data() -> dict[str, Any]:
    """Alignment × Quality scatter plot data."""
    points = []
    for p in PARTICIPANTS:
        points.append({
            "id": p["id"],
            "name": p["name"],
            "color": p["color"],
            "quality": _MOCK["best_scores"][p["id"]],
            "alignment": _MOCK["alignment"][p["id"]],
        })
    return {"points": points}


def get_wins_data() -> dict[str, Any]:
    """Pairwise win matrix data."""
    ordered = sorted(PARTICIPANTS, key=lambda p: -sum(
        v for v in _MOCK["wins"][p["id"]].values() if v is not None
    ))
    return {
        "participants": [{"id": p["id"], "name": p["name"], "color": p["color"]} for p in ordered],
        "matrix": {p["id"]: _MOCK["wins"][p["id"]] for p in ordered},
    }


def get_analytics_data() -> dict[str, Any]:
    """All four analytics charts."""
    return {
        "participants": PARTICIPANTS,
        "num_intervals": NUM_INTERVALS,
        "ranks": _MOCK["ranks"],
        "leniency": _MOCK["leniency"],
        "consensus": _MOCK["consensus"],
        "elo": _MOCK["elo"],
    }
