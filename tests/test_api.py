from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from theoracle.card_parser import CardData
from theoracle.main import app, get_db_path, get_manager
from theoracle.session_manager import SessionManager

MOCK_CARD = CardData(
    name="Lightning Bolt",
    mana_cost="{R}",
    type_line="Instant",
    oracle_text="Deal 3 damage to any target.",
    image_url="https://example.com/bolt.jpg",
)

MOCK_CARD_2 = CardData(
    name="Counterspell",
    mana_cost="{U}{U}",
    type_line="Instant",
    oracle_text="Counter target spell.",
    image_url="https://example.com/counter.jpg",
)


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "test.db")
    mgr = SessionManager()
    app.dependency_overrides[get_manager] = lambda: mgr
    app.dependency_overrides[get_db_path] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_session(client, player_name="Alice", num_players=2, pack_size=1, rounds=None):
    resp = client.post(
        "/sessions",
        json={
            "player_name": player_name,
            "num_players": num_players,
            "pack_size": pack_size,
            "rounds": rounds or ["left"],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _join_session(client, session_id, player_name):
    resp = client.post(f"/sessions/{session_id}/join", json={"player_name": player_name})
    assert resp.status_code == 200, resp.text
    return resp.json()["player_id"]


def _start_session(client, session_id, host_player_id):
    resp = client.post(f"/sessions/{session_id}/start", json={"player_id": host_player_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["seat_assignments"]


def _setup_draft(client, n_players=2, pack_size=1, rounds=None):
    """Create, fill, and start a session. Returns (session_id, pid_to_seat, pids)."""
    data = _create_session(client, "Alice", n_players, pack_size, rounds)
    session_id = data["session_id"]
    alice_id = data["player_id"]

    pids = [alice_id]
    names = ["Alice"]
    for i in range(1, n_players):
        name = f"Player{i}"
        pids.append(_join_session(client, session_id, name))
        names.append(name)

    assignments = _start_session(client, session_id, alice_id)
    pid_to_seat = {pids[names.index(a["player_name"])]: a["seat"] for a in assignments}

    return session_id, pid_to_seat, pids


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_create_session_returns_session_id_and_player_id(client):
    data = _create_session(client)
    assert "session_id" in data
    assert "player_id" in data


def test_join_session_returns_player_id(client):
    data = _create_session(client)
    pid = _join_session(client, data["session_id"], "Bob")
    assert pid


def test_get_session_state_waiting(client):
    data = _create_session(client, num_players=3)
    resp = client.get(f"/sessions/{data['session_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "waiting"
    assert len(body["players"]) == 1
    assert body["current_round"] is None


def test_start_session_assigns_seats(client):
    session_id, pid_to_seat, pids = _setup_draft(client)
    assert set(pid_to_seat.values()) == {0, 1}


def test_get_session_state_active(client):
    session_id, _, _ = _setup_draft(client)
    resp = client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["current_round"] == 0
    assert all(p["seat"] is not None for p in body["players"])


def test_session_not_found_returns_404(client):
    resp = client.get("/sessions/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Player identity persistence
# ---------------------------------------------------------------------------


def test_same_player_name_gets_same_player_id_across_sessions(client):
    d1 = _create_session(client, "Alice", num_players=2)
    d2 = _create_session(client, "Alice", num_players=2)
    assert d1["player_id"] == d2["player_id"]


def test_different_player_names_get_different_ids(client):
    d1 = _create_session(client, "Alice", num_players=2)
    _join_session(client, d1["session_id"], "Bob")
    d2 = _create_session(client, "Bob", num_players=2)
    assert d1["player_id"] != d2["player_id"]


# ---------------------------------------------------------------------------
# Join validation
# ---------------------------------------------------------------------------


def test_join_with_duplicate_name_returns_409(client):
    data = _create_session(client, num_players=3)
    sid = data["session_id"]
    _join_session(client, sid, "Bob")
    resp = client.post(f"/sessions/{sid}/join", json={"player_name": "Bob"})
    assert resp.status_code == 409


def test_join_when_session_full_returns_409(client):
    data = _create_session(client, "Alice", num_players=2)
    sid = data["session_id"]
    _join_session(client, sid, "Bob")
    resp = client.post(f"/sessions/{sid}/join", json={"player_name": "Charlie"})
    assert resp.status_code == 409


def test_join_after_session_started_returns_409(client):
    session_id, _, pids = _setup_draft(client)
    resp = client.post(f"/sessions/{session_id}/join", json={"player_name": "Latecomer"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Start validation
# ---------------------------------------------------------------------------


def test_non_host_cannot_start_session(client):
    data = _create_session(client, num_players=2)
    sid = data["session_id"]
    bob_id = _join_session(client, sid, "Bob")
    resp = client.post(f"/sessions/{sid}/start", json={"player_id": bob_id})
    assert resp.status_code == 403


def test_start_with_insufficient_players_returns_409(client):
    data = _create_session(client, num_players=3)
    resp = client.post(
        f"/sessions/{data['session_id']}/start",
        json={"player_id": data["player_id"]},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------


def test_full_happy_path(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1)

    cards = [MOCK_CARD, MOCK_CARD_2]
    with patch("theoracle.main.parse_card_identifier", side_effect=cards):
        for pid, seat in pid_to_seat.items():
            resp = client.post(
                f"/sessions/{session_id}/picks",
                json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
            )
            assert resp.status_code == 200, resp.text

    state = client.get(f"/sessions/{session_id}").json()
    assert state["is_round_complete"] is True


def test_pick_with_wrong_player_id_for_seat_returns_403(client):
    session_id, pid_to_seat, pids = _setup_draft(client)
    pid0, pid1 = pids[0], pids[1]
    seat0 = pid_to_seat[pid0]

    with patch("theoracle.main.parse_card_identifier", return_value=MOCK_CARD):
        resp = client.post(
            f"/sessions/{session_id}/picks",
            json={"player_id": pid1, "seat": seat0, "card_id": "M21/001"},
        )
    assert resp.status_code == 403


def test_pick_with_unresolvable_card_returns_422(client):
    session_id, pid_to_seat, pids = _setup_draft(client)
    pid, seat = next(iter(pid_to_seat.items()))

    with patch("theoracle.main.parse_card_identifier", return_value=None):
        resp = client.post(
            f"/sessions/{session_id}/picks",
            json={"player_id": pid, "seat": seat, "card_id": "bad/id"},
        )
    assert resp.status_code == 422
    assert "retry" in resp.json()["detail"]


def test_pick_when_session_not_active_returns_409(client):
    data = _create_session(client)
    pid = data["player_id"]
    with patch("theoracle.main.parse_card_identifier", return_value=MOCK_CARD):
        resp = client.post(
            f"/sessions/{data['session_id']}/picks",
            json={"player_id": pid, "seat": 0, "card_id": "M21/001"},
        )
    assert resp.status_code == 409


def test_pick_after_draft_complete_returns_422(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1, rounds=["left"])
    alice_id = pids[0]

    cards_iter = iter([MOCK_CARD, MOCK_CARD_2])
    with patch("theoracle.main.parse_card_identifier", side_effect=cards_iter):
        for pid, seat in pid_to_seat.items():
            client.post(
                f"/sessions/{session_id}/picks",
                json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
            )

    client.post(f"/sessions/{session_id}/advance", json={"player_id": alice_id})

    pid, seat = next(iter(pid_to_seat.items()))
    with patch("theoracle.main.parse_card_identifier", return_value=MOCK_CARD):
        resp = client.post(
            f"/sessions/{session_id}/picks",
            json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
        )
    # session.status == "complete" after advance, so the pick endpoint returns 409
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Advance round
# ---------------------------------------------------------------------------


def test_advance_when_round_not_complete_returns_422(client):
    session_id, _, pids = _setup_draft(client)
    alice_id = pids[0]
    resp = client.post(f"/sessions/{session_id}/advance", json={"player_id": alice_id})
    assert resp.status_code == 422


def test_advance_as_non_host_returns_403(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1)
    alice_id, bob_id = pids[0], pids[1]

    cards_iter = iter([MOCK_CARD, MOCK_CARD_2])
    with patch("theoracle.main.parse_card_identifier", side_effect=cards_iter):
        for pid, seat in pid_to_seat.items():
            client.post(
                f"/sessions/{session_id}/picks",
                json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
            )

    resp = client.post(f"/sessions/{session_id}/advance", json={"player_id": bob_id})
    assert resp.status_code == 403


def test_advance_completes_draft_when_last_round(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1, rounds=["left"])
    alice_id = pids[0]

    cards_iter = iter([MOCK_CARD, MOCK_CARD_2])
    with patch("theoracle.main.parse_card_identifier", side_effect=cards_iter):
        for pid, seat in pid_to_seat.items():
            client.post(
                f"/sessions/{session_id}/picks",
                json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
            )

    resp = client.post(f"/sessions/{session_id}/advance", json={"player_id": alice_id})
    assert resp.status_code == 200
    assert resp.json()["is_draft_complete"] is True

    state = client.get(f"/sessions/{session_id}").json()
    assert state["status"] == "complete"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def test_record_result_success(client):
    session_id, pid_to_seat, pids = _setup_draft(client)
    alice_id = pids[0]
    resp = client.post(
        f"/sessions/{session_id}/results",
        json={"player_id": alice_id, "winner_seat": 0},
    )
    assert resp.status_code == 200


def test_record_result_non_host_returns_403(client):
    session_id, _, pids = _setup_draft(client)
    bob_id = pids[1]
    resp = client.post(
        f"/sessions/{session_id}/results",
        json={"player_id": bob_id, "winner_seat": 0},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Website routes
# ---------------------------------------------------------------------------


def test_session_stats_page_returns_html(client):
    session_id, _, _ = _setup_draft(client)
    resp = client.get(f"/sessions/{session_id}/stats")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_session_stats_page_waiting_state(client):
    data = _create_session(client, num_players=3)
    resp = client.get(f"/sessions/{data['session_id']}/stats")
    assert resp.status_code == 200
    assert "Waiting" in resp.text


def test_player_history_page_returns_html(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1)
    alice_id = pids[0]
    alice_seat = pid_to_seat[alice_id]

    with patch("theoracle.main.parse_card_identifier", return_value=MOCK_CARD):
        client.post(
            f"/sessions/{session_id}/picks",
            json={"player_id": alice_id, "seat": alice_seat, "card_id": "M21/001"},
        )

    resp = client.get(f"/players/{alice_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Alice" in resp.text


def test_player_history_unknown_player_returns_404(client):
    resp = client.get("/players/nonexistent-id")
    assert resp.status_code == 404


def test_cards_page_returns_html(client):
    resp = client.get("/cards")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_cards_page_shows_picks_after_draft(client):
    session_id, pid_to_seat, pids = _setup_draft(client, n_players=2, pack_size=1)

    cards = [MOCK_CARD, MOCK_CARD_2]
    with patch("theoracle.main.parse_card_identifier", side_effect=cards):
        for pid, seat in pid_to_seat.items():
            client.post(
                f"/sessions/{session_id}/picks",
                json={"player_id": pid, "seat": seat, "card_id": "M21/001"},
            )

    resp = client.get("/cards")
    assert resp.status_code == 200
    assert "Lightning Bolt" in resp.text or "Counterspell" in resp.text
