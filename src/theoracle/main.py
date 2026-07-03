from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .card_parser import parse_card_identifier
from .db import get_connection
from .session_manager import SessionManager

app = FastAPI(title="TheOracle Draft API")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_manager = SessionManager()
_db_path = os.environ.get("ORACLE_DB_PATH", "drafts.db")


def get_manager() -> SessionManager:
    return _manager


def get_db_path() -> str:
    return _db_path


Manager = Annotated[SessionManager, Depends(get_manager)]
DbPath = Annotated[str, Depends(get_db_path)]

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    player_name: str
    num_players: int
    pack_size: int = 15
    rounds: list[str] = ["left", "right", "left"]


class JoinSessionRequest(BaseModel):
    player_name: str


class StartSessionRequest(BaseModel):
    player_id: str


class PickRequest(BaseModel):
    player_id: str
    seat: int
    card_id: str


class AdvanceRequest(BaseModel):
    player_id: str


class ResultRequest(BaseModel):
    player_id: str
    winner_seat: int


class CreateSessionResponse(BaseModel):
    session_id: str
    player_id: str


class JoinResponse(BaseModel):
    player_id: str


class PlayerInfo(BaseModel):
    player_name: str
    seat: int | None
    is_host: bool


class SessionStateResponse(BaseModel):
    session_id: str
    status: str
    num_players: int
    pack_size: int
    rounds: list[str]
    players: list[PlayerInfo]
    current_round: int | None
    is_round_complete: bool
    is_draft_complete: bool


class SeatAssignment(BaseModel):
    player_name: str
    seat: int


class StartResponse(BaseModel):
    seat_assignments: list[SeatAssignment]


class PickResponse(BaseModel):
    card_name: str
    pack_id: str
    pick_index: int
    seat: int


class AdvanceResponse(BaseModel):
    current_round: int
    is_draft_complete: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_session_or_404(session_id: str, manager: SessionManager):
    try:
        return manager.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/sessions", response_model=CreateSessionResponse)
