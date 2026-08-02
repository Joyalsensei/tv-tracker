# TV Tracker - Feature Roadmap 🗺️

> Saved on: July 15, 2026

## ✅ Current Features (Live on Render)
- User signup / login / logout
- Netflix-style home page (trending, popular, top-rated, genre shelves) — TMDB fetches parallelized
- Search TV shows & movies (via TMDB) — paginated + rate limited
- Add shows / movies to your list — CSRF-safe POST (was GET)
- Track watched episodes per season (toggle, bulk mark, mark all previous seasons)
- Continue Watching shelf on the home page (with Resume deep-link)
- Movie watch toggle — completion parity with series (confetti + banner)
- Watch history timeline
- Watch providers (Netflix, Prime, Hotstar, etc.)
- Recommendations & similar content
- Admin dashboard
- PostgreSQL on Render
- Security headers & CSRF protection
- Rate limiting + TMDB circuit breaker (graceful offline degradation)
- Stats/Profile dashboard: streaks, heatmap, milestones, ratings, genres, watch time
- PWA / manifest.json (install as app)
- Show status management (Plan to Watch / Watching / On Hold / Dropped / Completed)
- Request logging + health/uptime endpoint + DB backup command

---

## 🚀 Planned Features (Add Later)

### 1. PWA / manifest.json (Install as App)
- ✅ Done — manifest.json + apple-touch-icon are wired up
- Add service worker for offline caching of static assets

### 2. Favorites ❤️
- Add heart button on show/movie detail pages
- Create a My Favorites page
- (Note: Favorites table not yet created — will need a database migration)

### 3. Show Status Management
- ✅ Done — statuses available on the show detail page, affect My Shows grouping
- Favorites-style lists could build on this later

### 4. UI Polish & Dark Mode
- Loading skeletons / spinners while TMDB data loads
- Smoother transitions and animations
- Better responsive design for mobile
- Accessibility improvements

### 5. Stats Dashboard
- ✅ Done — streaks, heatmap, milestones, ratings, genres, watch time
- Future: shareable stat cards, CSV export

### 6. Upcoming Episodes Calendar
- Show what is airing soon based on tracked shows
- Calendar or timeline view

### 7. Custom Lists
- Let users create named lists (e.g. Weekend Binge, Classics, To Watch)
- Organize shows/movies into custom lists

### 8. Export / Import Data
- Download watch history as JSON or CSV
- Optionally import from TV Time
