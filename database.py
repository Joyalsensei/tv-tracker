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
            print(f"  [WARN] Migration warning (shows.user_status): {e}")

    # Migration: add last_watched_at column for sorting by recent activity
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN last_watched_at TEXT DEFAULT NULL')
        print("  Added column: shows.last_watched_at")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (shows.last_watched_at): {e}")

    # Migration: add total_episodes column for episode progress tracking
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN total_episodes INTEGER DEFAULT 0')
        print("  Added column: shows.total_episodes")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (shows.total_episodes): {e}")

    # Migration: add google_id column for Google OAuth login
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE DEFAULT NULL')
        print("  Added column: users.google_id")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (users.google_id): {e}")

    # Migration: add created_at column for the profile "member since" metric
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT NULL')
        print("  Added column: users.created_at")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (users.created_at): {e}")

    # Migration: password reset support (admin-driven, no email service).
    # The token is stored as a SHA-256 hash so a DB leak doesn't expose
    # working reset links; expiry is a UTC timestamp.
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN password_reset_token TEXT DEFAULT NULL')
        print("  Added column: users.password_reset_token")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (users.password_reset_token): {e}")

    try:
        cursor.execute('ALTER TABLE users ADD COLUMN password_reset_expires TEXT DEFAULT NULL')
        print("  Added column: users.password_reset_expires")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (users.password_reset_expires): {e}")

    # Migration: add email column for Google OAuth users
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN email TEXT DEFAULT NULL')
        print("  Added column: users.email")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (users.email): {e}")

    # Migration: add runtime column (minutes per episode for TV, total for movies)
    # Used by the stats page to compute watch time accurately instead of
    # guessing a flat 22/120 minutes for every title.
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN runtime INTEGER DEFAULT 0')
        print("  Added column: shows.runtime")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (shows.runtime): {e}")

    # Migration: add rating column (TMDB vote_average captured at add time)
    # Used by the stats page for average rating + rating distribution charts.
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN rating REAL DEFAULT 0')
        print("  Added column: shows.rating")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (shows.rating): {e}")

    # Migration: add genres column (comma-separated names captured at add time)
    # Used by the stats page genre chart without per-show TMDB calls.
    try:
        cursor.execute('ALTER TABLE shows ADD COLUMN genres TEXT DEFAULT NULL')
        print("  Added column: shows.genres")
    except Exception as e:
        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
            pass
        else:
            print(f"  [WARN] Migration warning (shows.genres): {e}")

    # ── Indexes for performance ──
    # watched_episodes: filtering by show+user is the most common query
    try:
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_show_user
            ON watched_episodes(show_tmdb_id, user_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_user_season
            ON watched_episodes(user_id, season_number)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_shows_user_status
            ON shows(user_id, status)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_watched_movies_user
            ON watched_movies(user_id, movie_tmdb_id)
        ''')
        # user-scoped scan for stats/history/continue-watching queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_user
            ON watched_episodes(user_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_shows_user
            ON shows(user_id)
        ''')
    except Exception as e:
        print(f"  [WARN] Index warning: {e}")

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


def backup_db(db_path=None):
    """Create a timestamped SQLite backup file using the online backup API.

    Safe to run while the app is live (WAL mode allows concurrent readers).
    Backups are written to <db_path>.backup.<YYYYMMDD-HHMMSS> and are
    gitignored (see .gitignore: *.db.backup.*).

    Usage: python database.py backup
    """
    import shutil
    import time
    if db_path is None:
        db_path = get_db_path()
    timestamp = time.strftime('%Y%m%d-%H%M%S')
    backup_path = f"{db_path}.backup.{timestamp}"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)  # consistent, lock-safe online backup
            print(f"  [OK] Backed up database to {backup_path}")
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backup":
        backup_db()
    else:
        init_db()
