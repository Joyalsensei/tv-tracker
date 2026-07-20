"""
Episode Count Repair Script - Run after deploying bug fix
Usage: python repair_episode_counts.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

env_path = Path(__file__).parent / '.env'
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

from database import get_conn, exe
from app import get_show_episode_count

def repair_all_shows():
    conn = get_conn()
    cursor = conn.cursor()
    exe(cursor, '''
        SELECT s.tmdb_id, s.name, s.user_id, u.username, s.total_episodes
        FROM shows s JOIN users u ON s.user_id = u.id
        WHERE s.status != 'Movie' ORDER BY u.username, s.name
    ''')
    shows = cursor.fetchall()
    if not shows:
        print("No TV shows found.")
        return
    print(f"Found {len(shows)} show(s) to check.\n")
    corrupted = 0
    fixed = 0
    for tmdb_id, name, user_id, username, old_total in shows:
        exe(cursor, '''
            SELECT COUNT(*) FROM watched_episodes
            WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
        ''', (tmdb_id, user_id))
        watched = cursor.fetchone()[0]
        new_total, _ = get_show_episode_count(tmdb_id)
        if new_total == 0:
            print(f"{username:<16} {name[:26]:<28} TMDB unreachable")
            continue
        is_corrupted = watched > new_total
        needs_update = old_total != new_total
        label = "CORRUPTED" if is_corrupted else ("UPDATED" if needs_update else "OK")
        if is_corrupted: corrupted += 1
        if needs_update and not is_corrupted: fixed += 1
        print(f"{username:<16} {name[:26]:<28} {watched:>4}/{new_total:<6} ({label})")
        if new_total > 0 and (is_corrupted or needs_update):
            exe(cursor, 'UPDATE shows SET total_episodes=? WHERE tmdb_id=? AND user_id=?',
                (new_total, tmdb_id, user_id))
    conn.commit()
    conn.close()
    print(f"\nFixed: {fixed}, Corrupted (repaired): {corrupted}")

if __name__ == "__main__":
    repair_all_shows()
