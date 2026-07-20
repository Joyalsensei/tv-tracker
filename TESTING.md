# TV Tracker — Testing Guide

## Previously Fixed Bugs (Do Not Reintroduce!)

This file documents known bugs and their fixes so future changes don't silently
reintroduce them. **Read this before modifying ANY watched-episode, search,
or progress-tracking code.**

---

### Bug: Movie search shows wrong result (Fixed July 2026)

**Root cause:** TMDB IDs are **not globally unique** across movies and TV shows.
The old `/show/<id>` route always tried TV lookup first, so a movie search result
could open a TV show's detail page if both happened to share the same numeric ID.

**Fix:** Added a dedicated `/movie/<movie_id>` route that directly fetches movie data
without attempting a TV lookup first. Search results now link movies to `/movie/<id>`
and TV shows to `/show/<id>`.

**Manual test checklist:**
1. Search for a popular movie (e.g. "The Matrix")
2. Click the movie result — it should open the movie detail page, not a TV show
3. Search for a TV show (e.g. "Breaking Bad")
4. Click the TV show result — it should open the show detail page as expected
5. Verify the search type selector (TV/Movie) changes the link routes correctly

---

### Bug: Episode/season auto-catch-up (Fixed July 2026)

**Root cause (regression):** When marking the last episode of a season as watched,
the backend returned `is_last_episode: true` but did NOT automatically mark earlier
episodes/seasons. The frontend on `season_detail.html` had confirm-dialog prompts,
but `show_detail.html`'s inline tracker never checked `is_last_episode` at all.
This meant:
- Only the clicked episode was marked
- Earlier seasons showed no green tick
- The progress bar was inaccurate (manual catch-up inflated counts)

**Fix:** The `mark_watched` endpoint now automatically:
1. Marks all earlier episodes in the same season (1..N-1) as watched
2. Marks ALL episodes in all prior seasons as watched
3. Returns `auto_caught_up: true` and `auto_marked_count` so the frontend
   can show a confirmation toast

**Manual test checklist:**
1. Add a show with 3+ seasons (e.g. "Breaking Bad" or "Stranger Things")
2. Go to the show detail page
3. Open the inline tracker for Season 3
4. Click the LAST episode of Season 3
5. Verify: All episodes in Season 3 AND all episodes in Seasons 1-2 show
   as watched (green dots)
6. Verify: The season list shows green checkmarks for all seasons
7. Verify: The My Shows progress bar shows the correct count
8. Repeat steps 2-7 on `season_detail.html` (the dedicated season page)
9. Mark a single episode as UNWATCHED — verify no auto-catch-up occurs
10. Verify that marking episodes WITHOUT reaching the last episode does NOT
    trigger auto-catch-up

---

### Bug: Season-level green tick missing (Fixed July 2026)

**Root cause:** The season list in `show_detail.html` had no visual indicator
of watched status. The green checkmark only appeared on individual episode
dots, not at the season level.

**Fix:** Added a `.season-check` span to each season row that displays:
- Green filled circle with checkmark when all episodes in that season are watched
- Dimmed circle when not all episodes are watched

**Manual test checklist:**
1. Open a show with partially watched seasons
2. Verify empty seasons show a dim (not-watched) circle
3. Mark a season complete via auto-catch-up
4. Verify the season row now shows a green checkmark
5. Refresh the page — green checkmarks should persist

---

### 🚨 Regression Risk: Season checkmark uses SHOW endpoint episode_count (Fixed August 2026)

**Root cause (regression):** The green checkmark comparison in `refreshSeasonCheckmarks`
was using `season.episode_count` from the **show endpoint** (via the `data-season-counts`
HTML attribute). The codebase documents that the show endpoint's per-season
`episode_count` can be stale/inaccurate for ongoing shows. The auto-catch-up
server logic correctly uses the **season endpoint** (which is always accurate),
but the UI compared against the wrong total — so green ticks never appeared
for prior seasons even though all episodes were correctly marked.

