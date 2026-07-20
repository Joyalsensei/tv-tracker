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
from markupsafe import escape

# Log ALL errors to stdout so we can see them in Render logs
logging.basicConfig(level=logging.ERROR, stream=sys.stdout, force=True)
logger = logging.getLogger(__name__)

from database import init_db, get_conn, exe, exemany, lastrowid, get_db_path

# Load .env from the same directory as this file (works from any working directory)
load_dotenv(Path(__file__).parent / '.env')

app = Flask(__name__)

# ── Persistent secret key (survives app restarts!) ─────────────
# If FLASK_SECRET_KEY is not set in env, we generate one and save it
# to a file so it stays the same across restarts. This prevents
# sessions from being invalidated every time the app redeploys.
_SECRET_KEY_FILE = Path(__file__).parent / '.secret_key'
secret_key = os.environ.get("FLASK_SECRET_KEY")
if secret_key:
    app.secret_key = secret_key
else:
    print("WARNING: FLASK_SECRET_KEY not set! Sessions will persist via .secret_key file.", file=sys.stderr)
    print("  Set FLASK_SECRET_KEY in your Render env for best results.", file=sys.stderr)
    if _SECRET_KEY_FILE.exists():
        app.secret_key = _SECRET_KEY_FILE.read_text().strip()
    else:
        app.secret_key = secrets.token_hex(32)
        _SECRET_KEY_FILE.write_text(app.secret_key)
        print(f"  Generated persistent key saved to {_SECRET_KEY_FILE}")

API_KEY = os.environ.get("TMDB_API_KEY")
DATABASE_PATH = get_db_path()

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
CACHE_TTL = 600  # 10 minutes (reduced TMDB API calls)


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

# Initialize database on startup
print("=" * 50)
print("  Starting TV Tracker...")
print("=" * 50)
print(f"  Database: SQLite ({DATABASE_PATH})")
print("  Connecting...")
try:
    init_db(DATABASE_PATH)
    print("  ✅ Database connected and tables ready!")
except Exception as e:
    print(f"  ❌ Database init failed: {e}", file=sys.stderr)
print("=" * 50)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "error")
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ── Helpers: episode counts ─────────────────────────────────────

SEASON_CACHE_TTL = 3600  # 1 hour cache for season-level data


def get_season_episode_count(show_id, season_number):
    """Get the ACTUAL number of episodes in a season by fetching the season endpoint.
    Cached for SEASON_CACHE_TTL seconds because the season endpoint returns
    the live episode list (unlike the show endpoint's stale episode_count).
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}",
        {"api_key": API_KEY},
        ttl=SEASON_CACHE_TTL
    )
    if not data:
        return 0
    return len(data.get("episodes", []))


def get_show_episode_count(show_id):
    """Fetch total episode count for a TV show from TMDB.
    Returns (episode_count, show_data_dict).

    🐛 FIX: Now fetches per-season data from the SEASON endpoints
    instead of relying on the show endpoint's stale season-level
    episode_count.  Ongoing shows like One Piece often have
    incorrect episode_count on the show endpoint, but the season
    endpoint always returns the actual episode list.

    Season data is cached for 1 hour to avoid excessive API calls.
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not data:
        return 0, None

    total = 0
    for s in data.get("seasons", []):
        if s["season_number"] <= 0:
            continue
        count = get_season_episode_count(show_id, s["season_number"])
        total += count

    return total, data


