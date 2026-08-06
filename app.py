from flask import Flask, jsonify, render_template, request, redirect, session, flash, url_for, abort
import requests
import os
import time
import secrets
import sys
import logging
import traceback
import threading
import hashlib
from datetime import date as _date, timedelta as _timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from pathlib import Path
from markupsafe import escape
from authlib.integrations.flask_client import OAuth

# Log to stdout so we can see them in Render logs.
# INFO level: request logging (method, path, status, duration) for monitoring.
logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
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
        print(f"  [INFO] Generated persistent key saved to {_SECRET_KEY_FILE}")

API_KEY = os.environ.get("TMDB_API_KEY")
DATABASE_PATH = get_db_path()

# ── Admin configuration ───────────────────────────────────────
# Comma-separated list of usernames allowed to open /admin (case-insensitive).
# If ADMIN_USERNAME is unset, the first registered user stays admin (legacy
# behavior) so existing deployments don't silently lose access.
ADMIN_USERNAMES = [
    u.strip().lower()
    for u in os.environ.get("ADMIN_USERNAME", "").split(",")
    if u.strip()
]

# ── Google OAuth Config ──────────────────────────────────
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTHLIB_INSECURE_TRANSPORT = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT", "0")

# Google OAuth is available if both env vars are set
google_oauth_available = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

if google_oauth_available:
    oauth = OAuth(app)
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid profile email'}
    )
    print("  [OK] Google OAuth configured!")
else:
    print("  [WARN] Google OAuth not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)")

# Security: ensure required secrets are configured
if not API_KEY:
    raise RuntimeError(
        "TMDB_API_KEY is required. "
        "Copy .env.example to .env and set TMDB_API_KEY to your TMDB API key."
    )

# Log all errors with full traceback to Render logs
@app.errorhandler(404)
def handle_404(error):
    return render_template('404.html'), 404


@app.errorhandler(500)
def handle_500(error):
    logger.error(f"500 ERROR: {error}")
    logger.error(traceback.format_exc())
    return render_template('500.html'), 500


@app.errorhandler(403)
def handle_403(error):
    return render_template('403.html'), 403


@app.errorhandler(400)
def handle_400(error):
    return render_template('400.html'), 400

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

# ── TMDB circuit breaker ────────────────────────────────────────────
# If TMDB is unreachable, pages would otherwise block for 3x10s timeouts
# per call (My Shows took 32s+ in testing). After N consecutive failures
# we fast-fail all TMDB calls for 60s so pages render instantly from
# cached/DB data, then automatically retry when TMDB recovers.
_tmdb_failures = 0
_tmdb_degraded_until = 0.0
_TMDB_DEGRADE_THRESHOLD = 3
_TMDB_DEGRADE_SECONDS = 60
_tmdb_lock = threading.Lock()  # counters are touched from parallel fetchers


def _mark_tmdb_failed():
    global _tmdb_failures, _tmdb_degraded_until
    with _tmdb_lock:
        _tmdb_failures += 1
        if _tmdb_failures >= _TMDB_DEGRADE_THRESHOLD:
            _tmdb_degraded_until = time.time() + _TMDB_DEGRADE_SECONDS
            _tmdb_failures = 0
            logger.warning("TMDB unreachable — degrading for %ds to keep pages fast", _TMDB_DEGRADE_SECONDS)


def _reset_tmdb_degraded():
    global _tmdb_failures, _tmdb_degraded_until
    with _tmdb_lock:
        _tmdb_failures = 0
        _tmdb_degraded_until = 0.0


def _cache_key(url, params):
    return f"{url}?{hash(frozenset(params.items()))}"


def tmdb_get(url, params, ttl=CACHE_TTL):
    """Fetch TMDB data with in-memory cache, retry/backoff for 429s, and a
    circuit breaker: if TMDB is unreachable we fast-fail for 60s instead of
    blocking pages on repeated 10s timeouts.

    🐛 The cache is checked FIRST so already-cached responses stay usable
    while the circuit is open (pages keep working from cache during an outage).
    """
    key = _cache_key(url, params)
    cached = _tmdb_cache.get(key)
    if cached and time.time() - cached["ts"] < ttl:
        return cached["data"]

    if time.time() < _tmdb_degraded_until:
        return None

    ok = False
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
            _reset_tmdb_degraded()  # any success closes the circuit
            ok = True
            return data
        except requests.exceptions.RequestException:
            if attempt == 2:
                break
            time.sleep(1)
    if not ok:
        _mark_tmdb_failed()
    return None


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # HSTS only when served over HTTPS in production
    if os.environ.get("FLASK_ENV") == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://image.tmdb.org; "
        "connect-src 'self' https://api.themoviedb.org; "
        "frame-ancestors 'self';"
    )
    return response


@app.before_request
def _record_request_start():
    request._start_time = time.time()


@app.after_request
def _log_request(response):
    """Request logging for monitoring: method, path, status, duration."""
    if request.path.startswith('/static'):
        return response
    duration_ms = (time.time() - getattr(request, '_start_time', time.time())) * 1000
    logger.info(
        "%s %s -> %s (%.0fms)",
        request.method, request.path, response.status_code, duration_ms
    )
    return response


def generate_csrf_token():
    """Generate or retrieve a CSRF token, refreshing only every 30 minutes.
    
    The 60-second refresh was too aggressive and caused form submissions
    (like the Remove button) to fail with 403 errors when a user spent
    more than a minute on a page before clicking.
    """
    if "_csrf_token" not in session or "_csrf_token_ts" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session["_csrf_token_ts"] = time.time()
    elif time.time() - session["_csrf_token_ts"] > 1800:  # 30 minutes
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session["_csrf_token_ts"] = time.time()
    return session["_csrf_token"]


def validate_csrf_token(token):
    return token and token == session.get("_csrf_token")


app.jinja_env.globals["csrf_token"] = generate_csrf_token

# ── Rate limiting (in-memory, per-IP sliding window) ────────────────
# Small, dependency-free limiter for abuse-prone endpoints.
# Note: in-memory means limits reset on restart — acceptable for this scale;
# a Redis-backed limiter would be the production upgrade path.
_rate_limit_hits = {}
RATE_LIMITS = {
    "search": (60, 40),   # 40 searches / minute
    "login": (60, 10),    # 10 login attempts / minute
    "signup": (60, 5),    # 5 signups / minute
    "reset": (60, 10),    # 10 password-reset submissions / minute
}


