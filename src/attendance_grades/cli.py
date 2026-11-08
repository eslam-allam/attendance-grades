"""Command-line interface for the attendance grade calculator.

Grading rule
------------
    effective_absences = absences + lates / lates_per_absence
    sessions_counted   = delivered_sessions - excused_absences
    grade              = max_grade * (1 - effective_absences / sessions_counted)

Sessions are measured against the report's "Total Delivered Sessions", so blank
cells do not magically turn an absence into 100% absence.  Excused absences
(Sick, Bereavement, Other, Competition/event, ...) are removed from the
denominator as well as from the penalty.  Blank cells are ignored.  Every third
late therefore turns into one absence (unless ``--lates-per-absence`` is
changed).

The formula's knobs are exposed on the command line; see ``--help``.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import shutil
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

GRN_RE = re.compile(r"^[A-Za-z]\d{6,}$")
SECTION_RE = re.compile(r"^Course/Section:\s*([^,]+)")

# Statuses that mean "absent, but excused".  They do not affect the grade.
DEFAULT_EXCUSED = ("sick", "bereavement", "other", "competition/event")

# Session status columns start here (after Section Name).
FIRST_SESSION_COLUMN = 10


@dataclass(frozen=True)
class GradeConfig:
    lates_per_absence: float = 3.0
    max_grade: float = 5.0
    include_excused: bool = False
    decimals: int = 2


def parse_report(path: Path) -> dict[str, list[dict[str, object]]]:
    """Return {section name: [student, ...]} with per-session statuses."""
    sections: dict[str, list[dict[str, object]]] = defaultdict(list)
    current_section: str | None = None

    with path.open(newline="", encoding="utf-8-sig") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\r\n")

            header = SECTION_RE.match(line)
            if header:
                current_section = header.group(1).strip()
                sections[current_section]  # create the key, keep insertion order
                continue

            if current_section is None or not line.strip():
                continue

            row = next(csv.reader([line]))
            if len(row) < 8:
                continue

            name = row[0].strip()
            grn = row[1].strip()
            if not name or not GRN_RE.match(grn):
                # Title rows, column headers, trailing separator rows, ...
                continue

            def number(index: int) -> int:
                try:
                    return int(float(row[index].strip()))
                except (ValueError, IndexError):
                    return 0

            sections[current_section].append(
                {
                    "name": name,
                    "grn": grn,
                    "delivered": number(3),  # Total Delivered Sessions
                    "statuses": [s for s in (cell.strip() for cell in row[FIRST_SESSION_COLUMN:]) if s],
                }
            )

    return dict(sections)


def categorize(statuses: list[str], excused: set[str]) -> dict[str, int]:
    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}

    for status in statuses:
        key = status.casefold()
        if key.startswith("present"):
            counts["present"] += 1
        elif key == "late":
            counts["late"] += 1
        elif key in ("absent", "absence"):
            counts["absent"] += 1
        elif key in excused:
            counts["excused"] += 1
        else:
            # Any other recorded status is treated as an excused absence
            # rather than silently punishing the student.
            counts["excused"] += 1

    return counts


def calculate_grade(
    counts: dict[str, int],
    delivered: int,
    config: GradeConfig,
) -> dict[str, float | int]:
    present = counts["present"]
    absences = counts["absent"]
    lates = counts["late"]
    excused = counts["excused"]

    # Grade against the sessions that were actually delivered.  Excused
    # absences are removed from both sides of the ratio unless the caller
    # explicitly asks to count them.
    sessions = delivered if delivered > 0 else present + absences + lates
    if config.include_excused:
        absences += excused
    else:
        sessions -= excused
    sessions = max(sessions, 0)

    effective_absences = absences + lates / config.lates_per_absence

    if sessions <= 0:
        rate = 1.0
    else:
        rate = 1.0 - effective_absences / sessions
    rate = min(max(rate, 0.0), 1.0)

    return {
        "sessions": sessions,
        "effective_absences": effective_absences,
        "attendance_pct": rate * 100,
        "grade": rate * config.max_grade,
    }


def section_sort_key(section: str) -> list[object]:
    """Natural sort so CMPE 270L-2 comes before CMPE 270L-10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", section)]


def student_sort_key(name: str, mode: str) -> tuple[str, ...]:
    """Sort by full name, or by last name then first name."""
    parts = name.split()
    if mode == "last-name" and parts:
        return (parts[-1].casefold(), " ".join(parts[:-1]).casefold(), name.casefold())
    return (name.casefold(),)