def get_season_episode_data(show_id, season_number):
    """Get full episode list for a season from TMDB.
    Returns list of {episode_number, name} dicts.
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}",
        {"api_key": API_KEY}
    )
    if not data or not data.get("episodes"):
        return []
    return [{"episode_number": e["episode_number"], "name": e.get("name", f"Episode {e['episode_number']}")} for e in data["episodes"]]


def _update_cached_total_episodes(show_id, user_id):
    """Update the cached total_episodes in the shows table for a given show+user.
    Called after any bulk mark operation so progress bar stays accurate.
    Returns the total, or 0 if TMDB couldn't be reached.
    """
    total, _ = get_show_episode_count(show_id)
    if total > 0:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'UPDATE shows SET total_episodes=? WHERE tmdb_id=? AND user_id=?', (total, show_id, user_id))
            conn.commit()
        finally:
            conn.close()
        return total
    return 0


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
    """Health check that also warms up the database connection.
    Call this periodically to keep the app and DB awake!
    """
    db_status = "unknown"
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        db_status = "ok"
    except Exception as e:
        db_status = str(e)
    return jsonify({"status": "ok", "database": db_status}), 200


# ═══════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        return render_template('signup.html')

    # POST handling
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
            exe(cursor,
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, password_hash))
            conn.commit()
            user_id = lastrowid(cursor)
            session['user_id'] = user_id
            session['username'] = username
            session.permanent = True
            return redirect('/myshows')
        except Exception:
            flash("Username already taken.", "error")
            return render_template('signup.html', username=username)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"SIGNUP ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not create account. Check Render logs for details.", "error")
        return render_template('signup.html', username=username)


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
                session['username'] = username
                session.permanent = True  # 👈 Keeps you logged in for 30 days
                return redirect('/myshows')
            else:
                flash("Invalid username or password.", "error")
                return redirect('/login')
        except Exception as e:
            logger.error(f"LOGIN ERROR: {e}")
            logger.error(traceback.format_exc())
            flash(f"Could not log in. Check Render logs for details.", "error")
            return redirect('/login')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
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

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (show["id"], session['user_id']))
            if cursor.fetchone():
                flash(f"{escape(show['name'])} is already in your shows.", "info")
            else:
                total_ep, _ = get_show_episode_count(show["id"])
                exe(cursor, '''
                    INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id, total_episodes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (show["id"], show["name"], show["poster_path"], show.get("status", ""), show.get("first_air_date", ""), session['user_id'], total_ep))
                conn.commit()
                flash(f"Added {escape(show['name'])}!", "success")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"ADD SHOW ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Could not add show. Please try again.", "error")

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

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (movie["id"], session['user_id']))
            if cursor.fetchone():
                flash(f"{escape(movie['title'])} is already in your movies.", "info")
            else:
                exe(cursor, '''
                    INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (movie["id"], movie["title"], movie["poster_path"], "Movie", movie.get("release_date", ""), session['user_id']))
                conn.commit()
                flash(f"Added {escape(movie['title'])} to My Movies!", "success")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"ADD MOVIE ERROR ({movie_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Could not add movie. Please try again.", "error")

    return redirect(request.referrer or '/mymovies')


# ═══════════════════════════════════════════════════════════════════
#  MY SHOWS  🐛 FIXED: Always use fresh TMDB totals
# ═══════════════════════════════════════════════════════════════════
@app.route('/myshows')
@login_required
def my_shows():
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''
                SELECT tmdb_id, name, poster_path, status, first_air_date,
                       COALESCE(user_status, ''), last_watched_at
                FROM shows WHERE user_id=? AND status != 'Movie'
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

                # Fetch show data from TMDB for the rating (1 API call, not per-season)
                show_data = tmdb_get(
                    f"https://api.themoviedb.org/3/tv/{show_id}",
                    {"api_key": API_KEY}
                )
                rating = show_data.get("vote_average", 0) if show_data else 0

                # Get cached total_episodes from DB (updated after every mark operation)
                exe(cursor, 'SELECT total_episodes FROM shows WHERE tmdb_id=? AND user_id=?',
                    (show_id, session['user_id']))
                row = cursor.fetchone()
                total_episodes = row[0] if row else 0

                # For ongoing shows, refresh total from TMDB season endpoints (accurate)
                # For ended shows, the DB cached value is fine
                tmdb_status = show_data.get('status') if show_data else None
                if total_episodes == 0 or tmdb_status == 'Returning Series':
                    fresh_total, _ = get_show_episode_count(show_id)
                    if fresh_total > 0:
                        total_episodes = fresh_total
                        exe(cursor, 'UPDATE shows SET total_episodes=? WHERE tmdb_id=? AND user_id=?',
                            (fresh_total, show_id, session['user_id']))

                # 🐛 FIX: Clamp percentage to 100% max (prevent overflow from incorrect data)
                if total_episodes > 0:
                    percent = min(int((watched_count / total_episodes) * 100), 100)
                else:
                    percent = 0

                # show_row[5] = user_status, show_row[6] = last_watched_at
                shows_with_progress.append(show_row + (watched_count, total_episodes, percent, rating))

            conn.commit()

        finally:
            conn.close()

        # Sort: most recently watched first (new shows without activity go last)
        # Tuple indices: [0-4]=show_row, [5]=user_status, [6]=last_watched_at,
        #                [7]=watched_count, [8]=total_episodes, [9]=percent, [10]=rating
        shows_with_progress.sort(key=lambda s: s[6] or '', reverse=True)

        # Apply user-defined status overrides (s[9] = percent, s[5] = user_status)
        in_progress = [s for s in shows_with_progress if s[9] < 100 or s[5] in ('watching', 'on_hold', 'plan_to_watch')]
        completed = [s for s in shows_with_progress if s[9] >= 100 and s[5] not in ('watching', 'on_hold', 'plan_to_watch')]

        return render_template('myshows.html', shows=in_progress, completed=completed)
    except Exception as e:
        logger.error(f"MY SHOWS ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load your shows. Please try again.", "error")
        return redirect('/')


# ═══════════════════════════════════════════════════════════════════
#  MY MOVIES
# ═══════════════════════════════════════════════════════════════════
@app.route('/mymovies')
@login_required
def my_movies():
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''
                SELECT tmdb_id, name, poster_path, status, first_air_date 
                FROM shows WHERE user_id=? AND status = 'Movie'
            ''', (session['user_id'],))
            movies = cursor.fetchall()

            movies_with_status = []
            for movie in movies:
                exe(cursor, 'SELECT movie_tmdb_id FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (movie[0], session['user_id']))
                watched = cursor.fetchone() is not None
                movies_with_status.append(movie + (watched, 0))
        finally:
            conn.close()

        return render_template('mymovies.html', movies=movies_with_status)
    except Exception as e:
        logger.error(f"MY MOVIES ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load your movies. Please try again.", "error")
        return redirect('/')


# ═══════════════════════════════════════════════════════════════════
#  REMOVE
# ═══════════════════════════════════════════════════════════════════
@app.route('/remove/<int:show_id>', methods=['POST'])
@login_required
def remove_show(show_id):
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'DELETE FROM shows WHERE tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            exe(cursor, 'DELETE FROM watched_episodes WHERE show_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            exe(cursor, 'DELETE FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"REMOVE ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Could not remove item. Try again.", "error")
    return redirect(request.referrer or '/myshows')


# ═══════════════════════════════════════════════════════════════════
#  SHOW / MOVIE DETAIL
# ═══════════════════════════════════════════════════════════════════
@app.route('/show/<int:show_id>')
@login_required
def show_detail(show_id):
    try:
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
                exe(cursor, 'SELECT tmdb_id, COALESCE(user_status, \'\') FROM shows WHERE tmdb_id=? AND user_id=?', (show_id, session['user_id']))
                row = cursor.fetchone()
                is_in_shows = row is not None
                user_status = row[1] if row else ''
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
                user_status=user_status,
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
    except Exception as e:
        logger.error(f"SHOW DETAIL ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Couldn't load details.", "error")
        return redirect('/myshows')


# ═══════════════════════════════════════════════════════════════════
#  API: WATCHED EPISODES  — for inline episode tracker
# ═══════════════════════════════════════════════════════════════════
@app.route('/api/show/<int:show_id>/watched')
@login_required
def api_show_watched(show_id):
    """Return the set of watched (season, episode) pairs for the current user + show.
    Used by the inline episode tracker on show_detail.html.
    """
    user_id = session['user_id']
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''SELECT season_number, episode_number FROM watched_episodes
                           WHERE show_tmdb_id=? AND user_id=?''', (show_id, user_id))
            watched = [{"season": row[0], "episode": row[1]} for row in cursor.fetchall()]

            # Also get watched count per season for progress
            exe(cursor, '''SELECT season_number, COUNT(*) FROM watched_episodes
                           WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
                           GROUP BY season_number''', (show_id, user_id))
            season_counts = {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()

        return jsonify({
            "watched": watched,
            "season_counts": season_counts,
        })
    except Exception as e:
        logger.error(f"API WATCHED ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"error": "Could not fetch watched data"}), 500


# ═══════════════════════════════════════════════════════════════════
#  API: SEASON EPISODES (proxied from TMDB — no API key exposed!)
# ═══════════════════════════════════════════════════════════════════
@app.route('/api/show/<int:show_id>/season/<int:season_number>')
@login_required
def api_season_episodes(show_id, season_number):
    """Return a season's episode list from TMDB with watched status
    merged in. Used by the inline episode tracker on show_detail.html.
    """
    user_id = session['user_id']

    # Fetch season data from TMDB via backend (no API key leak)
    season_data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}",
        {"api_key": API_KEY}
    )
    if not season_data or not season_data.get("episodes"):
        return jsonify({"error": "Could not fetch season data", "episodes": []}), 404

    # Get watched episodes for this season
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''SELECT episode_number FROM watched_episodes
                           WHERE show_tmdb_id=? AND season_number=? AND user_id=?''',
                (show_id, season_number, user_id))
            watched_set = {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

        episodes = []
        for ep in season_data["episodes"]:
            episodes.append({
                "episode_number": ep["episode_number"],
                "name": ep.get("name", f"Episode {ep['episode_number']}"),
                "watched": ep["episode_number"] in watched_set,
            })

        return jsonify({
            "season_number": season_number,
            "episodes": episodes,
        })
    except Exception as e:
        logger.error(f"API SEASON ERROR ({show_id}/{season_number}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"error": "Database error"}), 500


# ═══════════════════════════════════════════════════════════════════
#  SEASON DETAIL
# ═══════════════════════════════════════════════════════════════════
@app.route('/show/<int:show_id>/season/<int:season_number>')
@login_required
def season_detail(show_id, season_number):
    try:
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
    except Exception as e:
        logger.error(f"SEASON DETAIL ERROR ({show_id}/{season_number}): {e}")
        logger.error(traceback.format_exc())
        flash("Couldn't load season details.", "error")
        return redirect('/myshows')


# ═══════════════════════════════════════════════════════════════════
#  SHOW STATUS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════
SHOW_STATUSES = {
    "plan_to_watch": "📋 Plan to Watch",
    "watching": "📺 Watching",
    "on_hold": "⏸️ On Hold",
    "dropped": "🗑️ Dropped",
    "completed": "✅ Completed",
}


@app.route('/show/<int:show_id>/set_status', methods=['POST'])
@login_required
def set_show_status(show_id):
    """Set the user's personal tracking status for a show."""
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    new_status = request.form.get('status', '').strip()
    if new_status and new_status not in SHOW_STATUSES:
        return jsonify({"status": "error", "message": "Invalid status."}), 400

    user_id = session['user_id']
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'UPDATE shows SET user_status=? WHERE tmdb_id=? AND user_id=?',
                (new_status if new_status else None, show_id, user_id))
            conn.commit()
        finally:
            conn.close()
        return jsonify({"status": "ok", "new_status": new_status})
    except Exception as e:
        logger.error(f"SET STATUS ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Database error."}), 500