**Root cause of the REGRESSION:** The original fix for season-level checkmarks
did not address *how* the season totals were sourced. The `data-season-counts`
attribute was populated from the show endpoint (convenient — no extra API calls)
but the auto-catch-up used the season endpoint. Any mismatch between the two
sources made the checkmark comparison fail.

**Fix:**
1. Created a new server-side API endpoint `/api/show/<id>/season-totals` that
   fetches accurate per-season episode counts from the SEASON endpoint.
   This endpoint uses the 1-hour TMDB cache, so repeated calls are cheap.
2. Updated `refreshSeasonCheckmarks()` in `show_detail.html` to fetch from
   this endpoint (instead of parsing the stale `data-season-counts` attribute).
3. Removed the `data-season-counts` attribute from the template entirely.

**Why this fix is more durable:**
- The accurate season totals now come from the same endpoint that the
  auto-catch-up logic uses — so they CANNOT go out of sync.
- The new endpoint is cached (1 hour), so it doesn't add TMDB API costs.
- The JavaScript fetches the data lazily (only when `refreshSeasonCheckmarks`
  runs), not on every page load.

**Manual test checklist:**
1. Add a show with 3+ seasons where at least one season's actual episode count
   differs from the show endpoint's `episode_count` (e.g., an ongoing show)
2. Open the show detail page
3. Mark the last episode of the latest season via inline tracker
4. Verify ALL prior seasons show green checkmarks immediately
5. Refresh the page — green checkmarks should persist
6. Run the admin repair tool — verify totals match between season endpoints
7. Check that `data-season-counts` is no longer present in the HTML source

---

### Bug: Progress bar displayed wrong percentage (Fixed earlier)

**Root cause:** The `total_episodes` cached in the DB was not updated after
bulk mark operations. The progress bar calculated `watched / total` with a
stale total.

**Fix:** `_update_cached_total_episodes()` is called after every mark operation.
For ongoing shows, the total is refreshed from TMDB season endpoints on every
`myshows` page load.

**Manual test checklist:**
1. Mark several episodes across different seasons
2. Go to My Shows — verify the progress bar shows correct percentage
3. Run the admin repair tool — verify no shows need correction
4. For an ongoing show (Returning Series), wait for a new episode to air
5. Verify My Shows auto-detects the new total

---

## Full Regression Test Checklist

Run this after ANY change to search, episode tracking, or progress logic.

### Search
- [ ] Search for a movie — results show correct links
- [ ] Search for a TV show — results show correct links
- [ ] Click a movie result — opens movie detail
- [ ] Click a TV show result — opens show detail
- [ ] Add button on search card works for both types
- [ ] Empty search shows "No results" state

### Episode Tracking
- [ ] Mark a single episode watched — dot turns green
- [ ] Mark a single episode unwatched — dot clears
- [ ] Mark last episode of season — auto-catch-up fires
- [ ] Auto-catch-up: all earlier episodes in same season marked
- [ ] Auto-catch-up: all episodes in prior seasons marked
- [ ] Auto-catch-up toast appears with count
- [ ] Season checkmark turns green after auto-catch-up
- [ ] Mark Whole Season Watched button works
- [ ] Mark All Seasons Watched button works

### Progress / My Shows
- [ ] In-progress shows show correct count and percentage
- [ ] Completed shows appear in "Completed" section
- [ ] Progress bar does not exceed 100%
- [ ] Remove button removes the show and its watched data

### Stats
- [ ] Total episodes count matches database
- [ ] Completion rate is accurate
- [ ] Monthly activity chart renders

### Bottom Navigation
- [ ] All 5 nav items accessible (Explore, Shows, Upcoming, Movies, Profile)
- [ ] Active nav item is visually highlighted
- [ ] Nav works on both desktop and mobile
- [ ] Unauthenticated users see Login/Signup in nav
