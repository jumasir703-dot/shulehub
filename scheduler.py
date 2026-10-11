"""
scheduler.py
Automatic timetable generator for the School Timetable Manager.

Design decisions (per Testy's answers):
- periods_per_week is set per-subject on the Subjects page.
- If a subject has no teacher assigned yet, it is skipped entirely.
- "Avoid same subject twice a day" is a global toggle in Scheduling Rules
  (generation_settings table).
- Two per-subject rules can be added from the Scheduling Rules page
  (subject_rules table):
    - "double_lesson": schedule N two-period-in-a-row blocks per week for
      this subject (e.g. Physics gets 2 double lessons/week). Pairs are
      only formed between periods that are genuinely back-to-back in time
      (period A's end_time == period B's start_time), so a lesson never
      gets paired across the mid-morning break.
    - "not_after_period": this subject can never be placed in a period
      whose period_number is greater than the given value (e.g. Mathematics
      "not after period 6" keeps it out of the last two periods of the day).
- Optional subject groups (subject_groups table): subjects like Business
  Studies / Computer Studies / Home Science / Agriculture that are taught
  at the same time, with each student picking exactly one, share a common
  group name and are scheduled as a single unit — the whole group gets
  `periods_per_week` slots, and every member subject lands in the SAME
  day+period as the others in its group (each with its own teacher/room),
  rather than being scheduled independently. Subjects belonging to a group
  are excluded from the normal per-subject scheduling loop below.

Algorithm: greedy with day-spread preference, teacher-conflict checking
across the whole school, and load-balancing when a subject has more than
one qualified teacher. Not a full constraint solver — if the data is very
tight, some slots may go unfilled; those are reported back so you know
what to fix (add a teacher, relax a rule, free up a period) rather than
silently failing.
"""

from collections import defaultdict

import database as db


def _get_available_periods():
    """All non-break periods, with day/period_number/start/end times."""
    return db.fetch_all(
        "SELECT id, day, period_number, start_time, end_time FROM periods "
        "WHERE is_break=0 ORDER BY period_number, day"
    )


def _teachers_for_subject(subject_id):
    rows = db.fetch_all(
        "SELECT teacher_id FROM teacher_subjects WHERE subject_id=?",
        (subject_id,),
    )
    return [r["teacher_id"] for r in rows]


def _build_adjacency(periods):
    """
    Map period_id -> the period_id immediately after it on the same day
    (only when truly back-to-back in time, i.e. one's end_time equals the
    next's start_time — so a pair is never formed across a break).
    """
    by_day = defaultdict(list)
    for p in periods:
        by_day[p["day"]].append(p)
    next_period = {}
    for day, plist in by_day.items():
        plist_sorted = sorted(plist, key=lambda p: p["period_number"])
        for a, b in zip(plist_sorted, plist_sorted[1:]):
            if a["end_time"] and a["end_time"] == b["start_time"]:
                next_period[a["id"]] = b["id"]
    return next_period