def _update_show_timestamp(show_id, user_id):
    """Update last_watched_at for a show to current time.
    Called after any mark operation so the show jumps to top of My Shows.
    """
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'UPDATE shows SET last_watched_at=CURRENT_TIMESTAMP WHERE tmdb_id=? AND user_id=?',
                (show_id, user_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"UPDATE TIMESTAMP ERROR ({show_id}): {e}")


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
    try:
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

        # 🐛 FIX: Also update the cached total_episodes so progress stays in sync
        total_episodes = _update_cached_total_episodes(show_id, user_id)
        if total_episodes == 0:
            total_episodes, _ = get_show_episode_count(show_id)

        # 🆕 Update last_watched_at so show jumps to top of My Shows
        _update_show_timestamp(show_id, user_id)

        finished = (watched_count == total_episodes and total_episodes > 0)

        total_in_season = get_season_episode_count(show_id, season_number)
        is_last_episode = (episode_number == total_in_season and total_in_season > 0)

        return jsonify({
            "status": status,
            "finished": finished,
            "is_last_episode": is_last_episode,
            "season_number": season_number,
            "watched_count": watched_count,
            "total_episodes": total_episodes,
        })
    except Exception as e:
        logger.error(f"MARK WATCHED ERROR ({show_id}/{season_number}/{episode_number}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Could not mark episode."}), 500


