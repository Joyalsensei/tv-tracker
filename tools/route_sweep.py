# Route sweep: exercise every page/endpoint via the Flask test client.
# Uses a FAKE tmdb_get so the sweep is deterministic and fast (no network).
import io
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import app as appmod  # noqa: E402

SHOW = {
    "id": 1396, "name": "Breaking Bad", "poster_path": None,
    "vote_average": 9.5, "status": "Ended", "first_air_date": "2008-01-20",
    "episode_run_time": [47], "genres": [{"name": "Drama"}],
    "overview": "A chemistry teacher turns to crime.",
    "number_of_seasons": 1, "tagline": "",
    "seasons": [{"season_number": 1, "episode_count": 7}],
    "recommendations": {"results": []}, "similar": {"results": []},
    "videos": {"results": []},
    "next_episode_to_air": None,
}
MOVIE = {
    "id": 603, "title": "The Matrix", "poster_path": None,
    "vote_average": 8.7, "release_date": "1999-03-31", "runtime": 136,
    "genres": [{"name": "Action"}], "overview": "Reality is a simulation.",
    "recommendations": {"results": []}, "similar": {"results": []},
    "videos": {"results": []},
}
SEASON = {
    "name": "Season 1", "episodes": [
        {"episode_number": 1, "name": "Pilot", "air_date": "2008-01-20",
         "overview": "", "still_path": None},
        {"episode_number": 2, "name": "Cat's in the Bag", "air_date": "2008-01-27",
         "overview": "", "still_path": None},
    ],
}
CARD = {"id": 1, "name": "Test Show", "title": "Test Show", "poster_path": None,
        "vote_average": 8.0, "first_air_date": "2020-01-01",
        "release_date": "2020-01-01", "backdrop_path": None}


def fake_tmdb_get(url, params, ttl=600):
    if "search/tv" in url or "search/movie" in url:
        return {"results": [dict(CARD)], "total_results": 1, "total_pages": 1}
    if "trending/tv" in url or "trending/movie" in url or "/popular" in url or "/top_rated" in url or "/discover/tv" in url:
        return {"results": [dict(CARD)]}
    if "/season/" in url:
        return dict(SEASON)
    if url.endswith("/tv/1396") or "/tv/1396" in url and "/season" not in url and "/watch" not in url:
        return dict(SHOW)
    if url.endswith("/movie/603") or "/movie/603" in url:
        return dict(MOVIE)
    if "/watch/providers" in url:
        return {"results": {}}
    return dict(SHOW)


appmod.tmdb_get = fake_tmdb_get
app = appmod.app
client = app.test_client()


from database import get_conn as _get_conn  # noqa: E402


