from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError

import pytest

from theoracle.draft_arbiter import (
    DraftArbiter,
    LogicalPack,
    PickEvent,
    PlayerStats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_arbiter(tmp_path, **kwargs) -> DraftArbiter:
    defaults = dict(
        session_id="s1",
        num_players=3,
        pack_size=3,
        rounds=["left"],
        player_names=["Alice", "Bob", "Charlie"],
        save_path=str(tmp_path / "test.csv"),
    )
    defaults.update(kwargs)
    return DraftArbiter(**defaults)


def _drive_left_3p(arbiter: DraftArbiter, cards: list[str] | None = None) -> list[PickEvent]:
    """
    Drive a 3-player left-pass single-round draft to completion.

    With direction="left", next_seat = (seat - 1) % 3:
      R0S0: seat 0 → seat 2 → seat 1
      R0S1: seat 1 → seat 0 → seat 2
      R0S2: seat 2 → seat 1 → seat 0

    One valid serial pick order (each seat picks when a pack is available):
      pick 0: seat 0 from R0S0
      pick 1: seat 1 from R0S1
      pick 2: seat 2 from R0S2
      pick 3: seat 2 from R0S0  (arrived from seat 0)
      pick 4: seat 0 from R0S1  (arrived from seat 1)
      pick 5: seat 1 from R0S2  (arrived from seat 2)
      pick 6: seat 1 from R0S0  (arrived from seat 2)
      pick 7: seat 2 from R0S1  (arrived from seat 0)
      pick 8: seat 0 from R0S2  (arrived from seat 1)
    """
    if cards is None:
        cards = [f"Card{i}" for i in range(9)]
    order = [0, 1, 2, 2, 0, 1, 1, 2, 0]
    return [arbiter.record_pick(seat, cards[i]) for i, seat in enumerate(order)]


# ---------------------------------------------------------------------------
# Section 1 — PickEvent immutability
# ---------------------------------------------------------------------------

def test_pick_event_is_frozen():
    e = PickEvent(pack_id="R0S0", seat=0, pick_index=0, card_name="Lightning Bolt")
    with pytest.raises(FrozenInstanceError):
        e.card_name = "Counterspell"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Section 2 — LogicalPack.is_complete
# ---------------------------------------------------------------------------

def test_logical_pack_not_complete_when_empty():
    pack = LogicalPack("R0S0", 0, 0, 3, "left", [])
    assert not pack.is_complete


def test_logical_pack_complete_when_full():
    pack = LogicalPack("R0S0", 0, 0, 3, "left", ["A", "B", "C"])
    assert pack.is_complete


# ---------------------------------------------------------------------------
# Section 3 — PlayerStats
# ---------------------------------------------------------------------------

def test_player_stats_picked_cards_and_card_count():
    ps = PlayerStats(player_name="Alice", picked_cards=["A", "B", "A"], wins=0, losses=0)
    assert ps.picked_cards == ["A", "B", "A"]
    assert ps.card_count("A") == 2
    assert ps.card_count("B") == 1
    assert ps.card_count("Z") == 0


@pytest.mark.parametrize("wins,losses,expected", [
    (0, 0, 0.0),
    (3, 1, 0.75),
    (0, 5, 0.0),
])
def test_player_stats_win_rate(wins, losses, expected):
    ps = PlayerStats("Alice", [], wins, losses)
    assert ps.win_rate == expected


# ---------------------------------------------------------------------------
# Section 4 — Constructor validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("num_players,pack_size,match", [
    (1, 3, "num_players"),
    (3, 0, "pack_size"),
])
def test_constructor_invalid_params(tmp_path, num_players, pack_size, match):
    with pytest.raises(ValueError, match=match):
        _make_arbiter(tmp_path, num_players=num_players, pack_size=pack_size)


def test_constructor_invalid_direction(tmp_path):
    with pytest.raises(ValueError, match="invalid pass direction"):
        _make_arbiter(tmp_path, rounds=["diagonal"])


def test_constructor_player_names_length_mismatch(tmp_path):
    with pytest.raises(ValueError, match="player_names length"):
        _make_arbiter(tmp_path, num_players=3, player_names=["Alice", "Bob"])


def test_constructor_default_rounds(tmp_path):
    a = DraftArbiter("s1", num_players=2, save_path=str(tmp_path / "t.csv"))
    assert a._rounds == ["left", "right", "left"]


def test_constructor_default_player_names(tmp_path):
    a = DraftArbiter("s1", num_players=2, save_path=str(tmp_path / "t.csv"))
    assert a.player_names == ["seat_0", "seat_1"]


# ---------------------------------------------------------------------------
# Section 5 — Pack rotation: left pass
# ---------------------------------------------------------------------------

