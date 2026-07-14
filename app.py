from flask import Flask, jsonify, render_template, request, redirect, session, flash, url_for, abort
import requests
import os
import time
import secrets
import sys
import logging
import traceback
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from pathlib import Path

# Log ALL errors to stdout so we can see them in Render logs
logging.basicConfig(level=logging.ERROR, stream=sys.stdout, force=True)
logger = logging.getLogger(__name__)

from database import init_db, get_conn, use_pg, exe, exemany, lastrowid

# Load .env from the same directory as this file (works from any working directory)
load_dotenv(Path(__file__).parent / '.env')

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
API_KEY = os.environ.get("TMDB_API_KEY")
DATABASE_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "tracker.db"))

# Security: ensure required secrets are configured
if not API_KEY:
    raise RuntimeError(
        "TMDB_API_KEY is required. "
        "Copy .env.example to .env and set TMDB_API_KEY to your TMDB API key."
    )

# Log all 500 errors with full traceback to Render logs
@app.errorhandler(500)
def handle_500(error):
    logger.error(f"500 ERROR: {error}")
    logger.error(traceback.format_exc())
    return "Internal Server Error", 500

# Session cookie security
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=2592000,  # 30 days
    MAX_CONTENT_LENGTH=1024 * 1024,
)

# ── TMDB Response Cache (in-memory, TTL-based) ──────────────────────
_tmdb_cache = {}
CACHE_TTL = 300  # 5 minutes


def _cache_key(url, params):
    return f"{url}?{hash(frozenset(params.items()))}"


def tmdb_get(url, params, ttl=CACHE_TTL):
    """Fetch TMDB data with in-memory cache and retry/backoff for 429s."""
    key = _cache_key(url, params)
    cached = _tmdb_cache.get(key)
    if cached and time.time() - cached["ts"] < ttl:
        return cached["data"]
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 429:
                wait = 2 ** attempt  # exponential backoff: 1, 2, 4 seconds
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            _tmdb_cache[key] = {"data": data, "ts": time.time()}
            return data
        except requests.exceptions.RequestException:
            if attempt == 2:
                return None
            time.sleep(1)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://image.tmdb.org; "
        "connect-src 'self' https://api.themoviedb.org; "
        "frame-ancestors 'self';"
    )
    return response


def generate_csrf_token():
    """Generate or retrieve a CSRF token, refreshing only once per minute."""
    if "_csrf_token" not in session or "_csrf_token_ts" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session["_csrf_token_ts"] = time.time()
    elif time.time() - session["_csrf_token_ts"] > 60:
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session["_csrf_token_ts"] = time.time()
    return session["_csrf_token"]


def validate_csrf_token(token):
    return token and token == session.get("_csrf_token")


app.jinja_env.globals["csrf_token"] = generate_csrf_token

# Initialize database on startup (fail gracefully on Render if DB is down)
print("Connecting to database...")
try:
    init_db(DATABASE_PATH)
    print("Database connected successfully!")
