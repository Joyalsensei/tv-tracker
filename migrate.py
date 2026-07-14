import sqlite3
import shutil
from datetime import datetime


def migrate_db():
    backup_name = f"tracker.db.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy('tracker.db', backup_name)
    print(f"Backed up database to {backup_name}")

    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('PRAGMA foreign_keys = OFF;')
    cursor.execute('BEGIN TRANSACTION;')

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shows_new (
            tmdb_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            poster_path TEXT,
            status TEXT,
            first_air_date TEXT,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (tmdb_id, user_id)
        )
    """)
    cursor.execute("""
        INSERT INTO shows_new (tmdb_id, name, poster_path, status, first_air_date, user_id)
        SELECT tmdb_id, name, poster_path, status, first_air_date, COALESCE(user_id, 1)
        FROM shows
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorites_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_tmdb_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (show_tmdb_id, user_id) REFERENCES shows(tmdb_id, user_id),
            UNIQUE(show_tmdb_id, user_id)
        )
    """)
    cursor.execute("""
        INSERT INTO favorites_new (id, show_tmdb_id, added_at, user_id)
        SELECT f.id, f.show_tmdb_id, f.added_at, COALESCE(s.user_id, 1)
        FROM favorites f
        LEFT JOIN shows s ON f.show_tmdb_id = s.tmdb_id
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_episodes_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_tmdb_id INTEGER NOT NULL,
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            watched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (show_tmdb_id, user_id) REFERENCES shows(tmdb_id, user_id),
            UNIQUE(show_tmdb_id, season_number, episode_number, user_id)
        )
    """)
    cursor.execute("""
        INSERT INTO watched_episodes_new (id, show_tmdb_id, season_number, episode_number, watched_at, user_id)
        SELECT id, show_tmdb_id, season_number, episode_number, watched_at, COALESCE(user_id, 1)
        FROM watched_episodes
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_movies_new (
            movie_tmdb_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            watched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (movie_tmdb_id, user_id)
        )
    """)
    cursor.execute("""
        INSERT INTO watched_movies_new (movie_tmdb_id, user_id, watched_at)
        SELECT movie_tmdb_id, COALESCE(user_id, 1), watched_at
        FROM watched_movies
    """)

    cursor.execute('DROP TABLE IF EXISTS favorites;')
    cursor.execute('DROP TABLE IF EXISTS watched_episodes;')
    cursor.execute('DROP TABLE IF EXISTS watched_movies;')
    cursor.execute('DROP TABLE IF EXISTS shows;')

    cursor.execute('ALTER TABLE shows_new RENAME TO shows;')
    cursor.execute('ALTER TABLE favorites_new RENAME TO favorites;')
    cursor.execute('ALTER TABLE watched_episodes_new RENAME TO watched_episodes;')
    cursor.execute('ALTER TABLE watched_movies_new RENAME TO watched_movies;')

    conn.commit()
    cursor.execute('PRAGMA foreign_keys = ON;')
    conn.close()
    print('Migration completed successfully!')

if __name__ == '__main__':
    migrate_db()