def generate_timetable(stream_ids=None, overwrite=False):
    """
    Fill in timetable_entries automatically.

    Returns a list of per-stream report dicts:
        {"stream": name, "scheduled": int, "skipped": [str, ...]}
    """
    settings = db.get_generation_settings()
    avoid_same_day = settings.get("avoid_same_day_repeat", "1") == "1"
    vary_period = settings.get("vary_period_slot", "1") == "1"

    if stream_ids:
        streams = [db.fetch_one("SELECT * FROM streams WHERE id=?", (sid,)) for sid in stream_ids]
        streams = [s for s in streams if s]
    else:
        streams = db.fetch_all("SELECT * FROM streams ORDER BY grade_id, name")

    all_periods = _get_available_periods()
    next_period = _build_adjacency(all_periods)

    # teacher_busy tracks every (teacher_id, period_id) already booked
    # anywhere in the school, so we never double-book a teacher.
    teacher_busy = set()
    for e in db.fetch_all("SELECT teacher_id, period_id FROM timetable_entries"):
        if e["teacher_id"]:
            teacher_busy.add((e["teacher_id"], e["period_id"]))

    report = []

    for stream in streams:
        stream_id = stream["id"]

        if overwrite:
            old = db.fetch_all(
                "SELECT teacher_id, period_id FROM timetable_entries WHERE stream_id=?",
                (stream_id,),
            )
            db.execute("DELETE FROM timetable_entries WHERE stream_id=?", (stream_id,))
            for o in old:
                if o["teacher_id"]:
                    teacher_busy.discard((o["teacher_id"], o["period_id"]))

        filled_period_ids = {
            r["period_id"]
            for r in db.fetch_all("SELECT period_id FROM timetable_entries WHERE stream_id=?", (stream_id,))
        }
        available = [p for p in all_periods if p["id"] not in filled_period_ids]

        groups = db.fetch_all(
            "SELECT * FROM subject_groups WHERE grade_id=? AND (pathway_id IS NULL OR pathway_id=?)",
            (stream["grade_id"], stream["pathway_id"]),
        )

        teacher_load = defaultdict(int)
        subject_days_used = defaultdict(set)
        subject_periods_used = defaultdict(set)
        stream_report = {"stream": stream["name"], "scheduled": 0, "skipped": []}

        for group in groups:
            members = db.get_group_members(group["id"])
            if not members:
                continue

            member_teachers = {}
            missing_teacher = False
            for m in members:
                teachers = _teachers_for_subject(m["id"])
                if not teachers:
                    stream_report["skipped"].append(
                        f"{group['name']} (group): {m['name']} has no teacher assigned, whole group skipped"
                    )
                    missing_teacher = True
                    break
                member_teachers[m["id"]] = teachers
            if missing_teacher:
                continue

            # If any member subject has a "not after period X" rule, the whole
            # group must respect the strictest (smallest) one, since every
            # member sits in the exact same slot.
            not_after = None
            for m in members:
                rules = db.get_subject_rules(m["id"])
                if rules["not_after_period"] is not None:
                    if not_after is None or rules["not_after_period"] < not_after:
                        not_after = rules["not_after_period"]

            needed = group["periods_per_week"] or 5
            placed = 0
            fallback_levels = [
                (avoid_same_day, vary_period),
                (avoid_same_day, False),
                (False, False),
            ]
            for level_avoid_same_day, level_vary_period in fallback_levels:
                if placed >= needed:
                    break
                got = _place_group(
                    group, members, member_teachers, needed - placed, available, not_after,
                    teacher_busy, teacher_load, subject_days_used, subject_periods_used, stream_id,
                    avoid_same_day=level_avoid_same_day, vary_period=level_vary_period,
                )
                placed += got

            stream_report["scheduled"] += placed * len(members)
            if placed < needed:
                note = f"{group['name']} (group): only {placed}/{needed} periods placed (no free teacher/slot left"
                note += f", or ruled out by 'not after period {not_after}')" if not_after is not None else ")"
                stream_report["skipped"].append(note)

        subjects = db.fetch_all(
            "SELECT * FROM subjects WHERE grade_id=? AND (pathway_id IS NULL OR pathway_id=?) "
            "AND group_id IS NULL "
            "ORDER BY periods_per_week DESC",
            (stream["grade_id"], stream["pathway_id"]),
        )

        for subject in subjects:
            needed = subject["periods_per_week"] or 5
            teachers = _teachers_for_subject(subject["id"])
            if not teachers:
                stream_report["skipped"].append(f"{subject['name']}: no teacher assigned, skipped")
                continue

            rules = db.get_subject_rules(subject["id"])
            not_after = rules["not_after_period"]
            doubles_needed = rules["double_lesson_count"]

            placed = 0

            doubles_placed = 0
            if doubles_needed > 0:
                doubles_placed = _place_doubles(
                    subject, teachers, doubles_needed, available, not_after,
                    next_period, teacher_busy, teacher_load, subject_days_used, stream_id,
                )
                placed += doubles_placed * 2
                if doubles_placed < doubles_needed:
                    stream_report["skipped"].append(
                        f"{subject['name']}: only {doubles_placed}/{doubles_needed} "
                        f"double lesson(s) placed (no free adjacent slot/teacher)"
                    )

            # Progressive fallback: try with both preferences on first (spread
            # across days AND across period-slots), then relax "vary period"
            # first since it's the softer preference, then relax "avoid same
            # day" too as a last resort so we still fill the week if it's tight.
            remaining = needed - placed
            fallback_levels = [
                (avoid_same_day, vary_period),
                (avoid_same_day, False),
                (False, False),
            ]
            for level_avoid_same_day, level_vary_period in fallback_levels:
                if remaining <= 0:
                    break
                got = _place_subject(
                    subject, teachers, remaining, available, not_after, teacher_busy, teacher_load,
                    subject_days_used, subject_periods_used, stream_id,
                    avoid_same_day=level_avoid_same_day, vary_period=level_vary_period,
                )
                placed += got
                remaining -= got

            stream_report["scheduled"] += placed
            if placed < needed:
                note = f"{subject['name']}: only {placed}/{needed} periods placed (no free teacher/slot left"
                note += f", or ruled out by 'not after period {not_after}')" if not_after is not None else ")"
                stream_report["skipped"].append(note)

        report.append(stream_report)

    return report