def iter_rows(
    sections: dict[str, list[dict[str, object]]],
    excused: set[str],
    config: GradeConfig,
    sort_by: str = "full-name",
) -> Iterator[dict[str, object]]:
    """Yield one fully calculated row per student, sections/names sorted."""
    for section in sorted(sections, key=section_sort_key):
        students = sorted(
            sections[section],
            key=lambda s: student_sort_key(str(s["name"]), sort_by),
        )
        for student in students:
            counts = categorize(student["statuses"], excused)  # type: ignore[arg-type]
            delivered = int(student["delivered"])  # type: ignore[arg-type]
            result = calculate_grade(counts, delivered, config)
            yield {
                "section": section,
                "name": student["name"],
                "grn": student["grn"],
                "delivered": delivered,
                **counts,
                **result,
            }


def format_number(value: float, decimals: int) -> str:
    quantum = Decimal(1).scaleb(-decimals)
    return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def grade_style(grade: float, max_grade: float) -> str:
    """Colour grades green/yellow/red depending on how close to the maximum."""
    ratio = grade / max_grade if max_grade else 0.0
    if ratio >= 0.9:
        return "bold green"
    if ratio >= 0.6:
        return "bold yellow"
    return "bold red"


def render_table(
    sections: dict[str, list[dict[str, object]]],
    excused: set[str],
    config: GradeConfig,
    console: Console,
    sort_by: str = "full-name",
) -> None:
    for section in sorted(sections, key=section_sort_key):
        rows = list(iter_rows({section: sections[section]}, excused, config, sort_by))

        table = Table(
            box=box.ROUNDED,
            header_style="bold white",
            border_style="bright_black",
        )
        table.add_column("Student Name", style="white", max_width=40)
        table.add_column("GRN", style="dim", no_wrap=True)
        table.add_column("Absences", justify="right", no_wrap=True)
        table.add_column("Excused", justify="right", no_wrap=True)
        table.add_column("Lates", justify="right", no_wrap=True)
        table.add_column("Attendance", justify="right", no_wrap=True)
        table.add_column(f"Grade /{config.max_grade:g}", justify="right", no_wrap=True)

        for row in rows:
            grade_value = float(row["grade"])
            table.add_row(
                str(row["name"]),
                str(row["grn"]),
                str(row["absent"]),
                str(row["excused"]),
                str(row["late"]),
                f"{float(row['attendance_pct']):.1f}%",
                Text(
                    format_number(grade_value, config.decimals),
                    style=grade_style(grade_value, config.max_grade),
                ),
            )

        average = sum(float(row["grade"]) for row in rows) / len(rows) if rows else 0.0
        table.caption = (
            f"{len(rows)} students  ·  section average "
            f"{format_number(average, config.decimals)}/{config.max_grade:g}"
        )
        table.caption_justify = "left"
        table.caption_style = "dim"
        console.print(f"[bold cyan]{section}[/bold cyan]")
        console.print(table)
        console.print()


