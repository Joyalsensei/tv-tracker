import os
import psycopg2
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "tracker.db"))

# ── Database connection helpers ────────────────────────────────

def get_conn():
    """Return a database connection — PostgreSQL if DATABASE_URL is set, otherwise SQLite."""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)


def use_pg():
    """Return True if we are using PostgreSQL (DATABASE_URL is set)."""
    return DATABASE_URL is not None


def _adapt(sql):
    """Adapt SQLite SQL syntax to PostgreSQL syntax when needed."""
    if not use_pg():
        return sql
    s = sql.replace('?', '%s')
    if 'INSERT OR IGNORE' in s:
        # SQLite: INSERT OR IGNORE INTO ... VALUES (...)
        # PG:     INSERT INTO ... VALUES (...) ON CONFLICT DO NOTHING
        s = s.replace('INSERT OR IGNORE', 'INSERT')
        s = s.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        if sql.strip().endswith(';'):
            s += ';'
    return s


def exe(cursor, sql, params=None):
    """Execute SQL with correct placeholder style for the active database."""
    adapted = _adapt(sql)
    if params is not None:
        return cursor.execute(adapted, params)
    return cursor.execute(adapted)


def exemany(cursor, sql, seq_of_params):
    """Execute many SQL statements with correct placeholder style."""
    adapted = _adapt(sql)
    return cursor.executemany(adapted, seq_of_params)


def lastrowid(cursor):
    """Get the last inserted row ID — works with both SQLite and PostgreSQL.
    For PostgreSQL, you must append 'RETURNING id' to your INSERT statement."""
    if use_pg():
        return cursor.fetchone()[0]
    return cursor.lastrowid


def init_db(db_path=None):
    if db_path is None:
        db_path = DB_PATH

    conn = get_conn() if not db_path else (psycopg2.connect(DATABASE_URL) if DATABASE_URL else sqlite3.connect(db_path))
    cursor = conn.cursor()

    if use_pg():
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shows (
                tmdb_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                poster_path TEXT,
                status TEXT,
                first_air_date TEXT,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                id SERIAL PRIMARY KEY,
                show_tmdb_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(show_tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watched_episodes (
                id SERIAL PRIMARY KEY,
                show_tmdb_id INTEGER NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(show_tmdb_id, season_number, episode_number, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watched_movies (
                movie_tmdb_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (movie_tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        ''')
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shows (
                tmdb_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                poster_path TEXT,
                status TEXT,
                first_air_date TEXT,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_tmdb_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (show_tmdb_id, user_id) REFERENCES shows(tmdb_id, user_id),
                UNIQUE(show_tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watched_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                show_tmdb_id INTEGER NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                watched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (show_tmdb_id, user_id) REFERENCES shows(tmdb_id, user_id),
                UNIQUE(show_tmdb_id, season_number, episode_number, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watched_movies (
                movie_tmdb_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                watched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (movie_tmdb_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        ''')

    conn.commit()
    conn.close()
    print("Database initialized")


if __name__ == "__main__":
    init_db()