def _place_group(group, members, member_teachers, needed, available, not_after,
                  teacher_busy, teacher_load, subject_days_used, subject_periods_used, stream_id,
                  avoid_same_day, vary_period):
    """Place up to `needed` slots for an optional-subject group. Each slot is
    one period where EVERY member subject gets its own timetable_entries row
    (same stream, same period, different subject/teacher) — this is what
    makes them show up as parallel "pick one" options on the timetable.

    A candidate period only counts if every member subject can be given a
    teacher that's free at that period AND not already claimed by another
    member subject in that same slot (two subjects taught in parallel can't
    share one teacher). Members with fewer available teachers are tried
    first, since they're the most likely to fail and are cheapest to fail
    fast on.
    """
    group_key = f"group:{group['id']}"
    placed = 0
    for p in list(available):
        if placed >= needed:
            break
        if not_after is not None and p["period_number"] > not_after:
            continue
        if avoid_same_day and p["day"] in subject_days_used[group_key]:
            continue
        if vary_period and p["period_number"] in subject_periods_used[group_key]:
            continue

        ordered_members = sorted(members, key=lambda m: len(member_teachers[m["id"]]))
        assignment = {}  # subject_id -> teacher_id
        claimed_teachers = set()
        ok = True
        for m in ordered_members:
            chosen_teacher = None
            for t in sorted(member_teachers[m["id"]], key=lambda tid: teacher_load[tid]):
                if t in claimed_teachers:
                    continue
                if (t, p["id"]) not in teacher_busy:
                    chosen_teacher = t
                    break
            if chosen_teacher is None:
                ok = False
                break
            assignment[m["id"]] = chosen_teacher
            claimed_teachers.add(chosen_teacher)

        if not ok:
            continue

        for m in ordered_members:
            t = assignment[m["id"]]
            db.execute(
                "INSERT INTO timetable_entries (stream_id, subject_id, teacher_id, room_id, period_id) "
                "VALUES (?, ?, ?, NULL, ?)",
                (stream_id, m["id"], t, p["id"]),
            )
            teacher_busy.add((t, p["id"]))
            teacher_load[t] += 1

        subject_days_used[group_key].add(p["day"])
        subject_periods_used[group_key].add(p["period_number"])
        available.remove(p)
        placed += 1

    return placed


def _place_subject(subject, teachers, needed, available, not_after, teacher_busy, teacher_load,
                    subject_days_used, subject_periods_used, stream_id, avoid_same_day, vary_period):
    """Place up to `needed` single periods for a subject directly against the
    real school-wide `available` list (mutated in place as slots get used —
    there is only ever one such list per stream, so removals here are always
    correctly reflected for every other subject processed afterwards)."""
    placed = 0
    for p in list(available):
        if placed >= needed:
            break
        if not_after is not None and p["period_number"] > not_after:
            continue
        if avoid_same_day and p["day"] in subject_days_used[subject["id"]]:
            continue
        if vary_period and p["period_number"] in subject_periods_used[subject["id"]]:
            continue

        chosen_teacher = None
        for t in sorted(teachers, key=lambda tid: teacher_load[tid]):
            if (t, p["id"]) not in teacher_busy:
                chosen_teacher = t
                break
        if chosen_teacher is None:
            continue

        db.execute(
            "INSERT INTO timetable_entries (stream_id, subject_id, teacher_id, room_id, period_id) "
            "VALUES (?, ?, ?, NULL, ?)",
            (stream_id, subject["id"], chosen_teacher, p["id"]),
        )
        teacher_busy.add((chosen_teacher, p["id"]))
        teacher_load[chosen_teacher] += 1
        subject_days_used[subject["id"]].add(p["day"])
        subject_periods_used[subject["id"]].add(p["period_number"])
        available.remove(p)
        placed += 1

    return placed


def _place_doubles(subject, teachers, doubles_needed, available, not_after, next_period,
                    teacher_busy, teacher_load, subject_days_used, stream_id):
    """Place up to `doubles_needed` two-period-in-a-row blocks for a subject,
    directly against the real school-wide `available` list."""

    def is_eligible(period):
        return not_after is None or period["period_number"] <= not_after

    placed = 0
    for p in list(available):
        if placed >= doubles_needed:
            break
        if not is_eligible(p):
            continue
        second_id = next_period.get(p["id"])
        if second_id is None:
            continue
        second = next((a for a in available if a["id"] == second_id), None)
        if second is None or not is_eligible(second):
            continue  # partner slot already used elsewhere, or not eligible

        chosen_teacher = None
        for t in sorted(teachers, key=lambda tid: teacher_load[tid]):
            if (t, p["id"]) not in teacher_busy and (t, second_id) not in teacher_busy:
                chosen_teacher = t
                break
        if chosen_teacher is None:
            continue

        for slot in (p, second):
            db.execute(
                "INSERT INTO timetable_entries (stream_id, subject_id, teacher_id, room_id, period_id) "
                "VALUES (?, ?, ?, NULL, ?)",
                (stream_id, subject["id"], chosen_teacher, slot["id"]),
            )
            teacher_busy.add((chosen_teacher, slot["id"]))
            teacher_load[chosen_teacher] += 1
            available.remove(slot)
        subject_days_used[subject["id"]].add(p["day"])
        placed += 1

    return placed