def rate_limited(bucket, methods=None):
    """Rate-limit a route per-IP.

    By default only POSTs are counted, so refreshing a GET form is never
    blocked; pass methods=('GET', 'POST') for read-heavy routes like search.
    """
    window, max_requests = RATE_LIMITS.get(bucket, (60, 30))
    if methods is None:
        methods = ('POST',)

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if request.method not in methods:
                return f(*args, **kwargs)
            ip = request.remote_addr or "unknown"
            key = f"{bucket}:{ip}"
            now = time.time()
            hits = [t for t in _rate_limit_hits.get(key, []) if now - t < window]
            if len(hits) >= max_requests:
                return jsonify({"status": "error", "message": "Too many requests. Please wait a moment and try again."}), 429
            hits.append(now)
            _rate_limit_hits[key] = hits
            # Opportunistic cleanup to avoid unbounded growth
            if len(_rate_limit_hits) > 2000:
                _rate_limit_hits.clear()
            return f(*args, **kwargs)
        return wrapper
    return decorator

# Initialize database on startup
print("=" * 50)
print("  Starting TV Tracker...")
print("=" * 50)
print(f"  Database: SQLite ({DATABASE_PATH})")
print("  Connecting...")
try:
    init_db(DATABASE_PATH)
    print("  [OK] Database connected and tables ready!")
except Exception as e:
    print(f"  [ERROR] Database init failed: {e}", file=sys.stderr)
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

    🐛 SAFETY FIX: the total is only ever returned when EVERY aired
    season loaded successfully. If a season fetch fails (timeout,
    429, circuit breaker), we return (0, None) instead of a partial
    sum — otherwise callers would overwrite the cached total_episodes
    with a wrong number, which made long shows' progress bars and
    totals jump around.
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not data:
        return 0, None

    total = 0
    for s in data.get("seasons", []):
        sn = s["season_number"]
        if sn <= 0:
            continue
        # 🐛 NOTE: do NOT trust the show endpoint's per-season episode_count
        # (it is stale for long-running shows — the exact bug this codebase
        # worked around). Always count from the season endpoint.
        season_data = tmdb_get(
            f"https://api.themoviedb.org/3/tv/{show_id}/season/{sn}",
            {"api_key": API_KEY},
            ttl=SEASON_CACHE_TTL,
        )
        if season_data is None:
            return 0, None  # incomplete sum — refuse to trust it
        total += len(season_data.get("episodes", []))

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


def _get_season_episode_tuples(show_id, season_number, user_id):
    """Fetch a season's episode list directly from TMDB for bulk marking.

    Returns (episode_tuples, fetch_failed):
      - episode_tuples: [(show_id, season_number, episode_number, user_id), ...]
      - fetch_failed: True when the TMDB fetch FAILED (timeout / 429 /
        circuit breaker). A season that simply has no episodes yet returns
        ([], False) — the two cases MUST NOT be conflated, or failed seasons
        would be silently skipped by the bulk-mark actions on long shows.
    """
    data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_number}",
        {"api_key": API_KEY},
    )
    if data is None:
        return [], True
    return [(show_id, season_number, ep["episode_number"], user_id) for ep in data.get("episodes", [])], False