def create_session(body: CreateSessionRequest, manager: Manager, db_path: DbPath):
    try:
        session_id, player_id = manager.create_session(
            player_name=body.player_name,
            num_players=body.num_players,
            pack_size=body.pack_size,
            rounds=body.rounds,
            db_path=db_path,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return CreateSessionResponse(session_id=session_id, player_id=player_id)


@app.post("/sessions/{session_id}/join", response_model=JoinResponse)
def join_session(session_id: str, body: JoinSessionRequest, manager: Manager, db_path: DbPath):
    try:
        player_id = manager.join_session(
            session_id=session_id,
            player_name=body.player_name,
            db_path=db_path,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JoinResponse(player_id=player_id)


@app.get("/sessions/{session_id}", response_model=SessionStateResponse)
def get_session_state(session_id: str, manager: Manager):
    session = _get_session_or_404(session_id, manager)
    with session.lock:
        players = [
            PlayerInfo(
                player_name=p.player_name,
                seat=session.seat_map.get(p.player_id),
                is_host=p.is_host,
            )
            for p in session.players
        ]
        status = session.status
        arbiter = session.arbiter
        num_players = session.num_players

    current_round = None
    is_round_complete = False
    is_draft_complete = False
    if arbiter is not None:
        current_round = arbiter.current_round
        is_round_complete = arbiter.is_round_complete()
        is_draft_complete = arbiter.is_draft_complete()

    return SessionStateResponse(
        session_id=session_id,
        status=status,
        num_players=num_players,
        pack_size=session.pack_size,
        rounds=session.rounds,
        players=players,
        current_round=current_round,
        is_round_complete=is_round_complete,
        is_draft_complete=is_draft_complete,
    )


@app.post("/sessions/{session_id}/start", response_model=StartResponse)
def start_session(session_id: str, body: StartSessionRequest, manager: Manager):
    try:
        seat_map = manager.start_session(session_id=session_id, player_id=body.player_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    session = manager.get_session(session_id)
    with session.lock:
        name_by_pid = {p.player_id: p.player_name for p in session.players}

    assignments = [
        SeatAssignment(player_name=name_by_pid[pid], seat=seat)
        for pid, seat in sorted(seat_map.items(), key=lambda x: x[1])
    ]
    return StartResponse(seat_assignments=assignments)


@app.post("/sessions/{session_id}/picks", response_model=PickResponse)
def record_pick(session_id: str, body: PickRequest, manager: Manager):
    session = _get_session_or_404(session_id, manager)

    with session.lock:
        status = session.status
        actual_seat = session.seat_map.get(body.player_id)
        arbiter = session.arbiter

    if status != "active":
        raise HTTPException(status_code=409, detail=f"session is {status}, not active")
    if actual_seat is None:
        raise HTTPException(status_code=403, detail="player not in session")
    if actual_seat != body.seat:
        raise HTTPException(status_code=403, detail="player_id does not match seat")

    try:
        card = parse_card_identifier(body.card_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if card is None:
        raise HTTPException(status_code=422, detail="card not identified, please retry")

    try:
        event = arbiter.record_pick(seat=body.seat, card_name=card.name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    arbiter.save()
    return PickResponse(
        card_name=card.name,
        pack_id=event.pack_id,
        pick_index=event.pick_index,
        seat=event.seat,
    )


@app.post("/sessions/{session_id}/advance", response_model=AdvanceResponse)
def advance_round(session_id: str, body: AdvanceRequest, manager: Manager):
    session = _get_session_or_404(session_id, manager)

    with session.lock:
        if body.player_id != session.host_player_id:
            raise HTTPException(status_code=403, detail="only the host can advance the round")
        if session.status != "active":
            raise HTTPException(status_code=409, detail=f"session is {session.status}, not active")
        arbiter = session.arbiter

    try:
        arbiter.advance_round()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    is_complete = arbiter.is_draft_complete()
    if is_complete:
        with session.lock:
            session.status = "complete"

    arbiter.save()
    return AdvanceResponse(
        current_round=arbiter.current_round,
        is_draft_complete=is_complete,
    )


@app.post("/sessions/{session_id}/results", status_code=200)
def record_result(session_id: str, body: ResultRequest, manager: Manager):
    session = _get_session_or_404(session_id, manager)

    with session.lock:
        if body.player_id != session.host_player_id:
            raise HTTPException(status_code=403, detail="only the host can record results")
        arbiter = session.arbiter

    if arbiter is None:
        raise HTTPException(status_code=409, detail="session has not started")

    try:
        arbiter.record_result(winner_seat=body.winner_seat)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    arbiter.save()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Website routes
# ---------------------------------------------------------------------------


@app.get("/sessions/{session_id}/stats", response_class=HTMLResponse)
def session_stats_page(request: Request, session_id: str, manager: Manager):
    session = _get_session_or_404(session_id, manager)

    with session.lock:
        status = session.status
        players = list(session.players)
        seat_map = dict(session.seat_map)
        arbiter = session.arbiter
        num_players = session.num_players
        pack_size = session.pack_size
        rounds = list(session.rounds)

    player_stats: dict[int, object] = {}
    current_round = 0
    is_round_complete = False
    is_draft_complete = False

    if arbiter is not None:
        current_round = arbiter.current_round
        is_round_complete = arbiter.is_round_complete()
        is_draft_complete = arbiter.is_draft_complete()
        for seat in range(num_players):
            player_stats[seat] = arbiter.get_player_stats(seat)

    players_ctx = [
        {
            "player_name": p.player_name,
            "seat": seat_map.get(p.player_id),
            "is_host": p.is_host,
        }
        for p in players
    ]
    if status != "waiting":
        players_ctx.sort(key=lambda p: p["seat"])

    return templates.TemplateResponse(
        request,
        "session_stats.html",
        {
            "session_id": session_id,
            "status": status,
            "num_players": num_players,
            "pack_size": pack_size,
            "num_rounds": len(rounds),
            "players": players_ctx,
            "player_stats": player_stats,
            "current_round": current_round,
            "is_round_complete": is_round_complete,
            "is_draft_complete": is_draft_complete,
        },
    )


@app.get("/players/{player_id}", response_class=HTMLResponse)
def player_history_page(request: Request, player_id: str, db_path: DbPath):
    conn = get_connection(db_path)

    name_row = conn.execute(
        "SELECT player_name FROM global_players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if name_row is None:
        raise HTTPException(status_code=404, detail="player not found")

    player_name = name_row["player_name"]

    pick_rows = conn.execute(
        """
        SELECT pk.session_id, pk.round_number, pk.origin_seat, pk.pick_index, pk.card_name
        FROM picks pk
        JOIN players pl ON pl.session_id = pk.session_id AND pl.seat = pk.seat
        JOIN global_players gp ON gp.player_name = pl.player_name
        WHERE gp.player_id = ?
        ORDER BY pk.session_id, pk.id
        """,
        (player_id,),
    ).fetchall()

    sessions_map: dict[str, list] = {}
    for row in pick_rows:
        sid = row["session_id"]
        if sid not in sessions_map:
            sessions_map[sid] = []

        pack_cards = conn.execute(
            "SELECT card_name FROM picks "
            "WHERE session_id = ? AND round_number = ? AND origin_seat = ? "
            "ORDER BY pick_index",
            (sid, row["round_number"], row["origin_seat"]),
        ).fetchall()

        full_pack = [r["card_name"] for r in pack_cards]
        pack_size = conn.execute(
            "SELECT pack_size FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        is_complete = pack_size is not None and len(full_pack) == pack_size["pack_size"]
        pack_before = full_pack[row["pick_index"]:] if is_complete else []

        sessions_map[sid].append(
            {
                "round": row["round_number"],
                "pack_id": f"R{row['round_number']}S{row['origin_seat']}",
                "pick_index": row["pick_index"],
                "card_picked": row["card_name"],
                "pack_before": pack_before,
            }
        )

    sessions_ctx = [
        {"session_id": sid, "picks": picks} for sid, picks in sessions_map.items()
    ]

    return templates.TemplateResponse(
        request,
        "player_history.html",
        {
            "player_name": player_name,
            "sessions": sessions_ctx,
        },
    )


@app.get("/cards", response_class=HTMLResponse)
def cards_page(request: Request, db_path: DbPath):
    conn = get_connection(db_path)

    rows = conn.execute(
        """
        SELECT card_name,
               COUNT(*) AS times_selected,
               AVG(pick_index) AS avg_pick_order
        FROM picks
        GROUP BY card_name
        ORDER BY avg_pick_order DESC
        LIMIT 50
        """
    ).fetchall()

    cards = [
        {
            "card_name": row["card_name"],
            "times_selected": row["times_selected"],
            "avg_pick_order": row["avg_pick_order"],
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        request,
        "cards.html",
        {"cards": cards},
    )
