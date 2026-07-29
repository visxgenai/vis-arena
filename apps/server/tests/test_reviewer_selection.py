"""select_peer_reviewers: balanced, seeded-random reviewer subsampling."""
from __future__ import annotations

from collections import Counter

from vis_arena_server.rounds import select_peer_reviewers

PARTICIPANTS = [{"user_id": f"user-{i}", "submission_id": f"sub-{i}", "user_name": f"p{i}"} for i in range(6)]


def reviewers_for(round_id: str, owner: str, cap: int = 2) -> list[str]:
    return [p["user_id"] for p in select_peer_reviewers(round_id, PARTICIPANTS, owner, cap)]


def test_exact_cap_and_no_self_review():
    for p in PARTICIPANTS:
        got = reviewers_for("round-a", p["user_id"])
        assert len(got) == 2
        assert p["user_id"] not in got
        assert len(set(got)) == 2


def test_workload_is_balanced():
    # With a ring assignment every participant reviews exactly `cap` distinct owners.
    load = Counter()
    for p in PARTICIPANTS:
        for reviewer in reviewers_for("round-a", p["user_id"]):
            load[reviewer] += 1
    assert set(load.values()) == {2}
    assert len(load) == len(PARTICIPANTS)


def test_deterministic_within_a_round():
    for p in PARTICIPANTS:
        assert reviewers_for("round-a", p["user_id"]) == reviewers_for("round-a", p["user_id"])


def test_row_order_does_not_matter():
    shuffled_input = list(reversed(PARTICIPANTS))
    for p in PARTICIPANTS:
        assert reviewers_for("round-a", p["user_id"]) == [
            x["user_id"] for x in select_peer_reviewers("round-a", shuffled_input, p["user_id"], 2)
        ]


def test_rotates_across_rounds():
    # Different round ids should not all reproduce the same pairing for everyone.
    assignments = [
        tuple(tuple(reviewers_for(rid, p["user_id"])) for p in PARTICIPANTS)
        for rid in ("round-a", "round-b", "round-c")
    ]
    assert len(set(assignments)) > 1


def test_cap_zero_means_everyone():
    got = reviewers_for("round-a", "user-0", cap=0)
    assert sorted(got) == [f"user-{i}" for i in range(1, 6)]


def test_cap_larger_than_field_clamps():
    got = reviewers_for("round-a", "user-0", cap=99)
    assert len(got) == 5


def test_single_participant_no_peers():
    solo = [PARTICIPANTS[0]]
    assert select_peer_reviewers("round-a", solo, "user-0", 2) == []


def test_unknown_owner_gets_nothing():
    assert select_peer_reviewers("round-a", PARTICIPANTS, "stranger", 2) == []
