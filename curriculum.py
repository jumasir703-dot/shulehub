"""
curriculum.py
Reference data for the Kenyan school levels this app supports, plus the
default grading scales used by the Exam Analysis module.

Nothing here touches the database directly; database.py and app.py import
these constants to seed grades/subjects/grading bands and to label things.

Curriculum codes stored in grades.curriculum
--------------------------------------------
  "PP"      Pre-Primary (PP1-PP2)
  "CBC-LP"  Lower Primary (Grades 1-3)
  "CBC-UP"  Upper Primary (Grades 4-6)
  "CBC-JS"  Junior Secondary (Grades 7-9)
  "CBC"     Senior School (Grade 10+, pathway-based)   <- existing code, unchanged
  "8-4-4"   Legacy 8-4-4 (Forms 3-4)                   <- existing code, unchanged

The code "CBC" is deliberately left meaning *Senior School*, because the
rest of the app uses it to decide whether to show the pathway field.
"""

CURRICULA = {
    "PP":     "Pre-Primary",
    "CBC-LP": "Lower Primary",
    "CBC-UP": "Upper Primary",
    "CBC-JS": "Junior Secondary",
    "CBC":    "Senior School (CBE)",
    "8-4-4":  "Legacy 8-4-4",
}

# Grades the app creates on first run / on upgrade: (name, curriculum, sort_order)
# Streams are NOT created for these -- a school adds streams only for the
# classes it actually runs, so nothing unwanted appears in the Timetable Builder.
DEFAULT_GRADES = [
    ("Grade 1",  "CBC-LP", 1),
    ("Grade 2",  "CBC-LP", 2),
    ("Grade 3",  "CBC-LP", 3),
    ("Grade 7",  "CBC-JS", 7),
    ("Grade 8",  "CBC-JS", 8),
    ("Grade 9",  "CBC-JS", 9),
]

# Sort order for the grades that already existed before this upgrade.
LEGACY_SORT_ORDER = {"Grade 10": 10, "Form 3": 11, "Form 4": 12}

# Default subjects per curriculum: (name, periods_per_week).
# These follow the CBC learning areas; the periods are sensible starting
# points (each level sums to <= 40 lessons/week on an 8-period, 5-day week)
# and are editable on the Subjects page.
LEVEL_SUBJECTS = {
    "PP": [
        ("Language Activities", 5),
        ("Mathematical Activities", 5),
        ("Environmental Activities", 4),
        ("Psychomotor & Creative Activities", 5),
        ("Religious Activities", 3),
    ],
    "CBC-LP": [
        ("English Language Activities", 5),
        ("Kiswahili Language Activities", 4),
        ("Indigenous Language Activities", 2),
        ("Mathematical Activities", 5),
        ("Environmental Activities", 4),
        ("Hygiene & Nutrition Activities", 2),
        ("Religious Education Activities", 3),
        ("Movement & Creative Activities", 5),
    ],
    "CBC-UP": [
        ("English", 5),
        ("Kiswahili", 4),
        ("Mathematics", 5),
        ("Science & Technology", 4),
        ("Agriculture & Nutrition", 3),
        ("Social Studies", 3),
        ("Religious Education", 3),
        ("Creative Arts", 3),
        ("Physical & Health Education", 3),
    ],
    "CBC-JS": [
        ("English", 5),
        ("Kiswahili / KSL", 4),
        ("Mathematics", 5),
        ("Integrated Science", 4),
        ("Health Education", 2),
        ("Pre-Technical Studies", 3),
        ("Social Studies", 3),
        ("Religious Education", 3),
        ("Business Studies", 3),
        ("Agriculture", 3),
        ("Life Skills Education", 1),
        ("Sports & Physical Education", 2),
        ("Creative Arts", 2),
    ],
}


# --------------------------------------------------------------------------
# Grading scales (Exam Analysis)
# --------------------------------------------------------------------------
# Every band: (label, min_percentage, points, remark). A mark falls in the
# highest band whose min_percentage it reaches. These are the *defaults*;
# schools can edit the thresholds/remarks on the Grading Scales page.
SCALES = {
    "KCSE": {
        "title": "8-4-4 (KCSE style, A - E, 12 points)",
        "pass_pct": 50,
        "bands": [
            ("A",  80, 12, "Excellent"),
            ("A-", 75, 11, "Very Good"),
            ("B+", 70, 10, "Very Good"),
            ("B",  65,  9, "Good"),
            ("B-", 60,  8, "Good"),
            ("C+", 55,  7, "Fair"),
            ("C",  50,  6, "Average"),
            ("C-", 45,  5, "Average"),
            ("D+", 40,  4, "Below Average"),
            ("D",  35,  3, "Weak"),
            ("D-", 30,  2, "Weak"),
            ("E",   0,  1, "Poor"),
        ],
    },
    "CBC8": {
        "title": "CBC (8 performance levels: EE1 - BE2)",
        "pass_pct": 41,   # ME2 and above = "meeting expectation"
        "bands": [
            ("EE1", 90, 8, "Exceptional"),
            ("EE2", 75, 7, "Very Good"),
            ("ME1", 58, 6, "Good"),
            ("ME2", 41, 5, "Fair"),
            ("AE1", 31, 4, "Needs Improvement"),
            ("AE2", 21, 3, "Below Average"),
            ("BE1", 11, 2, "Well Below Average"),
            ("BE2",  0, 1, "Minimal"),
        ],
    },
}

# What the two-letter prefix of a CBC band means, for report forms.
CBC_LEVEL_NAMES = {
    "EE": "Exceeding Expectation",
    "ME": "Meeting Expectation",
    "AE": "Approaching Expectation",
    "BE": "Below Expectation",
}


def scale_for_curriculum(curriculum):
    """Which grading scale a grade uses: legacy 8-4-4 uses KCSE-style
    letters; every CBC level (and pre-primary) uses the 8-level CBC scale."""
    return "KCSE" if curriculum == "8-4-4" else "CBC8"
