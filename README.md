# attendance-grades

A small [uv](https://docs.astral.sh/uv/) tool that turns an attendance report
CSV into per-section grades out of 5.

The default output is a coloured terminal table (green/yellow/red grades) with
one table per section, students sorted alphabetically, and a section average.

## Install

```bash
uv tool install .
```

For development, install it in editable mode so source changes take effect
immediately:

```bash
uv tool install --editable .
```

## Usage

```bash
attendance-grades myReport.csv
```

The default input file is `myReport.csv` in the current directory:

```bash
attendance-grades
```

### Options

| Option | Default | Description |
|---|---|---|
| `--excused` | `sick,bereavement,other,competition/event` | Comma-separated statuses treated as excused |
| `--include-excused` | off | Count excused absences against the student |
| `--lates-per-absence N` | `3` | How many lates equal one absence |
| `--max-grade N` | `5` | Maximum grade |
| `--decimals N` | `2` | Decimal places in the grade column (0–6) |
| `--sort` | `full-name` | `full-name` or `last-name` (last name, then rest of the name) |
| `--format` | `table` | `table`, `markdown`, or `csv` |
| `--width N` | auto | Table width in columns (`0` = auto-detect) |
| `--push-canvas` | off | Prompt for Canvas credentials and push grades |
| `--canvas-grade-mode` | `auto` | `auto`, `points`, or `percentage` |
| `--canvas-dry-run` | off | Preview Canvas matches without posting grades |
| `--canvas-show-archived` | off | Include archived/completed courses in the course list |

Examples:

```bash
# Export Markdown instead of the pretty terminal table
attendance-grades myReport.csv --format markdown

# Export CSV for a spreadsheet
attendance-grades myReport.csv --format csv > grades.csv

# Two lates = one absence, grades out of 10, one decimal place
attendance-grades myReport.csv --lates-per-absence 2 --max-grade 10 --decimals 1

# Count excused absences as well, and narrow the table for a small terminal
attendance-grades myReport.csv --include-excused --width 100

# Sort each section by last name, then the rest of the name
attendance-grades myReport.csv --sort last-name

# Preview Canvas matching and grades without posting anything
attendance-grades myReport.csv --push-canvas --canvas-dry-run

# Include archived/completed courses in the Canvas course picker
attendance-grades myReport.csv --push-canvas --canvas-show-archived
```

## Pushing grades to Canvas LMS

```bash
attendance-grades myReport.csv --push-canvas
```

The tool asks for your Canvas **base URL** (for example
`https://school.instructure.com`) and an **access token** (entered with hidden
input). It validates the token, lists your active teacher courses, and lets you
select one or more courses. Archived/completed courses are hidden by default;
use `--canvas-show-archived` if you need them. For each course it:

1. matches the report section (for example `CMPE 270L-2`) to the Canvas course,
   or asks you to choose one;
2. lists the course assignments and lets you choose the target;
3. matches every report student to a Canvas user by **GRN** against Canvas
   `sis_user_id`, `integration_id`, `login_id`, or `email` (Canvas login IDs are
   often the email address; the part before `@` is also matched);
4. shows a preview of matched students, unmatched students, and the grade that
   will be posted;
5. asks for confirmation, then posts all grades for the assignment in a single
   bulk request.

Students are never matched by name. If a GRN does not match, you can map the
student manually by entering their Canvas email or user ID.

`--canvas-grade-mode` controls how grades are posted:

* `auto` (default) – points when the assignment's maximum is the same as
  `--max-grade`, otherwise a percentage of the maximum;
* `points` – post the grade value directly;
* `percentage` – post `grade / max_grade * 100%`.

Canvas endpoints used:

| Purpose | Endpoint |
|---|---|
| Validate token | `GET /api/v1/users/self` |
| List courses | `GET /api/v1/courses?enrollment_type=teacher` |
| List assignments | `GET /api/v1/courses/:course_id/assignments` |
| Roster + email | `GET /api/v1/courses/:course_id/users?include[]=email` |
| Bulk grading | `POST /api/v1/courses/:course_id/assignments/:assignment_id/submissions/update_grades` |
| Grade job status | `GET /api/v1/progress/:id` |
| Per-student fallback | `PUT /api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id` |

The bulk endpoint is preferred; if a Canvas instance does not expose it, the
tool falls back to one request per student.

You can set `CANVAS_BASE_URL` and `CANVAS_ACCESS_TOKEN` in the environment to
avoid retyping them. The prompts still appear, but pressing Enter accepts the
environment values.

## Grading rule

```
effective_absences = absences + lates / lates_per_absence
sessions_counted   = delivered_sessions - excused_absences
grade              = max_grade * (1 - effective_absences / sessions_counted)
```

* 100% attendance → `5.00`, 50% attendance → `2.50` (with the defaults).
* Sessions are measured against the report's **Total Delivered Sessions**, not
  just the cells that happen to contain a status. A student with 4 absences out
  of 8 delivered sessions is at 50%, even if the remaining cells are blank.
* Every 3 lates count as 1 absence (each late costs `1/3` of an absence).
* Excused absences (`Sick`, `Bereavement`, `Other`, `Competition/event`, …) are
  removed from both the penalty and the denominator, so they neither help nor
  hurt the grade. Use `--include-excused` to penalise them instead.
* Blank session cells are ignored.
* Students are listed alphabetically within each section; sections are sorted
  naturally (`CMPE 270L-2` before `CMPE 270L-10`).

## Uninstall

```bash
uv tool uninstall attendance-grades
```