# ═══════════════════════════════════════════════════════════════════
#  MARK SEASON WATCHED (bulk within a single season)
#  🐛 FIXED: Now updates cached total_episodes after marking
# ═══════════════════════════════════════════════════════════════════
@app.route('/mark_season_watched/<int:show_id>/<int:season_number>/<int:up_to_episode>', methods=['POST'])
@login_required
def mark_season_watched(show_id, season_number, up_to_episode):
    user_id = session['user_id']
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    try:
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

        # 🐛 FIX: Update cached total_episodes after bulk mark
        _update_cached_total_episodes(show_id, user_id)
        # 🆕 Update timestamp so show jumps to top
        _update_show_timestamp(show_id, user_id)

        return jsonify({"status": "ok", "marked_up_to": up_to_episode})
    except Exception as e:
        logger.error(f"MARK SEASON WATCHED ERROR ({show_id}/{season_number}/{up_to_episode}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Could not mark season."}), 500


# ═══════════════════════════════════════════════════════════════════
#  MARK ALL PREVIOUS SEASONS
#  🐛 FIXED: Now uses actual season episode data instead of
#  episode_count from the show endpoint, and updates cached total.
# ═══════════════════════════════════════════════════════════════════
@app.route('/mark_previous_seasons/<int:show_id>/<int:season_number>', methods=['POST'])
@login_required
def mark_previous_seasons(show_id, season_number):
    """Mark all episodes from all seasons BEFORE the given season as watched."""
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    user_id = session['user_id']
    show_data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not show_data:
        return jsonify({"status": "error", "message": "Could not fetch show data"})

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            episodes = []
            for season in show_data.get("seasons", []):
                sn = season["season_number"]
                if sn > 0 and sn < season_number:
                    # 🐛 FIX: Get actual episode data from the season endpoint
                    # instead of relying on episode_count from show endpoint,
                    # which can be inaccurate for ongoing shows.
                    season_episodes = get_season_episode_data(show_id, sn)
                    for ep in season_episodes:
                        episodes.append((show_id, sn, ep["episode_number"], user_id))
            if episodes:
                exemany(cursor, '''
                    INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                    VALUES (?, ?, ?, ?)
                ''', episodes)
            conn.commit()
        finally:
            conn.close()

        # 🐛 FIX: Update cached total_episodes after bulk mark
        _update_cached_total_episodes(show_id, user_id)
        _update_show_timestamp(show_id, user_id)

        return jsonify({"status": "ok", "marked_previous_seasons_up_to": season_number - 1, "marked_count": len(episodes)})
    except Exception as e:
        logger.error(f"MARK PREVIOUS SEASONS ERROR ({show_id}/{season_number}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Database error."}), 500