def _update_cached_total_episodes(show_id, user_id):
    """Update the cached total_episodes in the shows table for a given show+user.
    Called after any bulk mark operation so progress bar stays accurate.
    Returns the total, or 0 if TMDB couldn't be reached.

    🐛 SAFETY FIX: the cached total is only ever updated with a COMPLETE,
    verified total (get_show_episode_count refuses partial sums) and never
    shrunk below the number of episodes the user has already marked — so
    the "total episodes" number stays stable for long shows instead of
    flip-flopping when a TMDB fetch is slow or partially fails.
    """
    total, _ = get_show_episode_count(show_id)
    if total <= 0:
        return 0
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''SELECT COUNT(*) FROM watched_episodes
                       WHERE show_tmdb_id=? AND user_id=? AND season_number != 0''',
            (show_id, user_id))
        watched_count = cursor.fetchone()[0]
        if total >= watched_count:
            exe(cursor, 'UPDATE shows SET total_episodes=? WHERE tmdb_id=? AND user_id=?',
                (total, show_id, user_id))
            conn.commit()
        return total
    finally:
        conn.close()


# ── Helper: build poster / backdrop URLs ────────────────────────────
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"


# ═══════════════════════════════════════════════════════════════════
#  HOME
# ═══════════════════════════════════════════════════════════════════
@app.route('/')
def home():
    """Netflix-style home page with category shelves.

    🐛 PERF FIX: all TMDB shelf fetches run in parallel (was 10 sequential
    calls — up to several minutes when TMDB rate-limits us).
    """
    def _fetch(url, params):
        return tmdb_get(url, params)

    tasks = [
        ("trending_tv", "https://api.themoviedb.org/3/trending/tv/week", {"api_key": API_KEY}),
        ("trending_movies", "https://api.themoviedb.org/3/trending/movie/week", {"api_key": API_KEY}),
        ("popular_tv", "https://api.themoviedb.org/3/tv/popular", {"api_key": API_KEY}),
        ("top_rated", "https://api.themoviedb.org/3/tv/top_rated", {"api_key": API_KEY}),
        ("popular_movies", "https://api.themoviedb.org/3/movie/popular", {"api_key": API_KEY}),
    ]
    # Genre shelves (TV genres)
    genre_ids = {
        "Action & Adventure": 10759,
        "Comedy": 35,
        "Sci-Fi & Fantasy": 10765,
        "Drama": 18,
        "Mystery": 9648,
    }
    for label, genre_id in genre_ids.items():
        tasks.append((
            f"genre_{genre_id}",
            "https://api.themoviedb.org/3/discover/tv",
            {"api_key": API_KEY, "with_genres": genre_id, "sort_by": "popularity.desc", "page": 1},
        ))

    fetched = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        future_map = {pool.submit(_fetch, url, params): name for name, url, params in tasks}
        for fut in future_map:
            try:
                fetched[future_map[fut]] = fut.result()
            except Exception:
                fetched[future_map[fut]] = None

    genre_shelves = []
    for label, genre_id in genre_ids.items():
        data = fetched.get(f"genre_{genre_id}")
        if data and data.get("results"):
            genre_shelves.append({
                "label": label,
                "results": data["results"][:20]
            })

    # Continue Watching shelf — only for logged-in users
    continue_watching = []
    if session.get('user_id'):
        continue_watching = get_continue_watching(session['user_id'])

    return render_template(
        'search.html',
        trending_tv=(fetched.get("trending_tv") or {}).get("results", [])[:12],
        trending_movies=(fetched.get("trending_movies") or {}).get("results", [])[:12],
        popular_tv=(fetched.get("popular_tv") or {}).get("results", [])[:12],
        top_rated=(fetched.get("top_rated") or {}).get("results", [])[:12],
        popular_movies=(fetched.get("popular_movies") or {}).get("results", [])[:12],
        genre_shelves=genre_shelves,
        continue_watching=continue_watching,
    )


def get_continue_watching(user_id, limit=12):
    """In-progress shows for the Continue Watching shelf (home page).

    Semantics:
      - Only shows with at least one watched episode (watched_count > 0)
      - Excluded once COMPLETED (watched >= total) → "removed from Continue
        Watching" the moment a show hits 100%
      - Excluded if the user dropped it or only has it on their plan list
      - Includes the last watched (season, episode) so "Resume" can deep-link
        straight to the season page where they left off
    Sorted by most recently watched first. Uses DB-cached totals (updated on
    every mark operation and by the admin repair tool) — no TMDB calls.
    """
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, '''
            SELECT show_tmdb_id, COUNT(*) FROM watched_episodes
            WHERE user_id=? AND season_number != 0
            GROUP BY show_tmdb_id
        ''', (user_id,))
        watched_map = dict(cursor.fetchall())
        if not watched_map:
            return []

        exe(cursor, '''
            SELECT tmdb_id, name, poster_path, total_episodes,
                   COALESCE(user_status, ''), COALESCE(last_watched_at, '')
            FROM shows WHERE user_id=? AND status != 'Movie'
        ''', (user_id,))
        rows = cursor.fetchall()

        # Latest watched (season, episode) per show → resume target.
        # 🐛 PERF FIX: ONE window-function query instead of N per-show queries.
        exe(cursor, '''
            SELECT show_tmdb_id, season_number, episode_number FROM (
                SELECT show_tmdb_id, season_number, episode_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY show_tmdb_id
                           ORDER BY season_number DESC, episode_number DESC
                       ) AS rn
                FROM watched_episodes
                WHERE user_id=? AND season_number != 0
            ) WHERE rn = 1
        ''', (user_id,))
        resume = {row[0]: {"season": row[1], "episode": row[2]} for row in cursor.fetchall()}
    finally:
        conn.close()

    items = []
    for tmdb_id, name, poster, total, ustatus, last_watched in rows:
        watched = watched_map.get(tmdb_id, 0)
        if watched <= 0:
            continue
        if ustatus in ('dropped', 'plan_to_watch'):
            continue
        # Completed → remove from Continue Watching
        if total and total > 0 and watched >= total:
            continue
        items.append({
            "tmdb_id": tmdb_id,
            "name": name,
            "poster_path": poster,
            "watched_count": watched,
            "total_episodes": total or 0,
            "last_watched_at": last_watched,
            "resume": resume.get(tmdb_id),
        })

    items.sort(key=lambda x: x["last_watched_at"], reverse=True)
    return items[:limit]


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
    uptime_secs = int(time.time() - _START_TIME)
    return jsonify({
        "status": "ok",
        "database": db_status,
        "uptime_seconds": uptime_secs,
        "uptime_human": f"{uptime_secs // 3600}h {(uptime_secs % 3600) // 60}m",
    }), 200


# ═══════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════
@app.route('/signup', methods=['GET', 'POST'])
@rate_limited('signup')
def signup():
    if request.method == 'GET':
        return render_template('signup.html')

    # CSRF: every state-changing POST must carry a valid token
    if not validate_csrf_token(request.form.get('_csrf_token')):
        flash("Session expired. Please refresh the page and try again.", "error")
        return render_template('signup.html', username=request.form.get('username', '').strip())

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
                'INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
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
@rate_limited('login')
def login():
    if request.method == 'POST':
        # CSRF: every state-changing POST must carry a valid token
        if not validate_csrf_token(request.form.get('_csrf_token')):
            flash("Session expired. Please refresh the page and try again.", "error")
            return redirect('/login')

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
#  SEARCH  (pagination + rate limited)
# ═══════════════════════════════════════════════════════════════════
@app.route('/search')
@rate_limited('search', methods=('GET', 'POST'))
def search():
    query = request.args.get('query', '').strip()
    search_type = request.args.get('type', 'tv')

    # Validate + clamp page number (pagination)
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except (TypeError, ValueError):
        page = 1

    if not query:
        return redirect('/')

    url = f"https://api.themoviedb.org/3/search/{search_type}"
    params = {"api_key": API_KEY, "query": query, "page": page}
    data = tmdb_get(url, params)

    if data is None:
        flash("Couldn't reach TMDB. Check your connection and try again.", "error")
        return redirect('/')

    results = data.get("results", [])
    total_results = data.get("total_results", len(results))
    total_pages = data.get("total_pages", page)
    return render_template(
        'search_results.html',
        results=results,
        search_type=search_type,
        query=query,
        page=page,
        total_results=total_results,
        has_next=page < total_pages,
    )


# ═══════════════════════════════════════════════════════════════════
#  ADD SHOW / MOVIE  (CSRF-safe POST — was GET, which allowed CSRF abuse)
# ═══════════════════════════════════════════════════════════════════
@app.route('/add/<int:show_id>', methods=['POST'])
@login_required
def add_show(show_id):
    if not validate_csrf_token(request.form.get('_csrf_token')):
        return jsonify({"status": "error", "message": "Session expired. Please refresh and try again."}), 403

    url = f"https://api.themoviedb.org/3/tv/{show_id}"
    params = {"api_key": API_KEY}
    show = tmdb_get(url, params)

    if show is None:
        return jsonify({"status": "error", "message": "Couldn't fetch show details."}), 502

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (show["id"], session['user_id']))
            if cursor.fetchone():
                return jsonify({"status": "info", "message": f"{escape(show['name'])} is already in your shows."})

            # Fetch episode count gracefully — if it fails (timeout, rate limit),
            # still add the show and let My Shows refresh it later.
            total_ep = 0
            try:
                total_ep, _ = get_show_episode_count(show["id"])
            except Exception as ep_err:
                logger.error(f"Could not fetch episode count for {show['id']}: {ep_err}")

            # Capture runtime / rating / genres at add time so the stats page
            # can compute watch time + ratings WITHOUT per-show TMDB calls.
            runtimes = show.get("episode_run_time") or []
            runtime = runtimes[0] if runtimes else 0
            rating = float(show.get("vote_average") or 0)
            genres = ", ".join(g["name"] for g in (show.get("genres") or []))

            exe(cursor, '''
                INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id, total_episodes, runtime, rating, genres)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (show["id"], show["name"], show["poster_path"], show.get("status", ""), show.get("first_air_date", ""), session['user_id'], total_ep, runtime, rating, genres))
            conn.commit()
            return jsonify({"status": "ok", "message": f"Added {escape(show['name'])}!"})
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"ADD SHOW ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Could not add show. Please try again."}), 500