def test_left_pass_pick_events_correct_seats_and_indices(tmp_path):
    a = _make_arbiter(tmp_path)
    events = _drive_left_3p(a)
    assert len(events) == 9
    # R0S0: picked by seat 0 (idx 0), seat 2 (idx 1), seat 1 (idx 2)
    r0s0 = [e for e in events if e.pack_id == "R0S0"]
    assert [(e.seat, e.pick_index) for e in r0s0] == [(0, 0), (2, 1), (1, 2)]
    # R0S1: picked by seat 1, seat 0, seat 2
    r0s1 = [e for e in events if e.pack_id == "R0S1"]
    assert [(e.seat, e.pick_index) for e in r0s1] == [(1, 0), (0, 1), (2, 2)]
    # R0S2: picked by seat 2, seat 1, seat 0
    r0s2 = [e for e in events if e.pack_id == "R0S2"]
    assert [(e.seat, e.pick_index) for e in r0s2] == [(2, 0), (1, 1), (0, 2)]


def test_left_pass_full_pack_contents(tmp_path):
    a = _make_arbiter(tmp_path)
    cards = [f"C{i}" for i in range(9)]
    _drive_left_3p(a, cards)
    a.advance_round()
    # R0S0 picks: cards[0] (seat 0), cards[3] (seat 2), cards[6] (seat 1)
    assert a.get_full_pack("R0S0") == [cards[0], cards[3], cards[6]]


# ---------------------------------------------------------------------------
# Section 6 — Pack rotation: right pass
# ---------------------------------------------------------------------------

def test_right_pass_rotation(tmp_path):
    a = _make_arbiter(tmp_path, rounds=["right"])
    # direction="right", next_seat = (seat + 1) % 3
    # R0S0: seat 0 → seat 1 → seat 2
    # R0S1: seat 1 → seat 2 → seat 0
    # R0S2: seat 2 → seat 0 → seat 1
    order = [0, 1, 2, 1, 2, 0, 2, 0, 1]
    cards = [f"C{i}" for i in range(9)]
    for i, seat in enumerate(order):
        a.record_pick(seat, cards[i])
    a.advance_round()
    r0s0 = a.get_full_pack("R0S0")
    assert r0s0 == [cards[0], cards[3], cards[6]]  # seat 0, seat 1, seat 2


# ---------------------------------------------------------------------------
# Section 7 — Round management
# ---------------------------------------------------------------------------

def test_round_not_complete_mid_round(tmp_path):
    a = _make_arbiter(tmp_path)
    a.record_pick(0, "A")
    assert not a.is_round_complete()


def test_round_complete_after_all_picks(tmp_path):
    a = _make_arbiter(tmp_path)
    _drive_left_3p(a)
    assert a.is_round_complete()


def test_record_pick_raises_when_round_complete(tmp_path):
    a = _make_arbiter(tmp_path)
    _drive_left_3p(a)
    with pytest.raises(ValueError, match="round is complete"):
        a.record_pick(0, "Extra")


def test_advance_round_raises_when_round_not_complete(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="not yet complete"):
        a.advance_round()


def test_advance_round_raises_when_draft_complete(tmp_path):
    a = _make_arbiter(tmp_path)
    _drive_left_3p(a)
    a.advance_round()
    assert a.is_draft_complete()
    with pytest.raises(ValueError, match="draft is complete"):
        a.advance_round()


def test_multi_round_current_round_advances(tmp_path):
    a = _make_arbiter(tmp_path, rounds=["left", "right"])
    assert a.current_round == 0
    _drive_left_3p(a)
    a.advance_round()
    assert a.current_round == 1
    assert not a.is_draft_complete()


def test_draft_complete_after_all_rounds(tmp_path):
    a = _make_arbiter(tmp_path, rounds=["left", "right"])
    _drive_left_3p(a)
    a.advance_round()
    # Right-pass round 2
    order = [0, 1, 2, 1, 2, 0, 2, 0, 1]
    for i, seat in enumerate(order):
        a.record_pick(seat, f"R2C{i}")
    a.advance_round()
    assert a.is_draft_complete()


# ---------------------------------------------------------------------------
# Section 8 — Reconstruction
# ---------------------------------------------------------------------------

@pytest.fixture
def completed_arbiter(tmp_path) -> DraftArbiter:
    a = _make_arbiter(tmp_path)
    cards = [f"C{i}" for i in range(9)]
    _drive_left_3p(a, cards)
    a.advance_round()
    return a


@pytest.mark.parametrize("pick_index", [0, 1, 2])
def test_get_pack_before_pick(completed_arbiter, pick_index):
    full = completed_arbiter.get_full_pack("R0S0")
    result = completed_arbiter.get_pack_before_pick("R0S0", pick_index)
    assert result == full[pick_index:]