# ═══════════════════════════════════════════════════════════════════
#  MARK ALL SEASONS WATCHED
#  🐛 FIXED: Now uses actual season episode data and updates cached total.
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

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            episodes = []
            for season in show_data.get("seasons", []):
                sn = season["season_number"]
                if sn > 0:
                    # 🐛 FIX: Use actual episode data from the season endpoint
                    season_episodes = get_season_episode_data(show_id, sn)
                    for ep in season_episodes:
                        episodes.append((show_id, sn, ep["episode_number"], user_id))
            if episodes:
                exemany(cursor, '''
                    INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                    VALUES (?, ?, ?, ?)
                ''', episodes)
            conn.commit()
        finally:
            conn.close()

        # 🐛 FIX: Update cached total_episodes after bulk mark
        _update_cached_total_episodes(show_id, user_id)
        _update_show_timestamp(show_id, user_id)

        return jsonify({"status": "ok", "marked_count": len(episodes)})
    except Exception as e:
        logger.error(f"MARK ALL SEASONS WATCHED ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Database error."}), 500


# ═══════════════════════════════════════════════════════════════════
#  WATCH MOVIE TOGGLE
# ═══════════════════════════════════════════════════════════════════
@app.route('/watch_movie/<int:movie_id>', methods=['POST'])
@login_required
def watch_movie(movie_id):
    user_id = session['user_id']
    if not validate_csrf_token(request.form.get('_csrf_token')):
        abort(403)
    try:
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
    except Exception as e:
        logger.error(f"WATCH MOVIE ERROR ({movie_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Database error."}), 500


