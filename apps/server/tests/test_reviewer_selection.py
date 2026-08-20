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


def test_per_task_seed_produces_independent_rings():
    # Change A: the same round with different task seeds must shuffle independently,
    # so a harsh reviewer is not dealt both artifacts of the same participant by
    # construction. Deterministic given fixed seed strings.
    assignments_a = {p["user_id"]: reviewers_for("round-x:task-a", p["user_id"]) for p in PARTICIPANTS}
    assignments_b = {p["user_id"]: reviewers_for("round-x:task-b", p["user_id"]) for p in PARTICIPANTS}
    assert assignments_a != assignments_b
    # both rings keep every balance property
    for assignments in (assignments_a, assignments_b):
        load = Counter(r for reviewers in assignments.values() for r in reviewers)
        assert set(load.values()) == {2}


def test_task_assignment_avoids_forbidden_pairs():
    from vis_arena_server.rounds import select_task_assignment

    # forbid exactly the pairs the first-attempt shuffle would produce, forcing
    # the deterministic retry to find a later shuffle with zero repeats
    first_attempt = {
        p["user_id"]: set(reviewers_for("round-x:task-a", p["user_id"])) for p in PARTICIPANTS
    }
    forbidden = {(reviewer, owner) for owner, reviewers in first_attempt.items() for reviewer in reviewers}

    assignment = select_task_assignment("round-x", "task-a", PARTICIPANTS, 2, forbidden)

    pairs = {(p["user_id"], owner) for owner, reviewers in assignment.items() for p in reviewers}
    assert not (pairs & forbidden), "assignment repeated a forbidden reviewer->target pair"
    # balance survives: every owner gets cap reviewers, workload equal, no self-review
    load = Counter(p["user_id"] for reviewers in assignment.values() for p in reviewers)
    assert set(load.values()) == {2}
    for owner, reviewers in assignment.items():
        assert len(reviewers) == 2
        assert owner not in {p["user_id"] for p in reviewers}


def test_task_assignment_falls_back_to_minimal_overlap():
    from vis_arena_server.rounds import select_task_assignment

    # 2 participants, cap 1: the only possible assignment is mutual review, so a
    # forbidden mutual pair cannot be avoided — must still return an assignment.
    two = PARTICIPANTS[:2]
    forbidden = {("user-0", "user-1"), ("user-1", "user-0")}
    assignment = select_task_assignment("round-y", "task-a", two, 1, forbidden)
    assert {owner for owner in assignment} == {"user-0", "user-1"}
    assert all(len(reviewers) == 1 for reviewers in assignment.values())