except Exception as e:
    print(f"WARNING: Database init failed: {e}", file=sys.stderr)
    print("App will start but database-dependent features may not work.", file=sys.stderr)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "error")
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ── Helper: get total episode count for a show ─────────────────────
def get_show_episode_count(show_id):
    """Fetch & cache total episode count for a TV show.
    Returns (episode_count, show_data_dict).
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not data:
        return 0, None
    total = sum(
        s["episode_count"] for s in data.get("seasons", [])
        if s["season_number"] != 0
    )
    return total, data


def get_season_episode_count(show_id, season_number):
    """Get the number of episodes in a specific season."""
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}",
        {"api_key": API_KEY}
    )
    if not data:
        return 0
    return len(data.get("episodes", []))


# ── Helper: build poster / backdrop URLs ────────────────────────────
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"


# ═══════════════════════════════════════════════════════════════════
#  HOME
# ═══════════════════════════════════════════════════════════════════
@app.route('/')
def home():
    """Netflix-style home page with category shelves."""
    trending_tv = tmdb_get(
        "https://api.themoviedb.org/3/trending/tv/week",
        {"api_key": API_KEY}
    )
    trending_movies = tmdb_get(
        "https://api.themoviedb.org/3/trending/movie/week",
        {"api_key": API_KEY}
    )
    popular_tv = tmdb_get(
        "https://api.themoviedb.org/3/tv/popular",
        {"api_key": API_KEY}
    )
    top_rated = tmdb_get(
        "https://api.themoviedb.org/3/tv/top_rated",
        {"api_key": API_KEY}
    )
    popular_movies = tmdb_get(
        "https://api.themoviedb.org/3/movie/popular",
        {"api_key": API_KEY}
    )

    # Genre shelves (TV genres)
    genre_shelves = []
    genre_ids = {
        "Action & Adventure": 10759,
        "Comedy": 35,
        "Sci-Fi & Fantasy": 10765,
        "Drama": 18,
        "Mystery": 9648,
    }
    for label, genre_id in genre_ids.items():
        data = tmdb_get(
            "https://api.themoviedb.org/3/discover/tv",
            {"api_key": API_KEY, "with_genres": genre_id, "sort_by": "popularity.desc", "page": 1}
        )
        if data and data.get("results"):
            genre_shelves.append({
                "label": label,
                "results": data["results"][:20]
            })

    return render_template(
        'search.html',
        trending_tv=(trending_tv or {}).get("results", [])[:12],
        trending_movies=(trending_movies or {}).get("results", [])[:12],
        popular_tv=(popular_tv or {}).get("results", [])[:12],
        top_rated=(top_rated or {}).get("results", [])[:12],
        popular_movies=(popular_movies or {}).get("results", [])[:12],
        genre_shelves=genre_shelves,
    )


# ── Health check endpoint (Render friendly) ────────────────────────────
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200


# ═══════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            flash("Username and password required.", "error")
            return render_template('signup.html', username=username)
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template('signup.html', username=username)

        password_confirm = request.form.get('password_confirm', '')
        if password != password_confirm:
            flash("Passwords do not match.", "error")
            return render_template('signup.html', username=username)

        password_hash = generate_password_hash(password)

        try:
            conn = get_conn()
            try:
                cursor = conn.cursor()
                id_suffix = " RETURNING id" if use_pg() else ""
                exe(cursor,
                    f'INSERT INTO users (username, password_hash) VALUES (?, ?){id_suffix}',
                    (username, password_hash))
                conn.commit()
                session['user_id'] = lastrowid(cursor)
                return redirect('/myshows')
            except Exception:
                flash("Username already taken.", "error")
                return render_template('signup.html', username=username)
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"SIGNUP ERROR: {e}")
            logger.error(traceback.format_exc())
            flash("An error occurred. Please try again.", "error")
            return render_template('signup.html', username=username)

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            flash("Username and password required.", "error")
            return redirect('/login')

        try:
            conn = get_conn()
            try:
                cursor = conn.cursor()
                exe(cursor, 'SELECT id, password_hash FROM users WHERE username=?', (username,))
                user = cursor.fetchone()
            finally:
                conn.close()

            if user and check_password_hash(user[1], password):
                session['user_id'] = user[0]
                return redirect('/myshows')
            else:
                flash("Invalid username or password.", "error")
                return redirect('/login')
        except Exception as e:
            logger.error(f"LOGIN ERROR: {e}")
            logger.error(traceback.format_exc())
            flash("An error occurred. Please try again.", "error")
            return redirect('/login')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect('/login')


# ═══════════════════════════════════════════════════════════════════
#  SEARCH
# ═══════════════════════════════════════════════════════════════════
@app.route('/search')
def search():
    query = request.args.get('query', '').strip()
    search_type = request.args.get('type', 'tv')

    if not query:
        return redirect('/')

    url = f"https://api.themoviedb.org/3/search/{search_type}"
    params = {"api_key": API_KEY, "query": query}
    data = tmdb_get(url, params)

    if data is None:
        flash("Couldn't reach TMDB. Check your connection and try again.", "error")
        return redirect('/')

    results = data.get("results", [])
    return render_template('search_results.html', results=results, search_type=search_type, query=query)


# ═══════════════════════════════════════════════════════════════════
#  ADD SHOW / MOVIE
# ═══════════════════════════════════════════════════════════════════
@app.route('/add/<int:show_id>')
@login_required
def add_show(show_id):
    url = f"https://api.themoviedb.org/3/tv/{show_id}"
    params = {"api_key": API_KEY}
    show = tmdb_get(url, params)

    if show is None:
        flash("Couldn't fetch show details.", "error")
        return redirect('/')

    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (show["id"], session['user_id']))
        if cursor.fetchone():
            flash(f"{show['name']} is already in your shows.", "info")
        else:
            exe(cursor, '''
                INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (show["id"], show["name"], show["poster_path"], show.get("status", ""), show.get("first_air_date", ""), session['user_id']))
            conn.commit()
            flash(f"Added {show['name']}!", "success")
    finally:
        conn.close()

    return redirect(request.referrer or '/myshows')


@app.route('/add_movie/<int:movie_id>')
@login_required
def add_movie(movie_id):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": API_KEY}
    movie = tmdb_get(url, params)

    if movie is None:
        flash("Couldn't fetch movie details.", "error")
        return redirect('/')

    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (movie["id"], session['user_id']))
        if cursor.fetchone():
            flash(f"{movie['title']} is already in your movies.", "info")
        else:
            exe(cursor, '''
                INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (movie["id"], movie["title"], movie["poster_path"], "Movie", movie.get("release_date", ""), session['user_id']))
            conn.commit()
            flash(f"Added {movie['title']} to My Movies!", "success")
    finally:
        conn.close()

    return redirect(request.referrer or '/mymovies')


# ═══════════════════════════════════════════════════════════════════
#  MY SHOWS
# ═══════════════════════════════════════════════════════════════════
@app.route('/myshows')
@login_required
def my_shows():
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''
            SELECT tmdb_id, name, poster_path, status, first_air_date 
            FROM shows WHERE user_id=? AND status != "Movie"
        ''', (session['user_id'],))
        shows = cursor.fetchall()

        shows_with_progress = []
        for show_row in shows:
            show_id = show_row[0]
            exe(cursor, '''
                SELECT COUNT(*) FROM watched_episodes 
                WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
            ''', (show_id, session['user_id']))
            watched_count = cursor.fetchone()[0]

            total_episodes, show_data = get_show_episode_count(show_id)
            rating = show_data.get("vote_average", 0) if show_data else 0

            percent = int((watched_count / total_episodes) * 100) if total_episodes else 0

            shows_with_progress.append(show_row + (watched_count, total_episodes, percent, rating))
    finally:
        conn.close()

    in_progress = [s for s in shows_with_progress if s[7] < 100]
    completed = [s for s in shows_with_progress if s[7] == 100]

    return render_template('myshows.html', shows=in_progress, completed=completed)


# ═══════════════════════════════════════════════════════════════════
#  MY MOVIES
# ═══════════════════════════════════════════════════════════════════
@app.route('/mymovies')
@login_required
def my_movies():
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''
            SELECT tmdb_id, name, poster_path, status, first_air_date 
            FROM shows WHERE user_id=? AND status = "Movie"
        ''', (session['user_id'],))
        movies = cursor.fetchall()

        movies_with_status = []
        for movie in movies:
            exe(cursor, 'SELECT movie_tmdb_id FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (movie[0], session['user_id']))
            watched = cursor.fetchone() is not None

            movie_data = tmdb_get(
                f"https://api.themoviedb.org/3/movie/{movie[0]}",
                {"api_key": API_KEY}
            )
            rating = movie_data.get("vote_average", 0) if movie_data else 0

            movies_with_status.append(movie + (watched, rating))
    finally:
        conn.close()

    return render_template('mymovies.html', movies=movies_with_status)


# ═══════════════════════════════════════════════════════════════════
#  REMOVE
# ═══════════════════════════════════════════════════════════════════
@app.route('/remove/<int:show_id>', methods=['POST'])
@login_required
def remove_show(show_id):
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'DELETE FROM shows WHERE tmdb_id=? AND user_id=?', (show_id, session['user_id']))
        exe(cursor, 'DELETE FROM watched_episodes WHERE show_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
        exe(cursor, 'DELETE FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
        conn.commit()
    finally:
        conn.close()
    return redirect(request.referrer or '/myshows')


# ═══════════════════════════════════════════════════════════════════
#  SHOW / MOVIE DETAIL
# ═══════════════════════════════════════════════════════════════════
@app.route('/show/<int:show_id>')
@login_required
def show_detail(show_id):
    params = {"api_key": API_KEY, "append_to_response": "recommendations,similar"}

    # Try TV show first
    url = f"https://api.themoviedb.org/3/tv/{show_id}"
    show = tmdb_get(url, {**params, "append_to_response": "recommendations,similar,videos"})

    if show and show.get("seasons") is not None:
        providers = tmdb_get(
            f"https://api.themoviedb.org/3/tv/{show_id}/watch/providers",
            {"api_key": API_KEY}
        )
        watch_providers = []
        if providers:
            results = providers.get("results", {})
            for country in ["IN", "US"]:
                region = results.get(country, {})
                if region:
                    flatrate = region.get("flatrate", [])
                    watch_providers = [p for p in flatrate]
                    break

        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            is_in_shows = cursor.fetchone() is not None
        finally:
            conn.close()

        return render_template(
            'show_detail.html',
            show=show,
            watch_providers=watch_providers,
            rating=show.get("vote_average", 0),
            vote_count=show.get("vote_count", 0),
            recommendations=(show.get("recommendations") or {}).get("results", [])[:10],
            similar=(show.get("similar") or {}).get("results", [])[:10],
            is_in_shows=is_in_shows,
        )

    # Try movie
    movie_url = f"https://api.themoviedb.org/3/movie/{show_id}"
    movie_data = tmdb_get(movie_url, {"api_key": API_KEY, "append_to_response": "recommendations,similar,videos"})

    if movie_data and movie_data.get("title"):
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT movie_tmdb_id FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            is_watched = cursor.fetchone() is not None
        finally:
            conn.close()

        providers = tmdb_get(
            f"https://api.themoviedb.org/3/movie/{show_id}/watch/providers",
            {"api_key": API_KEY}
        )
        watch_providers = []
        if providers:
            results = providers.get("results", {})
            for country in ["IN", "US"]:
                region = results.get(country, {})
                if region:
                    flatrate = region.get("flatrate", [])
                    watch_providers = [p for p in flatrate]
                    break

        return render_template(
            'movie_detail.html',
            movie=movie_data,
            is_watched=is_watched,
            watch_providers=watch_providers,
            rating=movie_data.get("vote_average", 0),
            vote_count=movie_data.get("vote_count", 0),
            recommendations=(movie_data.get("recommendations") or {}).get("results", [])[:10],
            similar=(movie_data.get("similar") or {}).get("results", [])[:10],
        )

    flash("Couldn't load details.", "error")
    return redirect('/myshows')


# ═══════════════════════════════════════════════════════════════════
#  SEASON DETAIL
# ═══════════════════════════════════════════════════════════════════
@app.route('/show/<int:show_id>/season/<int:season_number>')
@login_required
def season_detail(show_id, season_number):
    url = f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}"
    params = {"api_key": API_KEY}
    season = tmdb_get(url, params)

    if season is None:
        flash("Couldn't load season.", "error")
        return redirect('/myshows')

    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''
            SELECT episode_number FROM watched_episodes
            WHERE show_tmdb_id=? AND season_number=? AND user_id=?
        ''', (show_id, season_number, session['user_id']))
        watched_rows = cursor.fetchall()
    finally:
        conn.close()

    watched_episodes = {row[0] for row in watched_rows}

    return render_template('season_detail.html', season=season, show_id=show_id, watched_episodes=watched_episodes)


# ═══════════════════════════════════════════════════════════════════
#  MARK WATCHED (episode toggle)
# ═══════════════════════════════════════════════════════════════════
@app.route('/watch/<int:show_id>/<int:season_number>/<int:episode_number>', methods=['POST'])
@login_required
def mark_watched(show_id, season_number, episode_number):
    token = request.form.get('_csrf_token')
    if not validate_csrf_token(token):
        abort(403)

    user_id = session['user_id']
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''
            SELECT id FROM watched_episodes 
            WHERE show_tmdb_id=? AND season_number=? AND episode_number=? AND user_id=?
        ''', (show_id, season_number, episode_number, user_id))
        existing = cursor.fetchone()

        if existing:
            exe(cursor, 'DELETE FROM watched_episodes WHERE id=?', (existing[0],))
            status = "unwatched"
        else:
            exe(cursor, '''
                INSERT INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                VALUES (?, ?, ?, ?)
            ''', (show_id, season_number, episode_number, user_id))
            status = "watched"

        conn.commit()

        exe(cursor, '''
            SELECT COUNT(*) FROM watched_episodes 
            WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
        ''', (show_id, user_id))
        watched_count = cursor.fetchone()[0]
    finally:
        conn.close()

    total_episodes, _ = get_show_episode_count(show_id)
    finished = (watched_count == total_episodes and total_episodes > 0 and status == "watched")

    total_in_season = get_season_episode_count(show_id, season_number)
    is_last_episode = (episode_number == total_in_season and total_in_season > 0)

    return jsonify({
        "status": status,
        "finished": finished,
        "is_last_episode": is_last_episode,
        "season_number": season_number,
    })


# ═══════════════════════════════════════════════════════════════════
#  MARK SEASON WATCHED (bulk within a single season)
# ═══════════════════════════════════════════════════════════════════
@app.route('/mark_season_watched/<int:show_id>/<int:season_number>/<int:up_to_episode>', methods=['POST'])
@login_required
def mark_season_watched(show_id, season_number, up_to_episode):
    user_id = session['user_id']
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        episodes = [(show_id, season_number, ep, user_id) for ep in range(1, up_to_episode + 1)]
        exemany(cursor, '''
            INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
            VALUES (?, ?, ?, ?)
        ''', episodes)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok", "marked_up_to": up_to_episode})


# ═══════════════════════════════════════════════════════════════════
#  MARK ALL PREVIOUS SEASONS (multi-season fix!)
# ═══════════════════════════════════════════════════════════════════
@app.route('/mark_previous_seasons/<int:show_id>/<int:season_number>', methods=['POST'])
@login_required
def mark_previous_seasons(show_id, season_number):
    """Mark all episodes from all seasons BEFORE the given season as watched."""
    user_id = session['user_id']
    show_data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not show_data:
        return jsonify({"status": "error", "message": "Could not fetch show data"})

    conn = get_conn()
    try:
        cursor = conn.cursor()
        episodes = []
        for season in show_data.get("seasons", []):
            sn = season["season_number"]
            if sn > 0 and sn < season_number:
                ep_count = season.get("episode_count", 0)
                for ep in range(1, ep_count + 1):
                    episodes.append((show_id, sn, ep, user_id))
        if episodes:
            exemany(cursor, '''
                INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                VALUES (?, ?, ?, ?)
            ''', episodes)
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "ok", "marked_previous_seasons_up_to": season_number - 1})


# ═══════════════════════════════════════════════════════════════════
#  MARK ALL SEASONS WATCHED
# ═══════════════════════════════════════════════════════════════════
@app.route('/mark_all_seasons_watched/<int:show_id>', methods=['POST'])
@login_required
def mark_all_seasons_watched(show_id):
    """Mark every episode of every season as watched for a show."""
    user_id = session['user_id']
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    show_data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not show_data:
        return jsonify({"status": "error", "message": "Could not fetch show data"})

    conn = get_conn()
    try:
        cursor = conn.cursor()
        episodes = []
        for season in show_data.get("seasons", []):
            sn = season["season_number"]
            if sn > 0:
                ep_count = season.get("episode_count", 0)
                for ep in range(1, ep_count + 1):
                    episodes.append((show_id, sn, ep, user_id))
        if episodes:
            exemany(cursor, '''
                INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                VALUES (?, ?, ?, ?)
            ''', episodes)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok", "marked_count": len(episodes)})


# ═══════════════════════════════════════════════════════════════════
#  WATCH MOVIE TOGGLE
# ═══════════════════════════════════════════════════════════════════
@app.route('/watch_movie/<int:movie_id>', methods=['POST'])
@login_required
def watch_movie(movie_id):
    user_id = session['user_id']
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT movie_tmdb_id FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (movie_id, user_id))
        existing = cursor.fetchone()
        if existing:
            exe(cursor, 'DELETE FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (movie_id, user_id))
            status = "unwatched"
        else:
            exe(cursor, 'INSERT INTO watched_movies (movie_tmdb_id, user_id) VALUES (?, ?)', (movie_id, user_id))
            status = "watched"
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": status})


# ═══════════════════════════════════════════════════════════════════
#  WATCHED HISTORY
# ═══════════════════════════════════════════════════════════════════
@app.route('/history')
@login_required
def history():
    """Watched history page — shows recently watched (timeline) and all watched content."""
    user_id = session['user_id']

    conn = get_conn()
    try:
        cursor = conn.cursor()

        # Watched movies with details
        exe(cursor, '''
            SELECT wm.movie_tmdb_id, COALESCE(s.name, 'Unknown'), COALESCE(s.poster_path, ''), wm.watched_at
            FROM watched_movies wm
            LEFT JOIN shows s ON wm.movie_tmdb_id = s.tmdb_id AND wm.user_id = s.user_id
            WHERE wm.user_id = ?
            ORDER BY wm.watched_at DESC
        ''', (user_id,))
        watched_movies = cursor.fetchall()

        # Completed shows (100% watched)
        exe(cursor, '''
            SELECT tmdb_id, name, poster_path, status, first_air_date
            FROM shows WHERE user_id=? AND status != "Movie"
        ''', (user_id,))
        all_shows = cursor.fetchall()
    finally:
        conn.close()

    completed_shows = []
    for row in all_shows:
        show_id = row[0]
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''
                SELECT COUNT(*) FROM watched_episodes
                WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
            ''', (show_id, user_id))
            watched_count = cursor.fetchone()[0]

            exe(cursor, '''
                SELECT watched_at FROM watched_episodes
                WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
                ORDER BY watched_at DESC LIMIT 1
            ''', (show_id, user_id))
            latest_row = cursor.fetchone()
            completed_at = latest_row[0] if latest_row else None
        finally:
            conn.close()

        total_episodes, show_data = get_show_episode_count(show_id)
        if total_episodes > 0 and watched_count >= total_episodes:
            rating = show_data.get("vote_average", 0) if show_data else 0
            completed_shows.append(row + (completed_at, rating))

    completed_shows.sort(key=lambda s: s[5] or "", reverse=True)

    # Build merged timeline (newest first)
    timeline = []
    for movie in watched_movies:
        timeline.append({
            "type": "movie",
            "tmdb_id": movie[0],
            "name": movie[1],
            "poster_path": movie[2],
            "date": movie[3],
        })
    for show in completed_shows:
        timeline.append({
            "type": "show",
            "tmdb_id": show[0],
            "name": show[1],
            "poster_path": show[2],
            "date": show[6],
        })
    timeline.sort(key=lambda x: x["date"] or "", reverse=True)

    def get_rating(tmdb_id, media_type):
        if media_type == "movie":
            data = tmdb_get(f"https://api.themoviedb.org/3/movie/{tmdb_id}", {"api_key": API_KEY})
        else:
            data = tmdb_get(f"https://api.themoviedb.org/3/tv/{tmdb_id}", {"api_key": API_KEY})
        return round(data.get("vote_average", 0), 1) if data else 0

    for item in timeline[:20]:
        item["rating"] = get_rating(item["tmdb_id"], item["type"])

    watched_movies_with_ratings = []
    for movie in watched_movies:
        watched_movies_with_ratings.append(movie + (get_rating(movie[0], "movie"),))

    return render_template(
        'history.html',
        timeline=timeline,
        watched_movies=watched_movies_with_ratings,
        completed_shows=completed_shows,
    )


# ═══════════════════════════════════════════════════════════════════
#  ADMIN DASHBOARD (only you can see this)
# ═══════════════════════════════════════════════════════════════════
@app.route('/admin')
@login_required
def admin_dashboard():
    """Simple admin page — shows user stats. Only the first user (you) can access."""
    user_id = session['user_id']

    # Only the first registered user (you) can see this
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT id FROM users ORDER BY id ASC LIMIT 1')
        first_user = cursor.fetchone()
        if not first_user or first_user[0] != user_id:
            flash("Admin access restricted.", "error")
            return redirect('/')

        # Total users
        exe(cursor, 'SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]

        # Recent signups (last 10)
        exe(cursor, 'SELECT id, username FROM users ORDER BY id DESC LIMIT 10')
        recent_users = cursor.fetchall()

        # Total shows
        exe(cursor, 'SELECT COUNT(*) FROM shows')
        total_shows = cursor.fetchone()[0]

        # Total movies
        exe(cursor, 'SELECT COUNT(*) FROM shows WHERE status = "Movie"')
        total_movies = cursor.fetchone()[0]

        # Total TV shows
        exe(cursor, 'SELECT COUNT(*) FROM shows WHERE status != "Movie"')
        total_tv = cursor.fetchone()[0]

        # Total watched episodes
        exe(cursor, 'SELECT COUNT(*) FROM watched_episodes')
        total_watched_eps = cursor.fetchone()[0]

        # Total watched movies
        exe(cursor, 'SELECT COUNT(*) FROM watched_movies')
        total_watched_movies = cursor.fetchone()[0]

        # Shows per user (top contributors)
        exe(cursor, '''
            SELECT u.username, COUNT(s.tmdb_id) as cnt
            FROM users u
            LEFT JOIN shows s ON u.id = s.user_id
            GROUP BY u.id
            ORDER BY cnt DESC
            LIMIT 10
        ''')
        user_activity = cursor.fetchall()

    finally:
        conn.close()

    return render_template('admin.html',
        total_users=total_users,
        recent_users=recent_users,
        total_shows=total_shows,
        total_movies=total_movies,
        total_tv=total_tv,
        total_watched_eps=total_watched_eps,
        total_watched_movies=total_watched_movies,
        user_activity=user_activity,
    )


if __name__ == "__main__":
    app.run(debug=True)