# ═══════════════════════════════════════════════════════════════════
#  WATCHED HISTORY
# ═══════════════════════════════════════════════════════════════════
@app.route('/history')
@login_required
def history():
    """Watched history page — shows recently watched (timeline) and all watched content."""
    user_id = session['user_id']

    try:
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

            # All user's shows (TV only, not movies)
            exe(cursor, '''
                SELECT tmdb_id, name, poster_path, status, first_air_date
                FROM shows WHERE user_id=? AND status != 'Movie'
            ''', (user_id,))
            all_shows = cursor.fetchall()

            # Completed shows — reuse the same connection, no N+1
            completed_shows = []
            for row in all_shows:
                show_id = row[0]
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

                total_episodes, _ = get_show_episode_count(show_id)
                if total_episodes > 0 and watched_count >= total_episodes:
                    completed_shows.append(row + (completed_at, 0))

            completed_shows.sort(key=lambda s: s[5] or "", reverse=True)

            # Build merged timeline (newest first) — no per-item API calls
            timeline = []
            for movie in watched_movies:
                timeline.append({
                    "type": "movie",
                    "tmdb_id": movie[0],
                    "name": movie[1],
                    "poster_path": movie[2],
                    "date": movie[3],
                    "rating": 0,
                })
            for show in completed_shows:
                timeline.append({
                    "type": "show",
                    "tmdb_id": show[0],
                    "name": show[1],
                    "poster_path": show[2],
                    "date": show[5],
                    "rating": 0,
                })
            timeline.sort(key=lambda x: x["date"] or "", reverse=True)

            watched_movies_simple = [(m[0], m[1], m[2], m[3], 0) for m in watched_movies]

        finally:
            conn.close()

        return render_template(
            'history.html',
            timeline=timeline,
            watched_movies=watched_movies_simple,
            completed_shows=completed_shows,
        )
    except Exception as e:
        logger.error(f"HISTORY ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load history. Please try again.", "error")
        return redirect('/myshows')


# ═══════════════════════════════════════════════════════════════════
#  ADMIN DASHBOARD (only you can see this)
# ═══════════════════════════════════════════════════════════════════

def _is_admin(user_id):
    """Check if the given user_id is the first registered user (admin)."""
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT id FROM users ORDER BY id ASC LIMIT 1')
        first_user = cursor.fetchone()
        return first_user and first_user[0] == user_id
    finally:
        conn.close()


