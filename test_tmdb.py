import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("TMDB_API_KEY")
if not API_KEY:
    raise RuntimeError("TMDB_API_KEY environment variable is required. See .env.example")

url = "https://api.themoviedb.org/3/search/tv"
params = {
    "api_key": API_KEY,
    "query": "Breaking Bad"
}

response = requests.get(url, params=params, timeout=10)
response.raise_for_status()
data = response.json()

print(data)