# TV Tracker - Feature Roadmap 🗺️

> Saved on: July 15, 2026

## ✅ Current Features (Live on Render)
- User signup / login / logout
- Netflix-style home page (trending, popular, top-rated, genre shelves)
- Search TV shows & movies (via TMDB)
- Add shows / movies to your list
- Track watched episodes per season (toggle, bulk mark, mark all previous seasons)
- Movie watch toggle
- Watch history timeline
- Watch providers (Netflix, Prime, Hotstar, etc.)
- Recommendations & similar content
- Admin dashboard
- PostgreSQL on Render
- Security headers & CSRF protection

---

## 🚀 Planned Features (Add Later)

### 1. PWA / manifest.json (Install as App)
- Add a manifest.json file so users can Add to Home Screen
- Make the app open full-screen like a native app on phones
- Add a nice app icon

### 2. Favorites ❤️
- Add heart button on show/movie detail pages
- Create a My Favorites page
- (Note: Favorites table not yet created — will need a database migration)

### 3. Show Status Management
- Let users set custom statuses: Plan to Watch, Watching, On Hold, Dropped, Completed
- Currently only tracks watched/unwatched — this adds more organization

### 4. UI Polish & Dark Mode
- Loading skeletons / spinners while TMDB data loads
- Smoother transitions and animations
- Better responsive design for mobile
- Accessibility improvements

### 5. Stats Dashboard
- Personal viewing stats: total hours watched, episodes count
- Completion rate, most-watched genres
- Watching streaks
- Pie charts / graphs

### 6. Upcoming Episodes Calendar
- Show what is airing soon based on tracked shows
- Calendar or timeline view

### 7. Custom Lists
- Let users create named lists (e.g. Weekend Binge, Classics, To Watch)
- Organize shows/movies into custom lists

### 8. Export / Import Data
- Download watch history as JSON or CSV
- Optionally import from TV Time
