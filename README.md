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
| `--format` | `table` | `table`, `markdown`, or `csv` |
| `--width N` | auto | Table width in columns (`0` = auto-detect) |

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
```

## Grading rule

```
effective_absences = absences + lates / lates_per_absence
sessions_counted   = present + absences + lates
grade              = max_grade * (1 - effective_absences / sessions_counted)
```

* 100% attendance → `5.00`, 50% attendance → `2.50` (with the defaults).
* Every 3 lates count as 1 absence (each late costs `1/3` of an absence).
* Excused absences (`Sick`, `Bereavement`, `Other`, `Competition/event`, …) are
  excluded from both the penalty and the denominator, so they neither help nor
  hurt the grade. Use `--include-excused` to penalise them instead.
* Blank session cells are ignored.
* Students are listed alphabetically within each section; sections are sorted
  naturally (`CMPE 270L-2` before `CMPE 270L-10`).

## Uninstall

```bash
uv tool uninstall attendance-grades
```
