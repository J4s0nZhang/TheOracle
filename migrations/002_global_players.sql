CREATE TABLE IF NOT EXISTS global_players (
    player_id   TEXT PRIMARY KEY,
    player_name TEXT NOT NULL UNIQUE
);