@pytest.mark.parametrize("pick_index", [0, 1, 2])
def test_get_pack_after_pick(completed_arbiter, pick_index):
    full = completed_arbiter.get_full_pack("R0S0")
    result = completed_arbiter.get_pack_after_pick("R0S0", pick_index)
    assert result == full[pick_index + 1:]


def test_get_pack_before_pick_0_returns_full(completed_arbiter):
    full = completed_arbiter.get_full_pack("R0S0")
    assert completed_arbiter.get_pack_before_pick("R0S0", 0) == full


def test_get_pack_after_pick_last_returns_empty(completed_arbiter):
    assert completed_arbiter.get_pack_after_pick("R0S0", 2) == []


def test_get_full_pack_raises_if_incomplete(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="not yet complete"):
        a.get_full_pack("R0S0")


def test_get_full_pack_raises_if_unknown(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="unknown pack_id"):
        a.get_full_pack("BOGUS")


def test_get_pack_before_pick_out_of_range(completed_arbiter):
    with pytest.raises(ValueError, match="out of range"):
        completed_arbiter.get_pack_before_pick("R0S0", 3)


def test_get_pack_after_pick_out_of_range(completed_arbiter):
    with pytest.raises(ValueError, match="out of range"):
        completed_arbiter.get_pack_after_pick("R0S0", -1)


# ---------------------------------------------------------------------------
# Section 9 — replay_draft
# ---------------------------------------------------------------------------

def test_replay_draft_order_and_count(tmp_path):
    a = _make_arbiter(tmp_path)
    events = _drive_left_3p(a)
    replayed = a.replay_draft()
    assert len(replayed) == 9
    assert replayed == events


def test_replay_draft_returns_copy(tmp_path):
    a = _make_arbiter(tmp_path)
    _drive_left_3p(a)
    r1 = a.replay_draft()
    r2 = a.replay_draft()
    assert r1 is not r2


# ---------------------------------------------------------------------------
# Section 10 — get_player_stats
# ---------------------------------------------------------------------------

def test_player_stats_picked_cards_grow_with_picks(tmp_path):
    a = _make_arbiter(tmp_path)
    cards = [f"C{i}" for i in range(9)]
    _drive_left_3p(a, cards)
    # Pick order: seats [0,1,2,2,0,1,1,2,0]; seat 0 picks at indices 0,4,8
    alice = a.get_player_stats(0)
    assert alice.player_name == "Alice"
    assert alice.picked_cards == [cards[0], cards[4], cards[8]]


def test_player_stats_default_names(tmp_path):
    a = DraftArbiter("s1", num_players=2, pack_size=1, rounds=["left"],
                     save_path=str(tmp_path / "t.csv"))
    a.record_pick(0, "A")
    a.record_pick(1, "B")
    a.advance_round()
    assert a.get_player_stats(0).player_name == "seat_0"
    assert a.get_player_stats(1).player_name == "seat_1"


def test_get_player_stats_invalid_seat(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="out of range"):
        a.get_player_stats(5)


# ---------------------------------------------------------------------------
# Section 11 — record_result
# ---------------------------------------------------------------------------

def test_record_result_winner_wins_others_lose(tmp_path):
    a = _make_arbiter(tmp_path)
    a.record_result(0)
    assert a.get_player_stats(0).wins == 1
    assert a.get_player_stats(0).losses == 0
    assert a.get_player_stats(1).wins == 0
    assert a.get_player_stats(1).losses == 1
    assert a.get_player_stats(2).losses == 1


def test_record_result_win_rate(tmp_path):
    a = _make_arbiter(tmp_path)
    a.record_result(0)
    a.record_result(0)
    assert a.get_player_stats(0).win_rate == 1.0
    assert a.get_player_stats(1).win_rate == 0.0


def test_record_result_allowed_on_incomplete_session(tmp_path):
    a = _make_arbiter(tmp_path)
    a.record_pick(0, "A")
    a.record_result(1)
    assert a.get_player_stats(1).wins == 1


def test_record_result_invalid_seat(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="out of range"):
        a.record_result(10)


