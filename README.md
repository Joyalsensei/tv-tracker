# TV Tracker 📺

Track your TV shows and movies — what you've watched, what you're watching, and what's next.

**Live demo:** [joyal.pythonanywhere.com](https://joyal.pythonanywhere.com)

---

## ✨ Features

- **Netflix-style home page** — Browse trending, popular, and top-rated TV shows & movies. Genre shelves make discovery easy.
- **Search** — Find any TV show or movie via TMDB.
- **Track episodes** — Mark episodes as watched season-by-season.
- **Inline episode tracker** — ⚡ Quick Mark button on show pages to toggle episodes without leaving the page.
- **Bulk actions** — Mark an entire season, all previous seasons, or the whole show as watched.
- **Movie tracking** — Toggle watched/unwatched for movies.
- **Watch history** — See a timeline of everything you've watched.
- **Where to watch** — See which streaming services (Netflix, Prime, Hotstar, etc.) have your shows.
- **Recommendations** — Get similar shows and recommendations for everything you look up.
- **User accounts** — Sign up, log in, and keep your data private.
- **Admin dashboard** — View user stats and activity (for the first registered user).

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python / Flask |
| Database | SQLite |
| API | TMDB (The Movie Database) |
| Hosting | PythonAnywhere |
| Templating | Jinja2 |
| Frontend | HTML, CSS (responsive, dark theme) |

---

## 🚀 Run Locally

### Prerequisites

- Python 3.10+
- A [TMDB API key](https://www.themoviedb.org/settings/api) (free)

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/Joyalsensei/tv-tracker.git
cd tv-tracker

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate    # Linux/Mac
venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env and add your TMDB_API_KEY and a FLASK_SECRET_KEY

# 5. Run the app
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## ☁️ Deploy on PythonAnywhere

1. Clone this repo into your PythonAnywhere home directory:
   ```bash
   git clone https://github.com/Joyalsensei/tv-tracker.git ~/tv-tracker
   ```

2. Go to **Web** tab on PythonAnywhere and set:
   - **Source code:** `/home/joyal/tv-tracker`
   - **WSGI configuration file:** Edit to point to `wsgi.py`:
     ```python
     import sys
     path = '/home/joyal/tv-tracker'
     if path not in sys.path:
         sys.path.insert(0, path)
     from app import app as application
     ```

3. Add environment variables in the **Web** tab:
   - `TMDB_API_KEY` — your TMDB API key
   - `FLASK_SECRET_KEY` — a random string for session encryption
   - `FLASK_ENV` — set to `production`

4. Install dependencies in a virtual environment:
   ```bash
   pip install -r requirements.txt --user
   ```

5. Click the green **Reload** button.

---

## 📁 Project Structure

```
tv-tracker/
├── app.py              # Flask application (routes, auth, API logic)
├── database.py         # Database helpers (SQLite)
├── wsgi.py             # WSGI entry point for PythonAnywhere
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .gitignore          # Git ignore rules
├── README.md           # This file
├── ROADMAP.md          # Planned features
└── templates/          # Jinja2 HTML templates
    ├── search.html         # Home page with content shelves
    ├── search_results.html # Search results
    ├── show_detail.html    # TV show details + inline episode tracker
    ├── movie_detail.html   # Movie details
    ├── season_detail.html  # Season episode list
    ├── myshows.html        # User's TV shows with progress
    ├── mymovies.html       # User's movies
    ├── history.html        # Watch history timeline
    ├── login.html          # Login page
    ├── signup.html         # Sign up page
    └── admin.html          # Admin dashboard
```

---

## 🐛 Bug Fixes (July 19, 2026)

### Fixed: Episode tracking issues

1. **Progress bar accuracy** — Now always fetches fresh episode counts from TMDB instead of using stale cached values. Works correctly for ongoing shows that get new seasons.
2. **Bulk mark operations** — Marking all seasons / previous seasons no longer breaks the episode counter. Cached totals are updated after every bulk operation.
3. **Inline episode tracker** — Added "⚡ Quick Mark" button on each season. Click to expand a compact grid of clickable episode circles — mark/unmark without leaving the page.

---

## 📌 Roadmap

See [ROADMAP.md](ROADMAP.md) for planned features including:
- PWA / Install as app on phone
- Favorites ❤️
- Show status management
- Stats dashboard
- And more!

---

## 📄 License

This project is for personal use. TMDB data is used in accordance with [TMDB Terms of Service](https://www.themoviedb.org/terms-of-use).
