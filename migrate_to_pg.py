"""
Migrate data from local SQLite (tracker.db) to PostgreSQL (DATABASE_URL).

Usage:
    python migrate_to_pg.py "postgresql://..."

You can provide the URL as a command-line argument, or set the DATABASE_URL
environment variable, or add DATABASE_URL=... to your .env file.

Safe to run multiple times - uses ON CONFLICT DO NOTHING.
"""

import os
import sys
import sqlite3
from pathlib import Path

try:
    import psycopg2
except ImportError:
    print('psycopg2 not installed. Run: pip install psycopg2-binary')
    sys.exit(1)

DB_PATH = os.environ.get('DATABASE_PATH', str(Path(__file__).parent / 'tracker.db'))

# Try to get DATABASE_URL from (in order):
# 1. Command-line argument
# 2. Environment variable
# 3. .env file (using python-dotenv)
DATABASE_URL = None

if len(sys.argv) > 1:
    DATABASE_URL = sys.argv[1]
elif os.environ.get('DATABASE_URL'):
    DATABASE_URL = os.environ.get('DATABASE_URL')
else:
    # Try loading from .env file
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('DATABASE_URL='):
                DATABASE_URL = line.split('=', 1)[1].strip().strip('"').strip("'")
                break

if not DATABASE_URL:
    print('ERROR: DATABASE_URL not found.')
    print()
    print('Run with your Neon connection string as an argument:')
    print('  python migrate_to_pg.py "postgresql://user:pass@host/db?sslmode=require"')
    print()
    print('Or set the DATABASE_URL environment variable.')

if not os.path.exists(DB_PATH):
    print(f'ERROR: Local database not found at {DB_PATH}')
    sys.exit(1)


def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    return psycopg2.connect(DATABASE_URL)


def count_rows_pg(cursor, table):
    cursor.execute(f'SELECT COUNT(*) FROM {table}')
    return cursor.fetchone()[0]


def migrate_table(label, sqlite_query, pg_insert, sqlite_conn, pg_cursor):
    sqlite_cursor = sqlite_conn.execute(sqlite_query)
    rows = sqlite_cursor.fetchall()
    if not rows:
        print(f'  {label}: 0 rows (nothing to migrate)')
        return 0
    tuples = [tuple(row) for row in rows]
    insert_sql = pg_insert + ' ON CONFLICT DO NOTHING'
    pg_cursor.executemany(insert_sql, tuples)
    pg_cursor.connection.commit()
    print(f'  {label}: {len(tuples)} rows migrated')
    return len(tuples)


def main():
    print('=' * 60)
    print('  SQLite -> PostgreSQL Data Migration')
    print('=' * 60)
    print()

    print('Connecting to SQLite...')
    sqlite_conn = get_sqlite_conn()
    print(f'   Connected: {DB_PATH}')

    print('Connecting to PostgreSQL...')
    pg_conn = get_pg_conn()
    pg_cursor = pg_conn.cursor()
    print('   Connected to Neon PostgreSQL')
    print()

    print('Pre-migration row counts:')
    tables = ['users', 'shows', 'favorites', 'watched_episodes', 'watched_movies']
    for table in tables:
        try:
            count = count_rows_pg(pg_cursor, table)
            print(f'   {table}: {count} rows')
        except Exception as e:
            print(f'   {table}: Table may not exist yet - {e}')
    print()

    print('Migrating data...')
    total = 0

    total += migrate_table('users',
        'SELECT id, username, password_hash FROM users',
        'INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)',
        sqlite_conn, pg_cursor)

    total += migrate_table('shows',
        'SELECT tmdb_id, name, poster_path, status, first_air_date, user_id FROM shows',
        'INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id) VALUES (%s, %s, %s, %s, %s, %s)',
        sqlite_conn, pg_cursor)

    total += migrate_table('favorites',
        'SELECT id, show_tmdb_id, user_id, added_at FROM favorites',
        'INSERT INTO favorites (id, show_tmdb_id, user_id, added_at) VALUES (%s, %s, %s, %s)',
        sqlite_conn, pg_cursor)

    total += migrate_table('watched_episodes',
        'SELECT id, show_tmdb_id, season_number, episode_number, user_id, watched_at FROM watched_episodes',
        'INSERT INTO watched_episodes (id, show_tmdb_id, season_number, episode_number, user_id, watched_at) VALUES (%s, %s, %s, %s, %s, %s)',
        sqlite_conn, pg_cursor)

    total += migrate_table('watched_movies',
        'SELECT movie_tmdb_id, user_id, watched_at FROM watched_movies',
        'INSERT INTO watched_movies (movie_tmdb_id, user_id, watched_at) VALUES (%s, %s, %s)',
        sqlite_conn, pg_cursor)

    print()
    print('=' * 60)
    print(f'  Migration complete! {total} total rows transferred.')
    print('=' * 60)

    print()
    print('Post-migration row counts:')
    for table in tables:
        try:
            count = count_rows_pg(pg_cursor, table)
            print(f'   {table}: {count} rows')
        except Exception as e:
            print(f'   {table}: Error - {e}')

    pg_cursor.close()
    pg_conn.close()
    sqlite_conn.close()
    print()
    print('All done! You can now deploy to Render.')


if __name__ == '__main__':
    main()
