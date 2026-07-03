from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from uuid import uuid4

from .db import get_connection
from .draft_arbiter import DraftArbiter


@dataclass
class LobbyPlayer:
    player_id: str
    player_name: str
    is_host: bool


@dataclass
class LobbySession:
    session_id: str
    host_player_id: str
    num_players: int
    pack_size: int
    rounds: list[str]
    db_path: str
    players: list[LobbyPlayer] = field(default_factory=list)
    status: str = "waiting"
    seat_map: dict[str, int] = field(default_factory=dict)
    arbiter: DraftArbiter | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, LobbySession] = {}
        self._registry_lock = threading.Lock()

    def _lookup_or_create_player(self, player_name: str, db_path: str) -> str:
        conn = get_connection(db_path)
        new_id = str(uuid4())
        conn.execute(
            "INSERT OR IGNORE INTO global_players (player_id, player_name) VALUES (?, ?)",
            (new_id, player_name),
        )
        conn.commit()
        row = conn.execute(
            "SELECT player_id FROM global_players WHERE player_name = ?",
            (player_name,),
        ).fetchone()
        return row["player_id"]

    def create_session(
        self,
        player_name: str,
        num_players: int,
        pack_size: int,
        rounds: list[str],
        db_path: str,
    ) -> tuple[str, str]:
        player_id = self._lookup_or_create_player(player_name, db_path)
        session_id = str(uuid4())
        session = LobbySession(
            session_id=session_id,
            host_player_id=player_id,
            num_players=num_players,
            pack_size=pack_size,
            rounds=rounds,
            db_path=db_path,
            players=[LobbyPlayer(player_id=player_id, player_name=player_name, is_host=True)],
        )
        with self._registry_lock:
            self._sessions[session_id] = session
        return session_id, player_id

    def join_session(self, session_id: str, player_name: str, db_path: str) -> str:
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)

        with session.lock:
            if session.status != "waiting":
                raise ValueError("session is not accepting new players")
            if len(session.players) >= session.num_players:
                raise ValueError("session is full")
            if any(p.player_name == player_name for p in session.players):
                raise ValueError(f"player name {player_name!r} already taken in this session")
            player_id = self._lookup_or_create_player(player_name, db_path)
            session.players.append(
                LobbyPlayer(player_id=player_id, player_name=player_name, is_host=False)
            )

        return player_id

    def start_session(self, session_id: str, player_id: str) -> dict[str, int]:
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)

        with session.lock:
            if player_id != session.host_player_id:
                raise PermissionError("only the host can start the session")
            if session.status != "waiting":
                raise ValueError(f"session is already {session.status}")
            if len(session.players) < session.num_players:
                raise ValueError(
                    f"need {session.num_players} players, "
                    f"only {len(session.players)} have joined"
                )

            shuffled = random.sample(session.players, len(session.players))
            seat_map = {p.player_id: i for i, p in enumerate(shuffled)}
            player_names = [p.player_name for p in shuffled]

            arbiter = DraftArbiter(
                session_id=session_id,
                num_players=session.num_players,
                pack_size=session.pack_size,
                rounds=session.rounds,
                player_names=player_names,
                db_path=session.db_path,
            )
            arbiter.save()

            session.seat_map = seat_map
            session.arbiter = arbiter
            session.status = "active"

        return dict(session.seat_map)

    def get_session(self, session_id: str) -> LobbySession:
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def verify_player_seat(self, session_id: str, player_id: str, seat: int) -> bool:
        session = self.get_session(session_id)
        with session.lock:
            return session.seat_map.get(player_id) == seat
