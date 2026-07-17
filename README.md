# TV Tracker 📺

Track your TV shows and movies — what you've watched, what you're watching, and what's next.

**Live demo:** 
https://joyal.pythonanywhere.com

---

## ✨ Features

- **Netflix-style home page** — Browse trending, popular, and top-rated TV shows & movies. Genre shelves make discovery easy.
- **Search** — Find any TV show or movie via TMDB.
- **Track episodes** — Mark episodes as watched season-by-season.
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
| Database | PostgreSQL (production) / SQLite (development) |
| API | TMDB (The Movie Database) |
| Hosting | Render |
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
git clone https://github.com/YOUR_USERNAME/tv-tracker.git
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

## ☁️ Deploy on Render

This app is designed to deploy easily on [Render](https://render.com):

1. Fork/clone this repo to GitHub
2. On Render, create a **New Web Service** — connect your GitHub repo
3. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Add environment variables:
   - `TMDB_API_KEY` — your TMDB API key
   - `FLASK_SECRET_KEY` — a random string for session encryption
   - `FLASK_ENV` — set to `production`
5. Create a **PostgreSQL database** on Render and link it (Render sets `DATABASE_URL` automatically)

---

## 📁 Project Structure

```
tv-tracker/
├── app.py              # Flask application (routes, auth, API logic)
├── database.py         # Database helpers (SQLite + PostgreSQL support)
├── add_show.py         # CLI helper to add shows
├── migrate.py          # Database migration utilities
├── requirements.txt    # Python dependencies
├── Procfile            # Render start command
├── runtime.txt         # Python version for Render
├── ROADMAP.md          # Planned features
└── templates/          # Jinja2 HTML templates
    ├── search.html         # Home page with content shelves
    ├── search_results.html # Search results
    ├── show_detail.html    # TV show details + episode tracking
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
