"""Restore the local SQLite tracker.db from the Neon PostgreSQL database.

The app's real data (all users, shows, movies, watch history) lives in the
Neon Postgres cloud DB referenced by DATABASE_URL in .env. The app code was
changed back to SQLite-only, so this script pulls a full copy back down.

SAFETY: read-only on Postgres. On SQLite, it backs up the current file,
deletes it, re-inits the schema, then imports every table.

Usage:  python tools/restore_from_neon.py
"""
import io
import os
import shutil
import sys
import time
import sqlite3

import psycopg2
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

load_dotenv()
PG_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tracker.db")


def main():
    if not PG_URL:
        print("ERROR: DATABASE_URL not set in .env")
        sys.exit(1)

    # 1. Backup current SQLite DB
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{DB_PATH}.pre-neon-restore.{ts}"
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
        print(f"[OK] Backed up current DB -> {backup_path}")
    else:
        print("[i] No existing tracker.db to back up")

    # 2. Fresh SQLite schema
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for suffix in ("-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)

    print("[i] Initializing fresh SQLite schema...")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import init_db
    init_db(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 3. Read from Neon (read-only)
    print("[i] Connecting to Neon PostgreSQL (read-only)...")
    pg = psycopg2.connect(PG_URL, connect_timeout=10)
    pgc = pg.cursor()

    def fetch(sql):
        pgc.execute(sql)
        return pgc.fetchall()

    # --- users ---
    rows = fetch("SELECT id, username, password_hash FROM users ORDER BY id")
    cur.executemany(
        "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)", rows
    )
    print(f"[OK] users: {len(rows)}")

    # --- shows ---
    rows = fetch(
        "SELECT tmdb_id, name, poster_path, status, first_air_date, user_id, "
        "total_episodes FROM shows ORDER BY user_id, tmdb_id"
    )
    cur.executemany(
        "INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, "
        "user_id, total_episodes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    n_tv = sum(1 for r in rows if r[3] != "Movie")
    n_mov = sum(1 for r in rows if r[3] == "Movie")
    print(f"[OK] shows: {len(rows)} total ({n_tv} TV, {n_mov} movies)")

    # --- watched_episodes ---
    rows = fetch(
        "SELECT id, show_tmdb_id, season_number, episode_number, user_id, "
        "watched_at FROM watched_episodes ORDER BY id"
    )
    cur.executemany(
        "INSERT INTO watched_episodes (id, show_tmdb_id, season_number, "
        "episode_number, user_id, watched_at) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    print(f"[OK] watched_episodes: {len(rows)}")

    # --- watched_movies ---
    rows = fetch(
        "SELECT movie_tmdb_id, user_id, watched_at FROM watched_movies "
        "ORDER BY user_id, movie_tmdb_id"
    )
    cur.executemany(
        "INSERT INTO watched_movies (movie_tmdb_id, user_id, watched_at) "
        "VALUES (?, ?, ?)",
        rows,
    )
    print(f"[OK] watched_movies: {len(rows)}")

    conn.commit()
    conn.close()
    pg.close()

    # 4. Re-run migrations so the restored DB has all current columns
    print("[i] Re-running migrations on restored DB...")
    init_db(DB_PATH)

    # 5. Verify
    v = sqlite3.connect(DB_PATH)
    for t in ("users", "shows", "watched_episodes", "watched_movies"):
        print(f"  verify {t}: {v.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    print("\nDone. tracker.db now contains the full Neon dataset.")


if __name__ == "__main__":
    main()