def _clean_test_users():
    """Remove users this sweep creates so reruns stay idempotent."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username LIKE 'tester_%'")
        for (uid,) in cur.fetchall():
            cur.execute("DELETE FROM watched_episodes WHERE user_id=?", (uid,))
            cur.execute("DELETE FROM watched_movies WHERE user_id=?", (uid,))
            cur.execute("DELETE FROM shows WHERE user_id=?", (uid,))
            cur.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()


def csrf():
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def check(method, path, expected=None, **kw):
    r = getattr(client, method)(path, **kw)
    if expected is not None:
        flag = "OK " if r.status_code == expected else "FAIL"
    else:
        flag = "OK " if r.status_code < 400 else "FAIL"
    print(f"[{flag}] {method.upper()} {path} -> {r.status_code}")
    return r


_clean_test_users()

print("=" * 60)
print("AUTH FLOW")
check("get", "/signup")
user = "tester_" + uuid.uuid4().hex[:6]
check("post", "/signup", data={
    "username": user, "password": "secret123",
    "password_confirm": "secret123", "_csrf_token": csrf(),
}, follow_redirects=False)
check("post", "/login", data={"username": user, "password": "secret123", "_csrf_token": csrf()}, follow_redirects=False)
check("get", "/logout")

print("=" * 60)
print("PUBLIC PAGES")
check("get", "/")
check("get", "/login")
check("get", "/signup")
check("get", "/search?query=matrix&type=movie")
check("get", "/search?query=breaking&type=tv&page=2")
check("get", "/search?query=bad&type=tv&page=abc")  # invalid page -> clamp
check("get", "/health")
check("get", "/nonexistent-page", expected=404)

print("=" * 60)
print("AUTH-GATED PAGES (logged in)")
check("post", "/login", data={"username": user, "password": "secret123", "_csrf_token": csrf()}, follow_redirects=False)
check("get", "/myshows")
check("get", "/mymovies")
check("get", "/upcoming")
check("get", "/stats")
check("get", "/history")
check("get", "/admin")
check("get", "/show/1396")
check("get", "/show/1396/season/1")
check("get", "/movie/603")
check("get", "/api/show/1396/watched")
check("get", "/api/show/1396/season-totals")
check("get", "/api/show/1396/season/1")

print("=" * 60)
print("MUTATIONS (CSRF-validated POSTs)")
check("post", "/add/1396", data={"_csrf_token": csrf()})
check("post", "/add_movie/603", data={"_csrf_token": csrf()})
check("post", "/watch/1396/1/1", data={"_csrf_token": csrf()})
check("post", "/watch_movie/603", data={"_csrf_token": csrf()})
check("post", "/show/1396/set_status", data={"_csrf_token": csrf(), "status": "watching"})
check("post", "/mark_season_watched/1396/1/2", data={"_csrf_token": csrf()})
check("post", "/mark_all_seasons_watched/1396", data={"_csrf_token": csrf()})
check("get", "/stats")

print("=" * 60)
print("NEGATIVE / SECURITY CASES")
check("get", "/nonexistent-page", expected=404)      # missing page -> 404
check("post", "/watch_movie/603", data={}, expected=403)          # missing CSRF -> 403
check("post", "/watch/1396/0/0", data={"_csrf_token": csrf()}, expected=400)  # invalid indices -> 400
check("get", "/add/1396", expected=405)              # GET on POST-only -> 405
check("post", "/add/1396", data={"username": user}, expected=403)  # no CSRF -> 403
check("post", "/show/1396/set_status", data={"_csrf_token": csrf(), "status": "bogus"}, expected=400)  # invalid status -> 400

print("=" * 60)
print("REMOVE + RE-VERIFY")
check("post", "/remove/1396", data={"_csrf_token": csrf()}, follow_redirects=True)
check("post", "/remove/603", data={"_csrf_token": csrf()}, follow_redirects=True)
check("get", "/stats")

print("=" * 60)
print("PASSWORD RESET FLOW")
import hashlib as _h
import time as _t
# Non-admin users are denied the admin reset tool
check("post", "/admin/reset_password", data={"_csrf_token": csrf(), "username": user, "action": "link"}, expected=403)
# Simulate an admin generating a one-time reset link (token stored as SHA-256)
_token = "sweepresettoken123"
_conn = _get_conn()
try:
    _cur = _conn.cursor()
    _cur.execute("UPDATE users SET password_reset_token=?, password_reset_expires=? WHERE username=?", (
        _h.sha256(_token.encode()).hexdigest(),
        _t.strftime('%Y-%m-%d %H:%M:%S', _t.gmtime(_t.time() + 3600)),
        user,
    ))
    _conn.commit()
finally:
    _conn.close()
check("get", "/reset_password/" + _token)  # valid link -> form
r = check("post", "/reset_password/" + _token, data={
    "_csrf_token": csrf(), "password": "newpass123", "password_confirm": "newpass123"
})  # success -> redirect to login (302)
if r.status_code == 302:
    print("PASS reset link used once (302 redirect)")
else:
    print("FAIL reset POST did not redirect:", r.status_code)
# Single-use: after a successful reset the link is dead (page shows 'Link Invalid',
# status stays 200 for good UX)
r = check("get", "/reset_password/" + _token)
if b"Link Invalid" in r.data:
    print("PASS reset link invalid after use")
else:
    print("FAIL reset link still usable after use!")
# Replaying the POST is rejected (redirect to login, password unchanged)
r = check("post", "/reset_password/" + _token, data={
    "_csrf_token": csrf(), "password": "another1", "password_confirm": "another1"
}, expected=302)
# The new password actually works (assert via session state, not status code)
check("post", "/login", data={"username": user, "password": "newpass123", "_csrf_token": csrf()})
with client.session_transaction() as sess:
    new_pw_ok = sess.get("user_id") is not None
print("PASS new password logs in" if new_pw_ok else "FAIL new password logs in")

# The old password must be rejected (failed login leaves the session unauthenticated)
check("get", "/logout")
check("post", "/login", data={"username": user, "password": "secret123", "_csrf_token": csrf()})
with client.session_transaction() as sess:
    old_pw_rejected = sess.get("user_id") is None
print("PASS old password rejected" if old_pw_rejected else "FAIL old password still works!")

_clean_test_users()
print("SWEEP COMPLETE")