def render_markdown(
    sections: dict[str, list[dict[str, object]]],
    excused: set[str],
    config: GradeConfig,
    sort_by: str = "full-name",
) -> str:
    lines: list[str] = []
    grade_header = f"Grade /{config.max_grade:g}"

    for section in sorted(sections, key=section_sort_key):
        lines.append(f"### {section}")
        lines.append("")
        lines.append(
            "| Student Name | GRN | Delivered | Counted | Present | Absences | Excused | Lates "
            f"| Effective Absences | Attendance % | {grade_header} |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

        for row in iter_rows({section: sections[section]}, excused, config, sort_by):
            grade = format_number(float(row["grade"]), config.decimals)
            lines.append(
                "| {name} | {grn} | {delivered} | {sessions} | {present} | {absent} | {excused} | {late} "
                "| {effective:.2f} | {pct:.1f}% | {grade} |".format(
                    name=row["name"],
                    grn=row["grn"],
                    delivered=row["delivered"],
                    sessions=row["sessions"],
                    present=row["present"],
                    absent=row["absent"],
                    excused=row["excused"],
                    late=row["late"],
                    effective=row["effective_absences"],
                    pct=row["attendance_pct"],
                    grade=grade,
                )
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_csv(
    sections: dict[str, list[dict[str, object]]],
    excused: set[str],
    config: GradeConfig,
    sort_by: str = "full-name",
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Section",
            "Student Name",
            "GRN",
            "Delivered",
            "Counted",
            "Present",
            "Absences",
            "Excused",
            "Lates",
            "Effective Absences",
            "Attendance %",
            f"Grade /{config.max_grade:g}",
        ]
    )

    for row in iter_rows(sections, excused, config, sort_by):
        writer.writerow(
            [
                row["section"],
                row["name"],
                row["grn"],
                row["delivered"],
                row["sessions"],
                row["present"],
                row["absent"],
                row["excused"],
                row["late"],
                f"{float(row['effective_absences']):.2f}",
                f"{float(row['attendance_pct']):.1f}",
                format_number(float(row["grade"]), config.decimals),
            ]
        )

    return buffer.getvalue()


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def decimal_places(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if not 0 <= number <= 6:
        raise argparse.ArgumentTypeError("must be between 0 and 6")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def positive_int(value: str) -> int:
    number = non_negative_int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return number


def terminal_width(override: int) -> int:
    if override:
        return override
    return max(shutil.get_terminal_size(fallback=(140, 24)).columns, 60)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="attendance-grades",
        description=(
            "Compute per-section attendance grades from an attendance report. "
            "By default: Grade = 5 * (1 - (absences + lates/3) / delivered sessions), "
            "with excused absences excluded."
        ),
    )
    parser.add_argument(
        "report",
        nargs="?",
        default="myReport.csv",
        type=Path,
        help="attendance CSV (default: myReport.csv)",
    )
    parser.add_argument(
        "--excused",
        default=",".join(DEFAULT_EXCUSED),
        help="comma-separated statuses treated as excused (default: %(default)s)",
    )
    parser.add_argument(
        "--lates-per-absence",
        type=positive_float,
        default=3.0,
        metavar="N",
        help="how many lates equal one absence (default: %(default)g)",
    )
    parser.add_argument(
        "--max-grade",
        type=positive_float,
        default=5.0,
        metavar="N",
        help="maximum grade (default: %(default)g)",
    )
    parser.add_argument(
        "--decimals",
        type=decimal_places,
        default=2,
        metavar="N",
        help="decimal places in the grade column, 0-6 (default: %(default)s)",
    )
    parser.add_argument(
        "--include-excused",
        action="store_true",
        help="count excused absences against the student (off by default)",
    )
    parser.add_argument(
        "--sort",
        choices=("full-name", "last-name"),
        default="full-name",
        help="student sort order: full name, or last name then rest of name (default: %(default)s)",
    )
    parser.add_argument(
        "--format",
        choices=("table", "markdown", "csv"),
        default="table",
        help="output format (default: %(default)s)",
    )
    parser.add_argument(
        "--width",
        type=non_negative_int,
        default=0,
        metavar="N",
        help="table width in columns; 0 means auto-detect (default: 0)",
    )
    parser.add_argument(
        "--push-canvas",
        action="store_true",
        help="prompt for Canvas URL/token and push grades to Canvas",
    )
    parser.add_argument(
        "--canvas-grade-mode",
        choices=("auto", "points", "percentage"),
        default="auto",
        help=(
            "how to post grades: points, percentage, or auto "
            "(points when the assignment is out of the max grade, otherwise percentage)"
        ),
    )
    parser.add_argument(
        "--canvas-dry-run",
        action="store_true",
        help="run the Canvas flow and preview matches without posting grades",
    )
    parser.add_argument(
        "--canvas-show-archived",
        action="store_true",
        help="include archived/completed courses in the Canvas course list",
    )
    parser.add_argument(
        "--canvas-wait",
        type=non_negative_float,
        default=60.0,
        metavar="SECONDS",
        help=(
            "how long to wait for Canvas to finish a bulk grade job; "
            "0 means fire-and-forget (default: %(default)g)"
        ),
    )
    parser.add_argument(
        "--canvas-concurrency",
        type=positive_int,
        default=4,
        metavar="N",
        help=(
            "parallel requests when falling back to per-student grading "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--canvas-verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="read grades back from Canvas after posting (default: on)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.report.is_file():
        parser.error(f"file not found: {args.report}")

    excused = {part.strip().casefold() for part in args.excused.split(",") if part.strip()}
    config = GradeConfig(
        lates_per_absence=args.lates_per_absence,
        max_grade=args.max_grade,
        include_excused=args.include_excused,
        decimals=args.decimals,
    )

    sections = parse_report(args.report)
    if not sections:
        print("No attendance data found in the report.", file=sys.stderr)
        return 1

    console = Console(width=terminal_width(args.width))

    if args.push_canvas:
        from .canvas import CanvasError, push_grades

        grades_by_section = {
            section: list(iter_rows({section: rows}, excused, config, args.sort))
            for section, rows in sections.items()
        }
        try:
            return push_grades(
                grades_by_section,
                config,
                console=console,
                dry_run=args.canvas_dry_run,
                grade_mode=args.canvas_grade_mode,
                show_archived=args.canvas_show_archived,
                wait_timeout=args.canvas_wait,
                concurrency=args.canvas_concurrency,
                verify=args.canvas_verify,
            )
        except CanvasError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Cancelled.[/yellow]")
            return 1

    if args.format == "csv":
        print(render_csv(sections, excused, config, args.sort), end="")
    elif args.format == "markdown":
        print(render_markdown(sections, excused, config, args.sort), end="")
    else:
        render_table(
            sections,
            excused,
            config,
            console,
            args.sort,
        )
    return 0
