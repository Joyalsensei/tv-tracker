<div align="center">

# 📺 TV Tracker

**Track your TV shows & movies — what you've watched, what you're watching, and what's next.**

Built with Flask · SQLite · TMDB · Vanilla JS

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=flat&logo=flask&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat&logo=sqlite&logoColor=white)
![TMDB](https://img.shields.io/badge/Data-TMDB-01D277?style=flat)
![License](https://img.shields.io/badge/License-Personal_Use-8A2BE2?style=flat)
![Accessibility](https://img.shields.io/badge/WCAG_2.2-AA-27AE60?style=flat)

🔗 **Live demo:** [joyal.pythonanywhere.com](https://joyal.pythonanywhere.com)

</div>

---

## ✨ Features

### 🏠 Netflix-style discovery
- **Home shelves** — Trending, popular, and top-rated TV & movies, plus genre rows for easy discovery
- **Search** — Find any show or movie via TMDB (paginated results, TV/Movie toggle)
- **Details** — Posters, overviews, ratings, streaming providers (Netflix, Prime, Hotstar…), and similar/recommended titles

### ▶️ Continue Watching
- Shows you've started appear in a **Continue Watching** shelf on the home page
- Shows `x/y episodes` progress with a **Resume** button that deep-links to the season where you left off
- Disappears automatically the moment a show hits 100% (or is dropped / set to plan-to-watch)

### ✅ Episode & movie tracking
- **Episode dots** — mark/unmark single episodes on the show page or per-season page
- **Auto-catch-up** — marking a season's final episode marks all earlier episodes & seasons
- **Bulk actions** — mark a whole season, all previous seasons, or the entire show
- **Movie tracking** — toggle watched/unwatched instantly
- **Optimistic UI** — toggles update instantly and roll back on failure, no page reloads
- **Celebration parity** — movies and finished series get the same confetti + success banner

### 📊 Profile & Stats
- Accurate metrics computed from stored data — episodes/movies watched, series completed, **watch time** (per-title runtime), **streaks** (current & longest), **52-week activity heatmap**, monthly activity, top genres, and ratings distribution
- **Milestones** celebrate your progress

### 🔐 Accounts & Security
- Sign up / log in with hashed passwords (bcrypt-style via Werkzeug)
- **CSRF protection** on every state-changing request; mutations are POST-only
- **Rate limiting** on login, signup & search
- **Configurable admin** — `/admin` is restricted to usernames in `ADMIN_USERNAME` (defaults to the first registered user)
- **Password recovery** — admin-driven one-time reset links or temporary passwords, no email service needed
- Security headers, HSTS in production, and **XSS-safe** rendering

### ♿ Accessibility (WCAG 2.2 AA)
- Visible keyboard focus, proper link/button semantics, accessible names
- Contrast-compliant palette, 44×44px touch targets, reduced-motion support

### 🛠️ Reliability
- **TMDB circuit breaker** — after repeated failures, pages render instantly from cached/DB data while offline
- Parallelized TMDB fetches, N+1 query elimination, DB indexes, in-memory cache
- Friendly 400/403/404/500 error pages

---

## 🧰 Tech Stack

| Layer      | Technology |
|------------|------------|
| Backend    | Python · Flask 3 · Jinja2 |
| Database   | SQLite (WAL mode) |
| API        | TMDB (The Movie Database) |
| Frontend   | HTML · CSS · Vanilla JS (no build step) |
| Hosting    | PythonAnywhere (see [deploy](#-deploy-on-pythonanywhere)) |

---

## 🚀 Run Locally

### Prerequisites
- **Python 3.10+**
- A free [TMDB API key](https://www.themoviedb.org/settings/api)

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/Joyalsensei/tv-tracker.git
cd tv-tracker

# 2. Create & activate a virtual environment
python -m venv venv
source venv/bin/activate        # Linux/macOS
venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
#    → edit .env and add your TMDB_API_KEY + a random FLASK_SECRET_KEY

# 5. Run the app
python app.py
```

Open **[http://localhost:5000](http://localhost:5000)** 🎬

---

## ⚙️ Configuration

| Variable | Required | Description |
|---|---|---|
| `TMDB_API_KEY` | ✅ | API key from [TMDB](https://www.themoviedb.org/settings/api) |
| `FLASK_SECRET_KEY` | ✅ | Random string for session encryption (auto-generated if missing) |
| `FLASK_ENV` | ❌ | `production` enables secure cookies + HSTS |
| `ADMIN_USERNAME` | ❌ | Comma-separated usernames allowed to open `/admin` (case-insensitive). Defaults to the first registered user |
| `DATABASE_PATH` | ❌ | Custom SQLite file path (e.g. `/data/tracker.db` for volumes) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | ❌ | Enables Google sign-in |

---

## ☁️ Deploy on PythonAnywhere

1. **Push your code to GitHub** (see [below](#-pushing-to-github)).
2. In the PythonAnywhere **Bash console**:

   ```bash
   git clone https://github.com/Joyalsensei/tv-tracker.git ~/tv-tracker
   cd ~/tv-tracker
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Open the **Web** tab → *Add a new web app* (Manual configuration, Python 3.10+).
4. Set **Source code** to `/home/<your-username>/tv-tracker` and update the WSGI file:

   ```python
   import sys
   path = '/home/<your-username>/tv-tracker'
   if path not in sys.path:
       sys.path.insert(0, path)
   from app import app as application
   ```

5. **Set environment variables.** PythonAnywhere's Web tab has **no**
   environment-variable form — but this app auto-loads a `.env` file from
   its own directory, so just create one in the project folder:

   ```bash
   cd ~/tv-tracker
   cp .env.example .env      # or create it with a text editor
   nano .env                 # add TMDB_API_KEY, FLASK_SECRET_KEY,
                             # FLASK_ENV=production, ADMIN_USERNAME=<you>
   ```

   > The WSGI file already does `from app import app`, and `app.py` runs
   > `load_dotenv()` at startup — so no WSGI edits are needed.
6. Click the green **Reload** button. Done! 🎉

> 💡 **Keep PythonAnywhere in sync with GitHub:** after each `git push`, open the PythonAnywhere Bash console and run `cd ~/tv-tracker && git pull`, then hit **Reload**.

---

## 🗂️ Project Structure

```
tv-tracker/
├── app.py                  # Flask app: routes, auth, TMDB, stats, APIs
├── database.py             # SQLite helpers, migrations, indexes, backups
├── wsgi.py                 # WSGI entry point for PythonAnywhere
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── README.md               # This file
├── ROADMAP.md              # Planned features
├── TESTING.md              # Bug history + regression checklist
├── static/
│   ├── style.css           # Global styles (dark theme, WCAG-compliant)
│   ├── app.js              # Shared JS: toasts, confetti, CSRF-safe actions
│   └── manifest.json       # PWA manifest
├── templates/              # Jinja2 templates
│   ├── base.html           # Layout, nav, toasts
│   ├── search.html         # Home page with shelves + Continue Watching
│   ├── search_results.html # Search results
│   ├── show_detail.html    # Show page + inline episode tracker
│   ├── season_detail.html  # Per-season episode list
│   ├── movie_detail.html   # Movie page
│   ├── myshows.html        # My Shows with progress
│   ├── mymovies.html       # My Movies
│   ├── history.html        # Watch history timeline
│   ├── stats.html          # Profile & statistics dashboard
│   ├── upcoming.html       # Upcoming episodes calendar
│   ├── admin.html          # Admin dashboard (users, reset, repair)
│   ├── reset_password.html # One-time password reset page
│   ├── login.html / signup.html / 400.html / 403.html / 404.html / 500.html
└── tools/
    └── route_sweep.py      # Automated route regression sweep
```

---

## 🧪 Testing

The repo ships a deterministic **route sweep** that exercises every page and endpoint
with a fake TMDB client (no network needed):

```bash
python tools/route_sweep.py
```

See [TESTING.md](TESTING.md) for the full regression checklist and bug history.

---

## 📌 Roadmap

Planned features live in [ROADMAP.md](ROADMAP.md) — PWA offline support, favorites,
show status management, and more.

---

## 📄 License & Attribution

For personal use. Show/movie metadata and imagery are provided by **TMDB** in
accordance with the [TMDB Terms of Service](https://www.themoviedb.org/terms-of-use).