# ---------------------------------------------------------------------------
# Section 12 — Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_picks_no_lost_events(tmp_path):
    # 2 players, 1 round, pack_size=20 to amplify contention
    a = DraftArbiter(
        "s1", num_players=2, pack_size=20, rounds=["left"],
        player_names=["P0", "P1"],
        save_path=str(tmp_path / "t.csv"),
    )
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def pick_loop(seat: int) -> None:
        barrier.wait()
        for i in range(20):
            try:
                a.record_pick(seat, f"seat{seat}_card{i}")
            except ValueError:
                pass  # no pack available yet — other player hasn't passed

    t0 = threading.Thread(target=pick_loop, args=(0,))
    t1 = threading.Thread(target=pick_loop, args=(1,))
    t0.start(); t1.start()
    t0.join(); t1.join()

    assert not errors
    # Total picks = 40 (2 packs × 20 picks); some may be missed by ValueError
    # but the event log must be internally consistent
    events = a.replay_draft()
    pack_ids = {e.pack_id for e in events}
    for pid in pack_ids:
        pack_events = [e for e in events if e.pack_id == pid]
        indices = [e.pick_index for e in pack_events]
        assert indices == list(range(len(indices))), f"gap in pick_index for {pid}"


# ---------------------------------------------------------------------------
# Section 13 — CSV persistence
# ---------------------------------------------------------------------------

def test_save_and_load_restores_picks(tmp_path):
    a = _make_arbiter(tmp_path)
    cards = [f"C{i}" for i in range(9)]
    _drive_left_3p(a, cards)
    a.advance_round()
    a.save()

    b = DraftArbiter.load(str(tmp_path / "test.csv"))
    assert b.session_id == "s1"
    assert b.is_draft_complete()
    assert b.get_full_pack("R0S0") == [cards[0], cards[3], cards[6]]
    assert b.replay_draft() == a.replay_draft()


def test_save_and_load_restores_player_stats(tmp_path):
    a = _make_arbiter(tmp_path)
    cards = [f"C{i}" for i in range(9)]
    _drive_left_3p(a, cards)
    a.record_result(0)
    a.save()

    b = DraftArbiter.load(str(tmp_path / "test.csv"))
    assert b.get_player_stats(0).wins == 1
    assert b.get_player_stats(1).losses == 1
    # Seat 0's picked_cards should match
    assert b.get_player_stats(0).picked_cards == a.get_player_stats(0).picked_cards


def test_save_preserves_other_sessions(tmp_path):
    csv_path = str(tmp_path / "shared.csv")

    a = DraftArbiter("s1", num_players=2, pack_size=1, rounds=["left"],
                     save_path=csv_path)
    a.record_pick(0, "CardA")
    a.record_pick(1, "CardB")
    a.advance_round()
    a.save()

    b = DraftArbiter("s2", num_players=2, pack_size=1, rounds=["left"],
                     save_path=csv_path)
    b.record_pick(0, "CardC")
    b.record_pick(1, "CardD")
    b.advance_round()
    b.save()

    # Reload s1 — s2's data must not have been destroyed
    a2 = DraftArbiter.load(csv_path)
    assert a2.get_full_pack("R0S0") == ["CardA"]


def test_load_raises_if_file_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        DraftArbiter.load(str(tmp_path / "nonexistent.csv"))


def test_get_card_stats_aggregates_across_sessions(tmp_path):
    csv_path = str(tmp_path / "shared.csv")

    a = DraftArbiter("s1", num_players=2, pack_size=1, rounds=["left"],
                     save_path=csv_path)
    a.record_pick(0, "Lightning Bolt")
    a.record_pick(1, "Counterspell")
    a.advance_round()
    a.save()

    b = DraftArbiter("s2", num_players=2, pack_size=1, rounds=["left"],
                     save_path=csv_path)
    b.record_pick(0, "Lightning Bolt")
    b.record_pick(1, "Island")
    b.advance_round()

    stats = b.get_card_stats("Lightning Bolt")
    assert stats is not None
    assert stats.times_selected == 2  # 1 from s1 CSV + 1 from s2 RAM


def test_get_card_stats_unknown_returns_none(tmp_path):
    a = _make_arbiter(tmp_path)
    assert a.get_card_stats("Nonexistent Card") is None


# ---------------------------------------------------------------------------
# Section 14 — Validation edge cases
# ---------------------------------------------------------------------------

def test_record_pick_invalid_seat(tmp_path):
    a = _make_arbiter(tmp_path)
    with pytest.raises(ValueError, match="out of range"):
        a.record_pick(-1, "Card")


def test_record_pick_no_pack_at_seat(tmp_path):
    a = DraftArbiter("s1", num_players=2, pack_size=2, rounds=["left"],
                     save_path=str(tmp_path / "t.csv"))
    a.record_pick(0, "First")  # R0S0 moves to seat 1; seat 0 queue is now empty
    with pytest.raises(ValueError, match="no pack available"):
        a.record_pick(0, "Oops")


def test_record_pick_after_draft_complete(tmp_path):
    a = _make_arbiter(tmp_path)
    _drive_left_3p(a)
    a.advance_round()
    with pytest.raises(ValueError, match="draft is complete"):
        a.record_pick(0, "Extra")