@app.route('/add_movie/<int:movie_id>', methods=['POST'])
@login_required
def add_movie(movie_id):
    if not validate_csrf_token(request.form.get('_csrf_token')):
        return jsonify({"status": "error", "message": "Session expired. Please refresh and try again."}), 403

    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": API_KEY}
    movie = tmdb_get(url, params)

    if movie is None:
        return jsonify({"status": "error", "message": "Couldn't fetch movie details."}), 502

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT tmdb_id FROM shows WHERE tmdb_id=? AND user_id=?', (movie["id"], session['user_id']))
            if cursor.fetchone():
                return jsonify({"status": "info", "message": f"{escape(movie['title'])} is already in your movies."})

            runtime = int(movie.get("runtime") or 0)
            rating = float(movie.get("vote_average") or 0)
            genres = ", ".join(g["name"] for g in (movie.get("genres") or []))

            exe(cursor, '''
                INSERT INTO shows (tmdb_id, name, poster_path, status, first_air_date, user_id, runtime, rating, genres)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (movie["id"], movie["title"], movie["poster_path"], "Movie", movie.get("release_date", ""), session['user_id'], runtime, rating, genres))
            conn.commit()
            return jsonify({"status": "ok", "message": f"Added {escape(movie['title'])} to My Movies!"})
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"ADD MOVIE ERROR ({movie_id}): {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Could not add movie. Please try again."}), 500


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

            # 🐛 PERF FIX: fetch ALL watched counts in ONE grouped query (was N+1)
            exe(cursor, '''
                SELECT show_tmdb_id, COUNT(*) FROM watched_episodes
                WHERE user_id=? AND season_number != 0
                GROUP BY show_tmdb_id
            ''', (session['user_id'],))
            watched_map = {row[0]: row[1] for row in cursor.fetchall()}

            shows_with_progress = []
            for show_row in shows:
                show_id = show_row[0]
                watched_count = watched_map.get(show_id, 0)

                # Get cached total_episodes + stored rating from the DB (rating is
                # captured at add time; totals update after every mark operation).
                # 🐛 PERF FIX: no per-show TMDB call — the TMDB status is already
                # stored in the status column, so only ONGOING shows hit TMDB
                # (and only to refresh their episode totals).
                exe(cursor, 'SELECT total_episodes, COALESCE(rating,0) FROM shows WHERE tmdb_id=? AND user_id=?',
                    (show_id, session['user_id']))
                row = cursor.fetchone()
                total_episodes = row[0] if row else 0
                rating = row[1] if row else 0
                tmdb_status = show_row[3]
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

            # Persist any total_episodes updates for ongoing shows
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
#  MOVIE DETAIL (dedicated route — prevents TV/movie ID collision)
# ═══════════════════════════════════════════════════════════════════
@app.route('/movie/<int:movie_id>')
@login_required
def movie_detail(movie_id):
    """Dedicated movie detail page. TMDB IDs are not globally unique
    across movies and TV shows, so we MUST use a separate route to
    avoid serving a TV show's page when a movie was clicked.
    """
    try:
        movie_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
        movie_data = tmdb_get(movie_url, {"api_key": API_KEY, "append_to_response": "recommendations,similar,videos"})

        if not movie_data or not movie_data.get("title"):
            flash("Couldn't load movie details.", "error")
            return redirect('/mymovies')

        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT movie_tmdb_id FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (movie_id, session['user_id']))
            is_watched = cursor.fetchone() is not None
        finally:
            conn.close()

        providers = tmdb_get(
            f"https://api.themoviedb.org/3/movie/{movie_id}/watch/providers",
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
    except Exception as e:
        logger.error(f"MOVIE DETAIL ERROR ({movie_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Couldn't load movie details.", "error")
        return redirect('/mymovies')


# ═══════════════════════════════════════════════════════════════════
@app.route('/remove/<int:show_id>', methods=['POST'])
@login_required
def remove_show(show_id):
    if not validate_csrf_token(request.form.get('_csrf_token')):
        flash("Session expired. Please try again.", "error")
        return redirect(request.referrer or '/myshows')
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            # 🐛 FIX: Delete child rows FIRST to avoid FOREIGN KEY constraint failures
            # watched_episodes references shows via show_tmdb_id
            exe(cursor, 'DELETE FROM watched_episodes WHERE show_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            # watched_movies is a separate table but same pattern
            exe(cursor, 'DELETE FROM watched_movies WHERE movie_tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            # Delete parent row LAST
            exe(cursor, 'DELETE FROM shows WHERE tmdb_id=? AND user_id=?', (show_id, session['user_id']))
            conn.commit()
        except Exception as db_err:
            logger.error(f"REMOVE DB ERROR ({show_id}): {db_err}")
            logger.error(traceback.format_exc())
            flash(f"Could not remove item. Please try again.", "error")
            return redirect(request.referrer or '/myshows')
        finally:
            try:
                conn.close()
            except:
                pass
    except Exception as e:
        logger.error(f"REMOVE CONNECTION ERROR ({show_id}): {e}")
        logger.error(traceback.format_exc())
        flash("Could not connect to database. Try again.", "error")
        return redirect(request.referrer or '/myshows')
    flash("Item removed successfully.", "success")
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
#  API: SEASON TOTALS (accurate per-season episode counts)
#  Used by show_detail.html to render green checkmarks correctly.
#  ⚠️  Uses SEASON endpoint (accurate) not show endpoint (stale).
# ═══════════════════════════════════════════════════════════════════
@app.route('/api/show/<int:show_id>/season-totals')
@login_required
def api_season_totals(show_id):
    """Return accurate per-season episode counts from SEASON endpoints.
    This avoids the show endpoint's stale episode_count, which can be
    wrong for ongoing shows (e.g. One Piece). The show_detail.html
    inline tracker uses this to render green checkmarks correctly.

    Returns: { "season_number": episode_count, ... }
    """
    show_data = tmdb_get(
        f"https://api.themoviedb.org/3/tv/{show_id}",
        {"api_key": API_KEY}
    )
    if not show_data:
        return jsonify({"error": "Could not fetch show data"}), 404

    totals = {}
    for season in show_data.get("seasons", []):
        sn = season["season_number"]
        if sn <= 0:
            continue
        count = get_season_episode_count(show_id, sn)
        if count > 0:
            totals[str(sn)] = count

    return jsonify(totals)


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


# ── App start time (for health/uptime reporting) ─────────────────────
_START_TIME = time.time()


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
#  MARK WATCHED (episode toggle) — with auto-catch-up
# ═══════════════════════════════════════════════════════════════════
@app.route('/watch/<int:show_id>/<int:season_number>/<int:episode_number>', methods=['POST'])
@login_required
def mark_watched(show_id, season_number, episode_number):
    """
    Toggle a single episode watched/unwatched.

    🔄 AUTO-CATCH-UP: "watched up to episode N" semantics. Marking episode N
    means every episode BEFORE it was watched, so this endpoint automatically
    marks:
      - All EARLIER episodes in the SAME season (1..N-1)
      - All episodes in ALL PRIOR seasons

    The backfill is idempotent (INSERT OR IGNORE) and prior seasons are only
    re-synced when they aren't provably complete already, so long shows don't
    re-fetch ~20 seasons on every click. If a TMDB fetch fails mid-catch-up,
    auto_caught_up is reported as False so the UI never claims a false
    "Caught up!". Both the season_detail page and the show_detail inline
    tracker benefit from this server-side logic.
    """
    token = request.form.get('_csrf_token')
    if not validate_csrf_token(token):
        abort(403)
    # Input validation: seasons/episodes are 1-indexed
    if season_number <= 0 or episode_number <= 0:
        abort(400)

    user_id = session['user_id']
    auto_caught_up = False
    auto_marked_count = 0

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
                total_in_season = get_season_episode_count(show_id, season_number)
            else:
                exe(cursor, '''
                    INSERT INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                    VALUES (?, ?, ?, ?)
                ''', (show_id, season_number, episode_number, user_id))
                status = "watched"

                # ── Auto-catch-up: "watched up to episode N" semantics ──
                # Marking episode N means every episode BEFORE it was watched:
                #   1. All earlier episodes in THIS season (1..N-1) — no TMDB needed.
                #   2. All episodes in ALL PRIOR seasons — so hopping into a later
                #      season backfills the history correctly.
                # 🐛 FIX: the old code only backfilled when the LAST episode of a
                # season was marked, so marking a mid-season episode left earlier
                # episodes permanently unwatched. It also silently skipped prior
                # seasons whenever a TMDB fetch failed mid-loop (very likely for
                # long shows: after 3 failures the circuit breaker makes the
                # remaining ~20 season fetches return nothing), yet still showed
                # a false "Caught up!" toast. Now the backfill always runs, prior
                # seasons are skipped only when provably already complete, and
                # auto_caught_up is only reported when every prior aired season
                # was actually synced.
                total_in_season = get_season_episode_count(show_id, season_number)
                # 🐛 SAFETY: don't backfill an out-of-range episode number when the
                # season's true length is known (guards against cascading phantom
                # rows from a direct request like /watch/<id>/1/9999). When TMDB
                # is unreachable (0) we still backfill best-effort so marking stays
                # useful during outages.
                if total_in_season == 0 or episode_number <= total_in_season:
                    earlier = [(show_id, season_number, ep, user_id) for ep in range(1, episode_number)]
                    if earlier:
                        exemany(cursor, '''
                            INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                            VALUES (?, ?, ?, ?)
                        ''', earlier)
                        auto_marked_count += max(cursor.rowcount, 0)

                show_data = tmdb_get(
                    f"https://api.themoviedb.org/3/tv/{show_id}",
                    {"api_key": API_KEY}
                )
                prior_seasons_ok = True
                prior_new_count = 0
                if show_data:
                    # All prior seasons (sn < current). NOTE: don't filter on the
                    # show endpoint's episode_count — it's stale for long-running
                    # shows, and a season below the current one is aired anyway.
                    # No "prev season complete => all complete" shortcut: a
                    # partially-failed catch-up could leave an EARLIER season with
                    # gaps while the immediately-previous one is complete, and a
                    # shortcut would skip fixing it forever. Re-syncing is cheap
                    # (cached TMDB data + INSERT OR IGNORE) and self-healing.
                    for season in show_data.get("seasons", []):
                        sn = season["season_number"]
                        if not (0 < sn < season_number):
                            continue
                        # Reuse the shared helper: distinguishes a FAILED fetch
                        # (report honestly) from a season with no episodes yet.
                        prior_episodes, fetch_failed = _get_season_episode_tuples(show_id, sn, user_id)
                        if fetch_failed:
                            # Fetch failed (timeout / 429 / circuit breaker).
                            # Never silently skip: report that catch-up was NOT
                            # complete so the UI can tell the truth.
                            prior_seasons_ok = False
                            continue
                        if prior_episodes:
                            exemany(cursor, '''
                                INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                                VALUES (?, ?, ?, ?)
                            ''', prior_episodes)
                            inserted = max(cursor.rowcount, 0)
                            prior_new_count += inserted
                            auto_marked_count += inserted

                    # Only claim "caught up" when every prior season was synced
                    # AND at least one new row was actually added this request.
                    if prior_seasons_ok and prior_new_count > 0:
                        auto_caught_up = True

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

        # Update last_watched_at so show jumps to top of My Shows
        _update_show_timestamp(show_id, user_id)

        finished = (watched_count == total_episodes and total_episodes > 0)

        is_last_episode = (episode_number == total_in_season and total_in_season > 0)

        return jsonify({
            "status": status,
            "finished": finished,
            "is_last_episode": is_last_episode,
            "auto_caught_up": auto_caught_up,
            "auto_marked_count": auto_marked_count,
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
            failed_seasons = []
            for season in show_data.get("seasons", []):
                sn = season["season_number"]
                if not (0 < sn < season_number):
                    continue
                # 🐛 FIX: fetch the season directly so a FAILED fetch is
                # distinguishable from a season with no episodes yet. The old
                # code silently skipped failed seasons — very likely for long
                # shows, where the circuit breaker trips mid-loop and the
                # remaining season fetches all return nothing.
                tuples, fetch_failed = _get_season_episode_tuples(show_id, sn, user_id)
                if fetch_failed:
                    failed_seasons.append(sn)
                    continue
                episodes.extend(tuples)
            marked_count = 0
            if episodes:
                exemany(cursor, '''
                    INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                    VALUES (?, ?, ?, ?)
                ''', episodes)
                marked_count = max(cursor.rowcount, 0)
            conn.commit()
        finally:
            conn.close()

        if failed_seasons:
            logger.warning(f"MARK PREVIOUS SEASONS incomplete ({show_id}): seasons {failed_seasons} could not be fetched")

        # 🐛 FIX: Update cached total_episodes after bulk mark
        _update_cached_total_episodes(show_id, user_id)
        _update_show_timestamp(show_id, user_id)

        return jsonify({
            "status": "ok",
            "marked_previous_seasons_up_to": season_number - 1,
            "marked_count": marked_count,
            "failed_seasons": failed_seasons,
            "incomplete": bool(failed_seasons),
        })
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
            failed_seasons = []
            for season in show_data.get("seasons", []):
                sn = season["season_number"]
                if sn <= 0:
                    continue
                # 🐛 FIX: fetch the season directly so a FAILED fetch is
                # distinguishable from a season with no episodes yet (e.g. an
                # upcoming season). The old code silently skipped failed
                # seasons — very likely for long shows, where the circuit
                # breaker trips mid-loop and the remaining fetches return
                # nothing, leaving the user with a false "all marked".
                tuples, fetch_failed = _get_season_episode_tuples(show_id, sn, user_id)
                if fetch_failed:
                    failed_seasons.append(sn)
                    continue
                episodes.extend(tuples)
            marked_count = 0
            if episodes:
                exemany(cursor, '''
                    INSERT OR IGNORE INTO watched_episodes (show_tmdb_id, season_number, episode_number, user_id)
                    VALUES (?, ?, ?, ?)
                ''', episodes)
                marked_count = max(cursor.rowcount, 0)
            conn.commit()
        finally:
            conn.close()

        if failed_seasons:
            logger.warning(f"MARK ALL SEASONS WATCHED incomplete ({show_id}): seasons {failed_seasons} could not be fetched")

        # 🐛 FIX: Update cached total_episodes after bulk mark
        total = _update_cached_total_episodes(show_id, user_id)
        if total == 0:
            total, _ = get_show_episode_count(show_id)
        _update_show_timestamp(show_id, user_id)

        return jsonify({
            "status": "ok",
            "marked_count": marked_count,
            "failed_seasons": failed_seasons,
            "incomplete": bool(failed_seasons),
            # Don't claim the show is finished if some seasons couldn't sync.
            "finished": total > 0 and not failed_seasons,
        })
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
        # Update timestamp so movie jumps to top of My Movies
        _update_show_timestamp(movie_id, user_id)
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

            # All user's shows (TV only, not movies) — include cached totals
            exe(cursor, '''
                SELECT tmdb_id, name, poster_path, status, first_air_date, total_episodes
                FROM shows WHERE user_id=? AND status != 'Movie'
            ''', (user_id,))
            all_shows = cursor.fetchall()

            # 🐛 PERF FIX: per-show counts + last-watched in ONE grouped query (was N+1)
            exe(cursor, '''
                SELECT show_tmdb_id, COUNT(*), MAX(watched_at)
                FROM watched_episodes
                WHERE user_id=? AND season_number != 0
                GROUP BY show_tmdb_id
            ''', (user_id,))
            show_stats = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

            # Completed shows — uses DB-cached total_episodes (updated on every
            # mark operation + admin repair). No TMDB calls per show here.
            completed_shows = []
            for row in all_shows:
                show_id = row[0]
                total_episodes = row[5] or 0
                watched_count, completed_at = show_stats.get(show_id, (0, None))
                if total_episodes > 0 and watched_count >= total_episodes:
                    completed_shows.append(row[:5] + (completed_at, 0))

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
    """Check if the given user_id is an admin.

    Admins are the usernames listed in the ADMIN_USERNAME env var
    (comma-separated, case-insensitive). If that var is unset, fall back to
    the legacy rule: the first registered user is the admin.
    """
    conn = get_conn()
    try:
        cursor = conn.cursor()
        exe(cursor, 'SELECT id, username FROM users WHERE id=?', (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        uid, uname = row[0], (row[1] or '').strip().lower()
        if ADMIN_USERNAMES:
            return uname in ADMIN_USERNAMES
        exe(cursor, 'SELECT id FROM users ORDER BY id ASC LIMIT 1')
        first_user = cursor.fetchone()
        return bool(first_user) and first_user[0] == uid
    finally:
        conn.close()


@app.route('/admin')
@login_required
def admin_dashboard():
    """Simple admin page — shows user stats. Only the first user (you) can access."""
    user_id = session['user_id']

    try:
        # Only admins (ADMIN_USERNAME env, or legacy first user) can see this
        if not _is_admin(user_id):
            flash("Admin access restricted — this account isn't in the admin list.", "error")
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
#  PASSWORD RESET (admin-driven — no email service required)
# ═══════════════════════════════════════════════════════════════════
RESET_TOKEN_TTL_SECONDS = 24 * 3600  # reset links are valid for 24 hours


def _hash_reset_token(token):
    """Store tokens as SHA-256 hashes so a DB leak doesn't expose live links."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.route('/admin/reset_password', methods=['POST'])
@login_required
def admin_reset_password():
    """Admin-only recovery tool: generate a one-time reset link for a user,
    or set a temporary password directly. No email service needed — the
    admin shares the link/password with the user."""
    if not _is_admin(session['user_id']):
        return jsonify({"status": "error", "message": "Admin access restricted."}), 403
    if not validate_csrf_token(request.form.get('_csrf_token')):
        return jsonify({"status": "error", "message": "Invalid CSRF token."}), 403

    username = request.form.get('username', '').strip()
    action = request.form.get('action', '')
    if not username:
        return jsonify({"status": "error", "message": "Username is required."}), 400

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            exe(cursor, 'SELECT id FROM users WHERE username=?', (username,))
            user = cursor.fetchone()
            if not user:
                return jsonify({"status": "error", "message": f"User '{escape(username)}' not found."}), 404
            uid = user[0]

            if action == 'link':
                # One-time reset link, valid 24h; store only the token hash
                token = secrets.token_urlsafe(32)
                expires_utc = time.strftime(
                    '%Y-%m-%d %H:%M:%S',
                    time.gmtime(time.time() + RESET_TOKEN_TTL_SECONDS)
                )
                exe(cursor, '''
                    UPDATE users SET password_reset_token=?, password_reset_expires=?
                    WHERE id=?
                ''', (_hash_reset_token(token), expires_utc, uid))
                conn.commit()
                reset_link = url_for('reset_password_page', token=token, _external=True)
                return jsonify({
                    "status": "ok",
                    "message": f"Reset link generated for {escape(username)} (valid 24h).",
                    "reset_link": reset_link,
                })

            if action == 'password':
                new_password = request.form.get('password', '')
                if len(new_password) < 6:
                    return jsonify({"status": "error", "message": "Temporary password must be at least 6 characters."}), 400
                exe(cursor, '''
                    UPDATE users SET password_hash=?, password_reset_token=NULL, password_reset_expires=NULL
                    WHERE id=?
                ''', (generate_password_hash(new_password), uid))
                conn.commit()
                return jsonify({"status": "ok", "message": f"Password for {escape(username)} has been reset."})

            return jsonify({"status": "error", "message": "Invalid action."}), 400
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"ADMIN RESET ERROR: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "error", "message": "Could not reset password."}), 500


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
@rate_limited('reset')
def reset_password_page(token):
    """One-time password reset page reached via the admin-generated link.
    The token is single-use and expires after 24 hours."""
    token_hash = _hash_reset_token(token)
    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            # datetime('now') is UTC — matches the UTC expiry we stored
            exe(cursor, '''SELECT id FROM users
                           WHERE password_reset_token=? AND password_reset_expires IS NOT NULL
                             AND password_reset_expires > datetime('now')''', (token_hash,))
            row = cursor.fetchone()
            valid = row is not None
            user_id = row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"RESET LOOKUP ERROR: {e}")
        logger.error(traceback.format_exc())
        return render_template('reset_password.html', valid=False, token='')

    if request.method == 'GET':
        return render_template('reset_password.html', valid=valid, token=token if valid else '')

    # POST: set the new password
    if not valid:
        flash("This reset link is invalid or has expired. Ask an admin for a new one.", "error")
        return redirect('/login')
    if not validate_csrf_token(request.form.get('_csrf_token')):
        flash("Session expired. Please refresh the page and try again.", "error")
        return render_template('reset_password.html', valid=True, token=token)

    new_password = request.form.get('password', '')
    confirm = request.form.get('password_confirm', '')
    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return render_template('reset_password.html', valid=True, token=token)
    if new_password != confirm:
        flash("Passwords do not match.", "error")
        return render_template('reset_password.html', valid=True, token=token)

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()
            # Single-use: clear the token so it cannot be replayed
            exe(cursor, '''
                UPDATE users SET password_hash=?, password_reset_token=NULL, password_reset_expires=NULL
                WHERE id=?
            ''', (generate_password_hash(new_password), user_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"RESET UPDATE ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not update your password. Please try again.", "error")
        return render_template('reset_password.html', valid=True, token=token)

    flash("Password updated! Log in with your new password.", "success")
    return redirect('/login')


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

    # 🐛 PERF FIX: fetch all tracked shows' data in parallel (was N sequential calls)
    def _fetch_show(tmdb_id):
        return tmdb_get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            {"api_key": API_KEY}
        )

    shows_data = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(_fetch_show, tmdb_id): tmdb_id for tmdb_id, _n, _p in shows}
        for fut in future_map:
            try:
                shows_data[future_map[fut]] = fut.result()
            except Exception:
                shows_data[future_map[fut]] = None

    for tmdb_id, name, poster_path in shows:
        show_data = shows_data.get(tmdb_id)
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
#  STATS / PROFILE DASHBOARD  (Part 2: redesigned, accurate metrics)
# ═══════════════════════════════════════════════════════════════════
@app.route('/stats')
@login_required
def stats_dashboard():
    """Personal profile & statistics dashboard.

    Every metric is computed from stored data (no guessing):
      - Episodes/Movies watched: exact row counts
      - Series completed: watched >= cached total_episodes (updated on every
        mark operation and by the admin repair tool)
      - Watch time: per-title runtime captured at add time; falls back to
        22 min/episode and 120 min/movie for legacy rows without runtime
      - Ratings/genres: TMDB values captured at add time
      - Streaks / heatmap / monthly activity: from watched_at timestamps
    """
    user_id = session['user_id']
    today = time.strftime('%Y-%m-%d')

    try:
        conn = get_conn()
        try:
            cursor = conn.cursor()

            # ── Exact counts ──
            exe(cursor, 'SELECT COUNT(*) FROM watched_episodes WHERE user_id=? AND season_number != 0', (user_id,))
            total_episodes = cursor.fetchone()[0]
            exe(cursor, 'SELECT COUNT(*) FROM watched_movies WHERE user_id=?', (user_id,))
            total_movies = cursor.fetchone()[0]
            exe(cursor, '''SELECT COUNT(DISTINCT show_tmdb_id) FROM watched_episodes
                           WHERE user_id=? AND season_number != 0''', (user_id,))
            shows_with_activity = cursor.fetchone()[0]

            # ── Per-show watched counts + last watched (one grouped query) ──
            exe(cursor, '''SELECT show_tmdb_id, COUNT(*), MAX(watched_at) FROM watched_episodes
                           WHERE user_id=? AND season_number != 0 GROUP BY show_tmdb_id''', (user_id,))
            show_stats = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

            # ── User's tracked shows (TV) with stored metadata ──
            exe(cursor, '''SELECT tmdb_id, total_episodes, runtime, rating, genres
                           FROM shows WHERE user_id=? AND status != 'Movie' ''', (user_id,))
            user_shows = cursor.fetchall()

            # ── Watched movies with stored metadata ──
            exe(cursor, '''SELECT wm.movie_tmdb_id, COALESCE(s.name,'Unknown'),
                                  COALESCE(s.runtime,0), COALESCE(s.rating,0),
                                  COALESCE(s.genres,'')
                           FROM watched_movies wm
                           LEFT JOIN shows s ON wm.movie_tmdb_id = s.tmdb_id AND wm.user_id = s.user_id
                           WHERE wm.user_id=?''', (user_id,))
            watched_movies = cursor.fetchall()

            # ── Monthly activity (episodes + movies, last 12 months) ──
            monthly = {}
            exe(cursor, '''SELECT strftime('%Y-%m', watched_at), COUNT(*) FROM watched_episodes
                           WHERE user_id=? AND watched_at >= date('now','-12 months') AND season_number != 0
                           GROUP BY 1''', (user_id,))
            for m, c in cursor.fetchall():
                monthly[m] = c
            exe(cursor, '''SELECT strftime('%Y-%m', watched_at), COUNT(*) FROM watched_movies
                           WHERE user_id=? AND watched_at >= date('now','-12 months')
                           GROUP BY 1''', (user_id,))
            for m, c in cursor.fetchall():
                monthly[m] = monthly.get(m, 0) + c

            # ── Member since ──
            exe(cursor, 'SELECT created_at FROM users WHERE id=?', (user_id,))
            created_row = cursor.fetchone()
            created_at = (created_row[0] if created_row else None) or ''

            # ── All activity dates (streaks + heatmap) ──
            ep_dates = []
            mv_dates = []
            exe(cursor, '''SELECT date(watched_at) FROM watched_episodes
                           WHERE user_id=? AND season_number != 0 AND watched_at IS NOT NULL''', (user_id,))
            for (d,) in cursor.fetchall():
                if d:
                    ep_dates.append(d)
            exe(cursor, '''SELECT date(watched_at) FROM watched_movies
                           WHERE user_id=? AND watched_at IS NOT NULL''', (user_id,))
            for (d,) in cursor.fetchall():
                if d:
                    mv_dates.append(d)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"STATS DB ERROR: {e}")
        logger.error(traceback.format_exc())
        flash("Could not load stats.", "error")
        return redirect('/myshows')

    # ── Series completed (vs cached totals — consistent with My Shows) ──
    completed_count = 0
    total_tv_shows = 0
    for tmdb_id, total_ep, runtime, rating, genres in user_shows:
        if (total_ep or 0) > 0:
            total_tv_shows += 1
            if show_stats.get(tmdb_id, (0, None))[0] >= total_ep:
                completed_count += 1
    completion_rate = round((completed_count / total_tv_shows) * 100) if total_tv_shows > 0 else 0

    # ── Watch time (accurate per-title runtime when known) ──
    total_minutes = 0
    for tmdb_id, total_ep, runtime, rating, genres in user_shows:
        watched = show_stats.get(tmdb_id, (0, None))[0]
        if watched > 0:
            total_minutes += watched * (runtime or 22)
    for _movie_id, _name, mrun, _mrating, _mgenres in watched_movies:
        total_minutes += (mrun or 120)
    total_hours = round(total_minutes / 60)

    # ── Ratings (of WATCHED items, stored vote_average) ──
    rated_items = []
    for tmdb_id, total_ep, runtime, rating, genres in user_shows:
        if show_stats.get(tmdb_id, (0, None))[0] > 0 and (rating or 0) > 0:
            rated_items.append(float(rating))
    for _movie_id, _name, mrun, mrating, _mgenres in watched_movies:
        if (mrating or 0) > 0:
            rated_items.append(float(mrating))
    avg_rating = round(sum(rated_items) / len(rated_items), 1) if rated_items else 0
    rating_buckets = [
        {"label": "8-10", "count": sum(1 for r in rated_items if 8 <= r <= 10)},
        {"label": "6-8",  "count": sum(1 for r in rated_items if 6 <= r < 8)},
        {"label": "4-6",  "count": sum(1 for r in rated_items if 4 <= r < 6)},
        {"label": "2-4",  "count": sum(1 for r in rated_items if 2 <= r < 4)},
        {"label": "0-2",  "count": sum(1 for r in rated_items if 0 < r < 2)},
    ]

    # ── Genres (of watched content, stored at add time) ──
    genre_counter = {}
    for tmdb_id, total_ep, runtime, rating, genres in user_shows:
        if show_stats.get(tmdb_id, (0, None))[0] > 0 and genres:
            for g in genres.split(','):
                g = g.strip()
                if g:
                    genre_counter[g] = genre_counter.get(g, 0) + 1
    for _movie_id, _name, mrun, _mrating, mgenres in watched_movies:
        if mgenres:
            for g in mgenres.split(','):
                g = g.strip()
                if g:
                    genre_counter[g] = genre_counter.get(g, 0) + 1
    genre_data = sorted(genre_counter.items(), key=lambda x: x[1], reverse=True)[:10]
    max_genre = genre_data[0][1] if genre_data else 1

    # ── Monthly activity chart ──
    monthly_episodes = [{"month": m, "count": c} for m, c in sorted(monthly.items())]
    monthly_episodes_max = max((m["count"] for m in monthly_episodes), default=1) or 1

    # ── Streaks ──
    activity_dates = sorted(set(ep_dates + mv_dates))
    date_set = set(activity_dates)

    def _add_days(d, n):
        return (_date.fromisoformat(d) + _timedelta(days=n)).isoformat()

    current_streak = 0
    d = today
    if d not in date_set:
        d = _add_days(today, -1)
    while d in date_set:
        current_streak += 1
        d = _add_days(d, -1)

    longest_streak = 0
    run = 0
    prev = None
    for d in activity_dates:
        run = run + 1 if (prev is not None and _add_days(prev, 1) == d) else 1
        longest_streak = max(longest_streak, run)
        prev = d

    # ── Best day ──
    day_counts = {}
    for d in ep_dates + mv_dates:
        day_counts[d] = day_counts.get(d, 0) + 1
    best_day = max(day_counts.items(), key=lambda x: x[1]) if day_counts else (None, 0)

    # ── Heatmap (last 52 weeks x 7 days, ending today) ──
    heatmap_weeks = []
    week = []
    start = _add_days(today, -363)
    for i in range(364):
        d = _add_days(start, i)
        week.append({"date": d, "count": day_counts.get(d, 0)})
        if len(week) == 7:
            heatmap_weeks.append(week)
            week = []

    # ── Milestones ──
    milestones = []
    if ep_dates:
        milestones.append({"icon": "🎬", "text": f"First episode watched on {min(ep_dates)}"})
    if mv_dates:
        milestones.append({"icon": "🍿", "text": f"First movie watched on {min(mv_dates)}"})
    if completed_count > 0:
        milestones.append({"icon": "🏆", "text": f"Completed {completed_count} series"})
    if longest_streak >= 2:
        milestones.append({"icon": "🔥", "text": f"Longest streak: {longest_streak} days"})
    if best_day[0]:
        milestones.append({"icon": "📈", "text": f"Most active day: {best_day[0]} ({best_day[1]} watched)"})
    for n in (100, 250, 500, 1000, 5000):
        if total_episodes >= n:
            milestones.append({"icon": "💯", "text": f"Watched {n} episodes"})
    if total_hours >= 50:
        milestones.append({"icon": "⏱", "text": f"Watched {total_hours} hours of content"})

    # Member since: prefer users.created_at, fall back to first activity
    member_since = created_at[:10] if created_at else (activity_dates[0] if activity_dates else "")

    return render_template(
        'stats.html',
        username=session.get('username', 'User'),
        member_since=member_since,
        total_episodes=total_episodes,
        total_movies=total_movies,
        total_hours=total_hours,
        total_tv_shows=total_tv_shows,
        completed_shows=completed_count,
        completion_rate=completion_rate,
        shows_with_activity=shows_with_activity,
        avg_rating=avg_rating,
        rating_buckets=rating_buckets,
        genre_data=genre_data,
        max_genre=max_genre,
        monthly_episodes=monthly_episodes,
        monthly_episodes_max=monthly_episodes_max,
        current_streak=current_streak,
        longest_streak=longest_streak,
        best_day=best_day,
        heatmap_weeks=heatmap_weeks,
        milestones=milestones,
    )



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug)
