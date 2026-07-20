import os
import sqlite3
from pathlib import Path

_DEFAULT_DB_PATH = str(Path(__file__).parent / "tracker.db")


def get_db_path():
    """Return the database path from env var, or the project default."""
    return os.environ.get("DATABASE_PATH", _DEFAULT_DB_PATH)


def get_conn():
    """Return a SQLite database connection."""
    conn = sqlite3.connect(get_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path=None):
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shows (
            tmdb_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            poster_path TEXT,
            status TEXT,
            first_air_date TEXT,
            user_id INTEGER NOT NULL,
            total_episodes INTEGER DEFAULT 0,
            PRIMARY KEY (tmdb_id, user_id)
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

    # Migration: add user_status column for Show Status Management
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN user_status TEXT DEFAULT NULL')
        print("  Added column: shows.user_status")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass  # Column already exists
        else:
            print(f"  ⚠️  Migration warning (shows.user_status): {e}")

    # Migration: add last_watched_at column for sorting by recent activity
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN last_watched_at TEXT DEFAULT NULL')
        print("  Added column: shows.last_watched_at")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  ⚠️  Migration warning (shows.last_watched_at): {e}")

    # Migration: add total_episodes column for episode progress tracking
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN total_episodes INTEGER DEFAULT 0')
        print("  Added column: shows.total_episodes")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  ⚠️  Migration warning (shows.total_episodes): {e}")

    # Migration: add google_id column for Google OAuth login
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE DEFAULT NULL')
        print("  Added column: users.google_id")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  ⚠️  Migration warning (users.google_id): {e}")

    # Migration: add email column for Google OAuth users
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN email TEXT DEFAULT NULL')
        print("  Added column: users.email")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  ⚠️  Migration warning (users.email): {e}")

    conn.commit()
    conn.close()
    print(f"Database initialized at {db_path}")


# ── Simple SQL wrappers (keeps app.py clean) ──────────────────────

def exe(cursor, sql, params=None):
    if params is not None:
        return cursor.execute(sql, params)
    return cursor.execute(sql)


def exemany(cursor, sql, seq_of_params):
    return cursor.executemany(sql, seq_of_params)


def lastrowid(cursor):
    return cursor.lastrowid


if __name__ == "__main__":
    init_db()
