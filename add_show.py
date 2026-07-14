import sqlite3
import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("TMDB_API_KEY")
if not API_KEY:
    raise RuntimeError("TMDB_API_KEY environment variable is required. See .env.example")


def search_show(query):
    url = "https://api.themoviedb.org/3/search/tv"
    params = {"api_key": API_KEY, "query": query}
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()["results"]


def add_show(show, user_id):
    conn = sqlite3.connect('tracker.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO shows (tmdb_id, name,poster_path, status, first_air_date, user_id)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (show["id"], show["name"], show.get("poster_path"), show.get("status", ""), show.get("first_air_date", ""), user_id))
    conn.commit()
    conn.close()
    print(f"Added: {show['name']}")


if __name__ == "__main__":
    import sys
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    results = search_show("Breaking Bad")
    if results:
        first_result = results[0]
        add_show(first_result, user_id)
    else:
        print("No results found.")