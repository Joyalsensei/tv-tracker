import os
import re
import time
import psycopg2
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "tracker.db"))

# ── Connection pooling (simple, single-connection cache) ────────────
# For Render's free tier with single worker, we reuse one connection
# to avoid the expensive SSL handshake on every request.
_pg_conn = None
_pg_conn_ts = 0
_PG_CONN_TTL = 300  # Reconnect every 5 minutes to avoid stale connections


class _PgConnection:
    """A wrapper around a PostgreSQL connection that makes close() a no-op
    so the same connection can be reused across requests. The underlying
    psycopg2 connection is cached in the module-level _pg_conn variable.
    """
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        """No-op for PostgreSQL – connection is cached and reused."""
        pass

    def __getattr__(self, name):
        # Fallback to the underlying psycopg2 connection for any other attributes
        return getattr(self._conn, name)


def _get_pg_conn():
    """Get or reuse a PostgreSQL connection with health check.
    Returns a _PgConnection wrapper that makes close() a no-op.
    """
    global _pg_conn, _pg_conn_ts

    # Check if existing connection is still alive and within TTL
    now = time.time()
    if _pg_conn is not None and (now - _pg_conn_ts) < _PG_CONN_TTL:
        try:
            # Lightweight health check
            cur = _pg_conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return _PgConnection(_pg_conn)
        except Exception:
            # Connection is dead, will reconnect below
            try:
                _pg_conn.close()
            except Exception:
                pass
            _pg_conn = None

    # Ensure DATABASE_URL has connect_timeout to avoid hanging
    db_url = DATABASE_URL
    if 'connect_timeout' not in (db_url or ''):
        separator = '&' if '?' in db_url else '?'
        db_url = f"{db_url}{separator}connect_timeout=10"

    _pg_conn = psycopg2.connect(db_url)
    _pg_conn_ts = now
    return _PgConnection(_pg_conn)


# ── Database connection helpers ────────────────────────────────

def get_conn():
    """Return a database connection — PostgreSQL (cached) if DATABASE_URL is set, otherwise SQLite."""
    if DATABASE_URL:
        return _get_pg_conn()
    return sqlite3.connect(DB_PATH)


def use_pg():
    """Return True if we are using PostgreSQL (DATABASE_URL is set)."""
    return DATABASE_URL is not None


# ── Table-aware ON CONFLICT targets for PostgreSQL ────────────────
_CONFLICT_TARGETS = {
    'shows': 'ON CONFLICT (tmdb_id, user_id) DO NOTHING',
    'watched_episodes': 'ON CONFLICT (show_tmdb_id, season_number, episode_number, user_id) DO NOTHING',
    'watched_movies': 'ON CONFLICT (movie_tmdb_id, user_id) DO NOTHING',
    'favorites': 'ON CONFLICT (show_tmdb_id, user_id) DO NOTHING',
    'users': 'ON CONFLICT (username) DO NOTHING',
}


def _adapt(sql):
    """Adapt SQLite SQL syntax to PostgreSQL syntax when needed."""
    if not use_pg():
        return sql
    s = sql.replace('?', '%s')
    if 'INSERT OR IGNORE' in s:
        s = s.replace('INSERT OR IGNORE', 'INSERT')
        s = s.rstrip().rstrip(';')
        # Parse table name to pick the correct conflict target
        match = re.match(r'INSERT\s+INTO\s+(\w+)', s, re.IGNORECASE)
        if match:
            table = match.group(1).lower()
            conflict = _CONFLICT_TARGETS.get(table, 'ON CONFLICT DO NOTHING')
            s += ' ' + conflict
        else:
            s += ' ON CONFLICT DO NOTHING'
        if sql.strip().endswith(';'):
            s += ';'
    return s


def order_nulls_last(column):
    """Return ORDER BY with NULLS LAST for PostgreSQL, plain for SQLite.
    Use like: f\"ORDER BY {order_nulls_last('column_name')} DESC\"
    """
    if use_pg():
        return f'{column} NULLS LAST'
    return column


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
    For PostgreSQL, you must append 'RETURNING id' to your INSERT statement.
    """
    if use_pg():
        return cursor.fetchone()[0]
    return cursor.lastrowid


def init_db(db_path=None):
    if db_path is None:
        db_path = DB_PATH

    if DATABASE_URL:
        conn = _get_pg_conn()
    else:
        conn = sqlite3.connect(db_path)

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
    if not use_pg():
        conn.close()
    print("Database initialized")


if __name__ == "__main__":
    init_db()
