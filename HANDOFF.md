# ShuleHub — handoff note (paste this into a new Claude chat, with the files listed below)

Last updated: 2026-11-04

## The project
ShuleHub: a Flask school-management app (Gtimetable / exam-analysis software) with a
SQLite/Postgres `database.py` (helpers: fetch_all, fetch_one, execute, get_connection, USE_POSTGRES,
get_school_settings), blueprints registered in `app.py`, a `permissions.py`, and `templates/base.html`
(top navigation, CSS variables --green-900, --gold-500, --paper, --ink, --muted, --line).

## Done so far
1. **Master timetable + Remedial timetable** (`master.py`, `remedial.py`, `templates/master/*`,
   `templates/remedial/*`, `apply_timetable_edits.py`). Delivered as `timetable_extras.zip`.
2. **Remedial v2** (same zip, already delivered):
   - Two blocks per schedule: Morning (M1, M2 ...) and Evening (E1, E2 ...), each with its own days,
     start time and sessions per day. Session length and gap are shared.
   - Two independent schedules: Weekdays (Mon-Fri) and Weekend (Sat-Sun), each with its own settings,
     subjects, teachers, Generate and Clear. Switch with `?schedule=weekday|weekend`.
   - Tables: remedial_config, remedial_slots (slot_number 1-8 morning, 11-18 evening), remedial_plan2,
     remedial_teachers2, remedial_entries, remedial_meta. Old v1 data is migrated once automatically.
   - Tested end to end against a throwaway SQLite database (not inside the real app).
3. **Lavender Glass theme** (this zip, `theme_lavender.zip`): `static/css/shulehub-theme.css`,
   `apply_theme.py` (adds/removes one <link> in templates/base.html; `--off` reverts), `preview.html`.
   Look taken from the user's video (TikTok "Dashboard V20"): lavender page, purple gradient navigation,
   cream panels, pastel glass stat cards (lavender, pink, yellow, blue), pill buttons.
   Palette: purple #8d74e0 -> #6a50c8, cream #fbf5ef, lavender bg #e2d9f6 -> #cfc2f0, ink #2d2547,
   pastels #e9e1fb #fbdfe4 #fdf1c9 #dbe8fb.

## Not done yet / next steps
- **Theme**: made without seeing the real `base.html` or the app's main stylesheet, so it re-points the
  existing CSS variables and styles generic elements (header, nav, .card, button, table, input).
  The video also shows a LEFT SIDEBAR with a profile avatar and a floating bottom pill navigation;
  those need the real markup. To finish: get `templates/base.html` and the main CSS file, then add
  the sidebar layout, stat-card classes and the hero banner to match the video. Check class names
  in base.html and adjust the selectors in shulehub-theme.css.
- **Library module** (requested, not started): books + copies (accession numbers), borrowers linked to
  existing students/teachers, issue/return with due dates and renewals, overdue list, fines and lost
  book charges, reports, CSV import, CSV/PDF exports with the school logo. Blocked on seeing
  `database.py` and the students module (table and column names). Build it as a blueprint
  (`library.py` + `templates/library/*`) with an apply script, like the timetable one.

## Files to give the next Claude
timetable_extras.zip, theme_lavender.zip, this HANDOFF.md, plus database.py, the students module,
templates/base.html and the main stylesheet if available.

## Working style the user likes
Deliver as a zip to unzip into the ShuleHub folder, with a helper script for any edits to app.py,
permissions.py or base.html (idempotent, writes .bak backups). Test before delivering.