@app.route('/admin')
@login_required
def admin_dashboard():
    """Simple admin page — shows user stats. Only the first user (you) can access."""
    user_id = session['user_id']

    try:
        # Only the first registered user (you) can see this
        if not _is_admin(user_id):
            flash("Admin access restricted.", "error")
            return redirect('/')

        conn = get_conn()
        try:
            cursor = conn.cursor()

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
            exe(cursor, "SELECT COUNT(*) FROM shows WHERE status = 'Movie'")
            total_movies = cursor.fetchone()[0]

            # Total TV shows
            exe(cursor, "SELECT COUNT(*) FROM shows WHERE status != 'Movie'")
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
    except Exception as e:
        logger.error(f"ADMIN ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("An error occurred loading the admin dashboard.", "error")
        return redirect('/')


# ═══════════════════════════════════════════════════════════════════
#  ADMIN: REPAIR EPISODE COUNTS
# ═══════════════════════════════════════════════════════════════════
@app.route('/admin/repair_episodes', methods=['POST'])
@login_required
def admin_repair_episodes():
    """Recalculate total_episodes for all TV shows using accurate season endpoint data.
    Accessible only by admin (first registered user).
    """
    user_id = session['user_id']
    if not _is_admin(user_id):
        return jsonify({"status": "error", "message": "Admin access restricted."}), 403

    if not validate_csrf_token(request.form.get('_csrf_token')):
        return jsonify({"status": "error", "message": "Invalid CSRF token."}), 403

    results = []
    fixed_count = 0
    corrupted_count = 0

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''
                SELECT s.tmdb_id, s.name, s.user_id, u.username, s.total_episodes
                FROM shows s
                JOIN users u ON s.user_id = u.id
                WHERE s.status != 'Movie'
                ORDER BY u.username, s.name
            ''')
            shows = cursor.fetchall()

            for tmdb_id, name, uid, username, old_total in shows:
                # Get watched count
                exe(cursor, '''
                    SELECT COUNT(*) FROM watched_episodes
                    WHERE show_tmdb_id=? AND user_id=? AND season_number != 0
                ''', (tmdb_id, uid))
                watched = cursor.fetchone()[0]

                # Get accurate total from TMDB season endpoints
                new_total, _ = get_show_episode_count(tmdb_id)

                if new_total == 0:
                    results.append({
                        "show": name,
                        "user": username,
                        "status": "error",
                        "detail": "TMDB unreachable"
                    })
                    continue

                is_corrupted = watched > new_total
                needs_update = old_total != new_total

                if is_corrupted:
                    corrupted_count += 1
                    status = "corrupted"
                    detail = f"was {old_total}, now {new_total} (watched {watched} > old total)"
                elif needs_update:
                    fixed_count += 1
                    status = "updated"
                    detail = f"{old_total} -> {new_total}"
                else:
                    status = "ok"
                    detail = f"{new_total} (unchanged)"

                results.append({
                    "show": name,
                    "user": username,
                    "status": status,
                    "detail": detail,
                    "watched": watched,
                    "old_total": old_total,
                    "new_total": new_total,
                })

                if new_total > 0 and (is_corrupted or needs_update):
                    exe(cursor, 'UPDATE shows SET total_episodes=? WHERE tmdb_id=? AND user_id=?',
                        (new_total, tmdb_id, uid))

            conn.commit()
        finally:
            conn.close()

        return jsonify({
            "status": "ok",
            "total": len(shows),
            "fixed": fixed_count,
            "corrupted": corrupted_count,
            "results": results,
        })
    except Exception as e:
        logger.error(f"ADMIN REPAIR ERROR: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
#  UPCOMING EPISODES CALENDAR
# ═══════════════════════════════════════════════════════════════════
@app.route('/upcoming')
@login_required
def upcoming():
    """Show upcoming episodes for all the user's tracked shows that are still airing.
    Uses TMDB's next_episode_to_air field (1 API call per show).
    """
    user_id = session['user_id']
    today = time.strftime('%Y-%m-%d')

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, '''
                SELECT tmdb_id, name, poster_path
                FROM shows WHERE user_id=? AND status != 'Movie'
            ''', (user_id,))
            shows = cursor.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"UPCOMING DB ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load upcoming episodes.", "error")
        return redirect('/myshows')

    upcoming_list = []
    newly_aired = []  # episodes that recently aired (next_ep with past date)

    for tmdb_id, name, poster_path in shows:
        show_data = tmdb_get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            {"api_key": API_KEY}
        )
        if not show_data:
            continue

        # Use next_episode_to_air from TMDB — it's the single source of truth
        # For date comparisons, ISO 8601 strings work with >= (lexicographic order)
        next_ep = show_data.get("next_episode_to_air")
        if next_ep and next_ep.get("air_date"):
            ep = {
                "show_id": tmdb_id,
                "show_name": name,
                "poster_path": poster_path,
                "season": next_ep["season_number"],
                "episode": next_ep["episode_number"],
                "name": next_ep.get("name", f"Episode {next_ep['episode_number']}"),
                "air_date": next_ep["air_date"],
                "overview": (next_ep.get("overview") or "")[:150],
                "still_path": next_ep.get("still_path"),
            }
            if next_ep["air_date"] >= today:
                upcoming_list.append(ep)
            else:
                newly_aired.append(ep)

    # Sort: upcoming by date ASC (soonest first), newly aired by date DESC (most recent first)
    upcoming_list.sort(key=lambda x: x["air_date"])
    newly_aired.sort(key=lambda x: x["air_date"], reverse=True)

    return render_template(
        'upcoming.html',
        upcoming=upcoming_list,
        newly_aired=newly_aired,
        today=today,
    )


# ═══════════════════════════════════════════════════════════════════
#  STATS DASHBOARD
# ═══════════════════════════════════════════════════════════════════
@app.route('/stats')
@login_required
def stats_dashboard():
    """Personal viewing stats dashboard."""
    user_id = session['user_id']

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()

            # ── Basic counts ──
            exe(cursor, 'SELECT COUNT(*) FROM watched_episodes WHERE user_id=? AND season_number != 0', (user_id,))
            total_episodes = cursor.fetchone()[0]

            exe(cursor, 'SELECT COUNT(*) FROM watched_movies WHERE user_id=?', (user_id,))
            total_movies = cursor.fetchone()[0]

            # ── Shows with activity ──
            exe(cursor, '''SELECT COUNT(DISTINCT show_tmdb_id) FROM watched_episodes
                           WHERE user_id=? AND season_number != 0''', (user_id,))
            shows_with_activity = cursor.fetchone()[0]

            exe(cursor, 'SELECT COUNT(*) FROM shows WHERE user_id=? AND status != \'Movie\'', (user_id,))
            total_tv_shows = cursor.fetchone()[0]

            # ── Completed shows (for completion rate) ──
            exe(cursor, '''SELECT COUNT(*) FROM shows s
                           WHERE s.user_id=? AND s.status != 'Movie'
                           AND (SELECT COUNT(*) FROM watched_episodes we
                                WHERE we.show_tmdb_id=s.tmdb_id AND we.user_id=s.user_id AND we.season_number != 0) >= s.total_episodes
                           AND s.total_episodes > 0''', (user_id,))
            completed_shows = cursor.fetchone()[0]

            # ── Monthly activity (last 12 months) ──
            exe(cursor, '''
                SELECT strftime('%Y-%m', watched_at) as month, COUNT(*) as cnt
                FROM watched_episodes
                WHERE user_id=? AND watched_at >= date('now', '-12 months') AND season_number != 0
                GROUP BY month
                ORDER BY month ASC
            ''', (user_id,))
            monthly_rows = cursor.fetchall()

            # ── User's shows with genres ──
            exe(cursor, '''
                SELECT tmdb_id, name FROM shows
                WHERE user_id=? AND status != 'Movie'
            ''', (user_id,))
            user_shows = cursor.fetchall()

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"STATS DB ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load stats.", "error")
        return redirect('/myshows')

    # ── Convert monthly rows to dicts for Jinja ──
    monthly_episodes = []
    month_max = 0
    for month, count in monthly_rows:
        monthly_episodes.append({"month": month, "count": count})
        if count > month_max:
            month_max = count
    monthly_episodes_max = month_max or 1

    # ── Fetch genres from TMDB for each show (cached) ──
    genre_counter = {}
    for tmdb_id, name in user_shows:
        show_data = tmdb_get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            {"api_key": API_KEY}
        )
        if show_data and show_data.get("genres"):
            for g in show_data["genres"]:
                gname = g["name"]
                genre_counter[gname] = genre_counter.get(gname, 0) + 1

    genre_data = sorted(genre_counter.items(), key=lambda x: x[1], reverse=True)[:10]
    max_genre = genre_data[0][1] if genre_data else 1

    # ── Completion rate ──
    completion_rate = round((completed_shows / total_tv_shows) * 100) if total_tv_shows > 0 else 0

    # ── Total hours (estimate: 22 min per TV episode, 120 min per movie) ──
    total_minutes = (total_episodes * 22) + (total_movies * 120)
    total_hours = round(total_minutes / 60)

    return render_template(
        'stats.html',
        total_episodes=total_episodes,
        total_movies=total_movies,
        total_hours=total_hours,
        total_tv_shows=total_tv_shows,
        completion_rate=completion_rate,
        genre_data=genre_data,
        max_genre=max_genre,
        monthly_episodes=monthly_episodes,
        monthly_episodes_max=monthly_episodes_max,
        shows_with_activity=shows_with_activity,
    )


if __name__ == "__main__":
    app.run(debug=True)
