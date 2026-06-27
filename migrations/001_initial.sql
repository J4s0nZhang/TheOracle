CREATE TABLE sessions (
    session_id    TEXT    PRIMARY KEY,
    num_players   INTEGER NOT NULL,
    pack_size     INTEGER NOT NULL DEFAULT 15,
    current_round INTEGER NOT NULL DEFAULT 0,
    is_complete   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE session_rounds (
    session_id     TEXT    NOT NULL REFERENCES sessions(session_id),
    round_index    INTEGER NOT NULL,
    pass_direction TEXT    NOT NULL CHECK (pass_direction IN ('left', 'right')),
    PRIMARY KEY (session_id, round_index)
);

CREATE TABLE players (
    session_id  TEXT    NOT NULL REFERENCES sessions(session_id),
    seat        INTEGER NOT NULL,
    player_name TEXT    NOT NULL,
    PRIMARY KEY (session_id, seat)
);

CREATE TABLE picks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL REFERENCES sessions(session_id),
    round_number    INTEGER NOT NULL,
    origin_seat     INTEGER NOT NULL,
    seat            INTEGER NOT NULL,
    pick_index      INTEGER NOT NULL,
    player_pick_num INTEGER NOT NULL,
    card_name       TEXT    NOT NULL,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, round_number, origin_seat, pick_index)
);

CREATE TABLE results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(session_id),
    winner_seat INTEGER NOT NULL,
    recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_picks_card    ON picks(card_name, pick_index);
CREATE INDEX idx_picks_player  ON picks(session_id, seat, player_pick_num);
CREATE INDEX idx_picks_pack    ON picks(session_id, round_number, origin_seat);
CREATE INDEX idx_results_session ON results(session_id, winner_seat);
