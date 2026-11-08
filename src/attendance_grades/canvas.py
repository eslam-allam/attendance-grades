"""Push computed attendance grades to Canvas LMS.

Canvas endpoints used (all under the user's Canvas base URL):

* ``GET  /api/v1/users/self`` – validate the access token
* ``GET  /api/v1/courses`` – list active courses where the user is a teacher
* ``GET  /api/v1/courses/:course_id/assignments`` – list assignments
* ``GET  /api/v1/courses/:course_id/users`` with ``include[]=email`` – roster
* ``POST /api/v1/courses/:course_id/assignments/:assignment_id/submissions/update_grades``
  – bulk grade update (preferred)
* ``PUT  /api/v1/courses/:course_id/assignments/:assignment_id/submissions/:user_id``
  – per-student fallback when the bulk endpoint is unavailable
* ``GET  /api/v1/progress/:id`` – wait for the bulk job to finish

Students are matched to Canvas users by concrete identifiers only: the report
GRN against Canvas SIS ID / integration ID / login ID / email (including the
local part of an email), never by fuzzy name matching.  Anything left unmatched
can be mapped manually by Canvas email or user ID.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
import questionary
from questionary import Choice, Style
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .cli import GradeConfig, format_number

IDENTIFIER_FIELDS = (
    ("email", "email"),
    ("primary_email", "email"),
    ("login_id", "email/login ID"),
    ("sis_user_id", "SIS ID"),
    ("integration_id", "integration ID"),
)

SECTION_CODE_RE = re.compile(r"([A-Za-z]{2,}\s*\d+[A-Za-z]?\s*-\s*\d+)")
PROGRESS_STATES = {"completed", "failed"}
ARCHIVED_STATES = {"completed", "deleted"}


class CanvasError(RuntimeError):
    """A Canvas API request failed."""


class CanvasAuthError(CanvasError):
    """Canvas rejected the access token."""


class Cancelled(Exception):
    """The user cancelled an interactive prompt."""


QSTYLE = Style(
    [
        ("qmark", "fg:#00d7d7 bold"),
        ("question", "bold"),
        ("answer", "fg:#00d787 bold"),
        ("pointer", "fg:#00d7d7 bold"),
        ("highlighted", "fg:#00d7d7 bold"),
        ("selected", "fg:#00d787"),
        ("separator", "fg:#6c6c6c"),
        ("instruction", "fg:#6c6c6c"),
        ("text", ""),
        ("disabled", "fg:#6c6c6c italic"),
    ]
)


def ask(question):
    """Ask a questionary question, treating Esc/None as a cancellation."""
    answer = question.ask()
    if answer is None:
        raise Cancelled
    return answer


def normalize(value: Any) -> str:
    return str(value).strip().casefold()


def identity_tokens(value: Any) -> Iterator[tuple[str, str]]:
    """Yield normalized identifier aliases for one Canvas user field."""
    token = normalize(value)
    if not token:
        return
    yield token, ""
    if "@" in token:
        local_part = token.split("@", 1)[0]
        if local_part:
            yield local_part, "email local part"


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "attendance-grades",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CanvasClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return

        message = response.text
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error") or message
        except ValueError:
            pass

        if response.status_code == 401:
            raise CanvasAuthError(
                "Canvas rejected the access token (401). Check that the token is "
                "valid and belongs to this Canvas instance."
            )
        raise CanvasError(f"Canvas API error {response.status_code}: {message}")

    def get(self, path: str, params: Any = None) -> httpx.Response:
        response = self._client.get(path, params=params)
        self._raise_for_status(response)
        return response

    def get_json(self, path: str, params: Any = None) -> Any:
        return self.get(path, params).json()

    @staticmethod
    def _next_link(link_header: str | None) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            match = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
            if match:
                return match.group(1)
        return None

    def paginate(self, path: str, params: Any = None) -> Iterator[dict[str, Any]]:
        """Yield every item, following Canvas's Link: rel="next" headers."""
        url: str | None = path
        next_params = params
        while url:
            response = self.get(url, next_params)
            payload = response.json()
            if not isinstance(payload, list):
                raise CanvasError(f"Expected a list from {url}, got {type(payload).__name__}")
            yield from payload
            url = self._next_link(response.headers.get("link"))
            next_params = None

    # -- Canvas resources --------------------------------------------------

    def current_user(self) -> dict[str, Any]:
        return self.get_json("/api/v1/users/self")

    def list_courses(self, include_archived: bool = False) -> list[dict[str, Any]]:
        courses = list(
            self.paginate(
                "/api/v1/courses",
                {
                    "enrollment_type": "teacher",
                    "enrollment_state": "active",
                    "per_page": "100",
                    "include[]": "term",
                },
            )
        )
        if not include_archived:
            courses = [course for course in courses if not is_archived(course)]
        return sorted(courses, key=lambda c: (str(c.get("course_code") or ""), str(c.get("name") or "")))

    def list_assignments(self, course_id: int) -> list[dict[str, Any]]:
        assignments = list(
            self.paginate(
                f"/api/v1/courses/{course_id}/assignments",
                {"per_page": "100"},
            )
        )
        assignments = [a for a in assignments if a.get("published", True)]
        return sorted(assignments, key=lambda a: str(a.get("name") or "").casefold())

    def list_students(self, course_id: int) -> list[dict[str, Any]]:
        users = list(
            self.paginate(
                f"/api/v1/courses/{course_id}/users",
                {
                    "enrollment_type": "student",
                    "include[]": "email",
                    "per_page": "100",
                },
            )
        )

        # Some Canvas versions omit email on the course-users endpoint.  The
        # sections endpoint with include[]=students&include[]=email can fill it
        # in, so try that only when needed.
        if any(not (user.get("email") or user.get("primary_email")) for user in users):
            try:
                sections = list(
                    self.paginate(
                        f"/api/v1/courses/{course_id}/sections",
                        [
                            ("include[]", "students"),
                            ("include[]", "email"),
                            ("per_page", "100"),
                        ],
                    )
                )
            except CanvasError:
                sections = []

            emails: dict[Any, str] = {}
            for section in sections:
                for student in section.get("students") or []:
                    email = user_email(student)
                    if email:
                        emails[student.get("id")] = email
            for user in users:
                if not (user.get("email") or user.get("primary_email")) and emails.get(user.get("id")):
                    user["email"] = emails[user["id"]]

        return users

    def update_grades(
        self,
        course_id: int,
        assignment_id: int,
        grades: dict[int, str],
        *,
        max_workers: int = 4,
    ) -> tuple[str, dict[str, Any]]:
        """Post all grades at once; fall back to one request per student.

        Returns ``("bulk", progress)`` or ``("individual", {})``.
        """
        bulk_path = (
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}"
            "/submissions/update_grades"
        )
        data = {
            f"grade_data[{user_id}][posted_grade]": grade
            for user_id, grade in grades.items()
        }
        response = self._client.post(bulk_path, data=data)

        if response.status_code in (404, 405):
            self._post_grades_individually(
                course_id, assignment_id, grades, max_workers=max_workers
            )
            return "individual", {}

        self._raise_for_status(response)
        progress = response.json() if response.content else {}
        return "bulk", progress

    def _post_grades_individually(
        self,
        course_id: int,
        assignment_id: int,
        grades: dict[int, str],
        *,
        max_workers: int = 4,
    ) -> None:
        """Fallback for Canvas servers without the bulk grading endpoint."""
        errors: list[tuple[int, Exception]] = []

        def post(user_id: int, grade: str) -> None:
            response = self._client.put(
                f"/api/v1/courses/{course_id}/assignments/{assignment_id}"
                f"/submissions/{user_id}",
                data={"submission[posted_grade]": grade},
            )
            self._raise_for_status(response)

        workers = max(1, min(max_workers, len(grades)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(post, user_id, grade): user_id
                for user_id, grade in grades.items()
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - reported per student
                    errors.append((futures[future], exc))

        if errors:
            user_id, first_error = errors[0]
            raise CanvasError(
                f"{len(errors)} of {len(grades)} individual grade post(s) failed "
                f"(first: user {user_id}: {first_error})"
            )

    def wait_for_progress(
        self,
        progress_id: int,
        *,
        timeout: float = 60.0,
        interval: float = 0.5,
    ) -> dict[str, Any] | None:
        """Poll briefly for the background job; return None if it is still running."""
        deadline = time.monotonic() + timeout
        while True:
            progress = self.progress(progress_id)
            state = str(progress.get("workflow_state") or "")
            if state in PROGRESS_STATES:
                return progress
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def list_submission_scores(
        self,
        course_id: int,
        assignment_id: int,
    ) -> dict[int, float | None]:
        """Return {canvas_user_id: score} for an assignment, for verification."""
        submissions = self.paginate(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
            {"per_page": "100"},
        )
        scores: dict[int, float | None] = {}
        for submission in submissions:
            user_id = submission.get("user_id")
            if user_id is None:
                continue
            score = submission.get("score")
            try:
                scores[int(user_id)] = None if score is None else float(score)
            except (TypeError, ValueError):
                scores[int(user_id)] = None
        return scores

    def progress(self, progress_id: int) -> dict[str, Any]:
        return self.get_json(f"/api/v1/progress/{progress_id}")


def user_email(user: dict[str, Any]) -> str:
    """Return the user's email. Canvas login IDs are often the email address."""
    for field in ("email", "primary_email", "login_id"):
        value = str(user.get(field) or "").strip()
        if value:
            return value
    return ""


def is_archived(course: dict[str, Any]) -> bool:
    """Canvas uses workflow_state='completed' for archived/past courses."""
    state = str(course.get("workflow_state") or "").strip().casefold()
    return state in ARCHIVED_STATES


def user_name(user: dict[str, Any]) -> str:
    return str(user.get("name") or user.get("display_name") or user.get("short_name") or user.get("id"))


def build_user_index(users: Iterable[dict[str, Any]]) -> dict[str, list[tuple[dict[str, Any], str]]]:
    index: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for user in users:
        seen: set[str] = set()
        for field, label in IDENTIFIER_FIELDS:
            value = user.get(field)
            if not value:
                continue
            for token, alias in identity_tokens(value):
                if token in seen:
                    continue
                seen.add(token)
                index[token].append((user, label if not alias else alias))
    return index


def match_students(
    rows: Iterable[dict[str, Any]],
    users: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], str]], list[tuple[dict[str, Any], str]]]:
    """Match report rows to Canvas users by GRN, never by name."""
    index = build_user_index(users)
    matches: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    unmatched: list[tuple[dict[str, Any], str]] = []
    used_user_ids: set[Any] = set()

    for row in rows:
        key = normalize(row["grn"])
        candidates: dict[Any, tuple[dict[str, Any], str]] = {}
        for user, label in index.get(key, []):
            candidates.setdefault(user.get("id"), (user, label))

        if len(candidates) == 1:
            user, label = next(iter(candidates.values()))
            if user.get("id") in used_user_ids:
                unmatched.append((row, "Canvas user already matched to another student"))
            else:
                matches.append((row, user, label))
                used_user_ids.add(user.get("id"))
        elif not candidates:
            unmatched.append((row, "no GRN match in Canvas"))
        else:
            unmatched.append((row, "GRN matches multiple Canvas users"))

    return matches, unmatched


def resolve_user(
    answer: str,
    users: list[dict[str, Any]],
    index: dict[str, list[tuple[dict[str, Any], str]]],
    used_user_ids: set[Any],
) -> dict[str, Any] | None:
    answer = answer.strip()
    if not answer:
        return None

    if answer.isdigit():
        for user in users:
            if str(user.get("id")) == answer:
                return user if user.get("id") not in used_user_ids else None

    candidates: dict[Any, dict[str, Any]] = {}
    for user, _label in index.get(normalize(answer), []):
        candidates.setdefault(user.get("id"), user)
    if len(candidates) == 1:
        user = next(iter(candidates.values()))
        return user if user.get("id") not in used_user_ids else None
    return None


def section_code(section: str) -> str | None:
    match = SECTION_CODE_RE.search(section)
    if not match:
        return None
    return re.sub(r"[^a-z0-9]", "", match.group(1).casefold())


def normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def auto_section(course: dict[str, Any], section_names: list[str]) -> str | None:
    haystack = normalized_text(
        " ".join(
            str(course.get(field) or "")
            for field in ("name", "original_name", "course_code")
        )
    )
    by_code = [
        name
        for name in section_names
        if (code := section_code(name)) and code in haystack
    ]
    if len(by_code) == 1:
        return by_code[0]

    by_name = [
        name
        for name in section_names
        if normalized_text(name) in haystack or haystack in normalized_text(name)
    ]
    if len(by_name) == 1:
        return by_name[0]
    return None


# -- interactive helpers ---------------------------------------------------


def choose_many(
    console: Console,
    items: list[Any],
    title: str,
    formatter,
) -> list[Any]:
    console.print()
    choices = [Choice(formatter(item), value=item) for item in items]
    selected = ask(
        questionary.checkbox(
            title,
            choices=choices,
            style=QSTYLE,
            instruction="(type to filter, space to select, enter to confirm)",
            use_search_filter=True,
            use_jk_keys=False,
            validate=lambda picked: bool(picked) or "Select at least one course.",
        )
    )
    return list(selected)


def choose_one(
    console: Console,
    items: list[Any],
    title: str,
    formatter,
) -> Any:
    console.print()
    choices = [Choice(formatter(item), value=item) for item in items]
    return ask(
        questionary.select(
            title,
            choices=choices,
            style=QSTYLE,
            instruction="(type to filter, enter to select)",
            use_search_filter=True,
            use_jk_keys=False,
        )
    )


def course_label(course: dict[str, Any]) -> str:
    code = course.get("course_code") or course.get("id")
    return f"{code} — {course.get('name') or ''}"


def assignment_label(assignment: dict[str, Any]) -> str:
    points = assignment.get("points_possible")
    points_text = f"{points:g} pts" if isinstance(points, (int, float)) else "points unknown"
    due = assignment.get("due_at")
    due_text = f", due {due}" if due else ""
    return f"{assignment.get('name')} ({points_text}{due_text})"


def prompt_connection(console: Console) -> tuple[str, str]:
    env_url = os.environ.get("CANVAS_BASE_URL", "").strip()
    raw_url = ask(
        questionary.text(
            "Canvas base URL",
            default=env_url,
            instruction="e.g. https://school.instructure.com",
            style=QSTYLE,
            validate=lambda value: bool(value.strip()) or "Enter a Canvas URL.",
        )
    ).strip()

    if not re.match(r"^https?://", raw_url, re.IGNORECASE):
        raw_url = "https://" + raw_url
    base_url = raw_url.rstrip("/")

    if base_url.startswith("http://") and not re.match(
        r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", base_url
    ):
        console.print(
            "[yellow]Warning: the access token will be sent over plain HTTP.[/yellow]"
        )

    env_token = os.environ.get("CANVAS_ACCESS_TOKEN", "")
    token = ask(questionary.password("Canvas access token", style=QSTYLE)).strip()
    if not token and env_token:
        token = env_token.strip()
    if not token:
        raise CanvasError("an access token is required")

    return base_url, token


def grade_mode_for_assignment(
    console: Console,
    assignment: dict[str, Any],
    max_grade: float,
    requested: str,
) -> str:
    if requested != "auto":
        return requested

    points = assignment.get("points_possible")
    if not isinstance(points, (int, float)) or math.isclose(float(points), max_grade):
        return "points"

    console.print(
        f"[yellow]This assignment is out of {points:g} points, but the grades are "
        f"out of {max_grade:g}.[/yellow]"
    )
    choice = ask(
        questionary.select(
            "How should grades be posted?",
            choices=[
                Choice(f"Points out of {max_grade:g}", value="points"),
                Choice("Percentage of the maximum", value="percentage"),
            ],
            default="percentage",
            style=QSTYLE,
        )
    )
    return choice


def canvas_grade(grade: float, mode: str, config: GradeConfig) -> str:
    if mode == "percentage":
        percentage = grade / config.max_grade * 100
        return f"{percentage:.2f}%"
    return format_number(grade, config.decimals)


def expected_score(displayed: str, points_possible: Any) -> float | None:
    """Convert the grade string we posted into the score Canvas should store."""
    try:
        if displayed.endswith("%"):
            if not isinstance(points_possible, (int, float)):
                return None
            return float(displayed[:-1]) / 100 * float(points_possible)
        return float(displayed)
    except (TypeError, ValueError):
        return None


def verify_posted_grades(
    console: Console,
    client: CanvasClient,
    course_id: int,
    assignment_id: int,
    planned: list[tuple[dict[str, Any], dict[str, Any], str, str]],
    points_possible: Any,
) -> int | None:
    """Read the grades back from Canvas and report any that did not land.

    Returns the number of mismatches, or ``None`` if verification could not run.
    """
    try:
        with console.status("Reading grades back from Canvas…", spinner="dots"):
            actual = client.list_submission_scores(course_id, assignment_id)
    except CanvasError as exc:
        console.print(f"[yellow]Could not verify grades: {exc}[/yellow]")
        return None

    if not actual:
        console.print("[yellow]Canvas returned no submission rows to verify against.[/yellow]")
        return None

    mismatches: list[tuple[dict[str, Any], float, float | None]] = []
    for row, user, _label, displayed in planned:
        expected = expected_score(displayed, points_possible)
        if expected is None:
            continue
        got = actual.get(int(user["id"]))
        if got is None or abs(got - expected) > 0.01:
            mismatches.append((row, expected, got))

    if not mismatches:
        console.print(f"[green]Verified {len(planned)} grade(s) in Canvas.[/green]")
        return 0

    console.print(
        f"[yellow]Canvas confirmed {len(planned) - len(mismatches)} of "
        f"{len(planned)} grade(s). Not confirmed:[/yellow]"
    )
    for row, expected, got in mismatches:
        shown = "no score" if got is None else f"{got:g}"
        console.print(
            f"  • {row['name']} ({row['grn']}): expected {expected:g}, "
            f"Canvas shows {shown}"
        )
    return len(mismatches)


# -- main flow -------------------------------------------------------------


def _push_grades(
    grades_by_section: dict[str, list[dict[str, Any]]],
    config: GradeConfig,
    *,
    console: Console | None = None,
    dry_run: bool = False,
    grade_mode: str = "auto",
    show_archived: bool = False,
    wait_timeout: float = 60.0,
    concurrency: int = 4,
    verify: bool = True,
) -> int:
    console = console or Console()
    section_names = list(grades_by_section)

    console.print(
        Panel.fit(
            "[bold cyan]Canvas LMS grade push[/bold cyan]\n"
            f"[dim]{len(section_names)} report section(s) loaded[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    try:
        base_url, token = prompt_connection(console)
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 1

    with CanvasClient(base_url, token) as client:
        try:
            with console.status("Connecting to Canvas...", spinner="dots"):
                me = client.current_user()
        except CanvasAuthError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        except CanvasError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

        console.print(
            f"[green]Authenticated as[/green] {me.get('name') or me.get('short_name') or me.get('id')} "
            f"({me.get('login_id') or me.get('email') or 'no login shown'})"
        )

        try:
            with console.status("Loading Canvas courses...", spinner="dots"):
                courses = client.list_courses(include_archived=show_archived)
        except CanvasError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

        if not courses and not show_archived:
            include_archived = ask(
                questionary.confirm(
                    "No active courses found. Include archived/completed courses?",
                    default=False,
                    style=QSTYLE,
                )
            )
            if include_archived:
                try:
                    with console.status("Reloading courses...", spinner="dots"):
                        courses = client.list_courses(include_archived=True)
                except CanvasError as exc:
                    console.print(f"[red]{exc}[/red]")
                    return 1
                show_archived = True

        if not courses:
            console.print("[red]No courses found where you are a teacher.[/red]")
            return 1

        if not show_archived:
            console.print(
                "[dim]Archived/completed courses are hidden; "
                "use --canvas-show-archived to include them.[/dim]"
            )

        try:
            selected_courses = choose_many(console, courses, "Your Canvas courses", course_label)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Cancelled.[/yellow]")
            return 1

        pushed = 0
        confirmed = 0
        failures = 0
        verification_failures = 0

        for course in selected_courses:
            course_id = int(course["id"])
            console.rule(f"[bold]{course_label(course)}[/bold]")

            section = auto_section(course, section_names)
            if section is None:
                if len(section_names) == 1:
                    section = section_names[0]
                    console.print(f"Using report section: [cyan]{section}[/cyan]")
                else:
                    try:
                        section = choose_one(
                            console,
                            section_names,
                            "Which report section belongs to this course?",
                            lambda name: name,
                        )
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[yellow]Cancelled.[/yellow]")
                        return 1
            else:
                console.print(f"Matched report section: [cyan]{section}[/cyan]")

            rows = grades_by_section[section]

            try:
                with console.status("Loading assignments...", spinner="dots"):
                    assignments = client.list_assignments(course_id)
            except CanvasError as exc:
                console.print(f"[red]{exc}[/red]")
                failures += 1
                continue

            if not assignments:
                console.print("[red]No published assignments found in this course.[/red]")
                failures += 1
                continue

            try:
                assignment = choose_one(
                    console,
                    assignments,
                    "Target assignment",
                    assignment_label,
                )
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Cancelled.[/yellow]")
                return 1

            try:
                with console.status("Loading course roster...", spinner="dots"):
                    users = client.list_students(course_id)
            except CanvasError as exc:
                console.print(f"[red]{exc}[/red]")
                failures += 1
                continue

            index = build_user_index(users)
            matches, unmatched = match_students(rows, users)

            if unmatched:
                console.print(f"[yellow]{len(unmatched)} student(s) could not be matched by GRN:[/yellow]")
                for row, reason in unmatched:
                    console.print(f"  • {row['name']} ({row['grn']}): {reason}")

                manual = ask(
                    questionary.confirm(
                        "Map unmatched students manually by email/user ID?",
                        default=True,
                        style=QSTYLE,
                    )
                )

                if manual:
                    used_ids = {user.get("id") for _row, user, _label in matches}
                    remaining = []
                    for row, reason in unmatched:
                        raw = ask(
                            questionary.text(
                                f"Canvas email or user ID for {row['name']} ({row['grn']})",
                                default="",
                                instruction="(blank to skip)",
                                style=QSTYLE,
                            )
                        )
                        user = resolve_user(raw, users, index, used_ids)
                        if user is None:
                            if raw.strip():
                                console.print("  [red]No unique Canvas user found; skipping.[/red]")
                            remaining.append((row, reason))
                        else:
                            matches.append((row, user, "manual"))
                            used_ids.add(user.get("id"))
                    unmatched = remaining

            mode = grade_mode_for_assignment(
                console,
                assignment,
                config.max_grade,
                grade_mode,
            )

            planned: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
            for row, user, label in matches:
                displayed = canvas_grade(float(row["grade"]), mode, config)
                planned.append((row, user, label, displayed))

            preview = Table(
                title=f"{section} → {assignment.get('name')}",
                title_style="bold cyan",
                title_justify="left",
                box=box.ROUNDED,
                header_style="bold white",
                border_style="bright_black",
            )
            preview.add_column("Student", max_width=34)
            preview.add_column("GRN", no_wrap=True)
            preview.add_column("Canvas user")
            preview.add_column("Email / login")
            preview.add_column("Matched by")
            preview.add_column("Grade", justify="right")
            for row, user, label, displayed in planned:
                preview.add_row(
                    str(row["name"]),
                    str(row["grn"]),
                    user_name(user),
                    user_email(user) or "[dim]—[/dim]",
                    label,
                    displayed,
                )
            console.print(preview)
            console.print(
                f"Matched [green]{len(planned)}[/green] of {len(rows)} students; "
                f"[yellow]{len(unmatched)}[/yellow] unmatched."
            )

            if not planned:
                console.print("[red]Nothing to push for this course.[/red]")
                failures += 1
                continue

            if dry_run:
                console.print("[yellow]Dry run: grades were not posted.[/yellow]")
                continue

            approved = ask(
                questionary.confirm(
                    f"Push {len(planned)} grade(s) to “{assignment.get('name')}”?",
                    default=False,
                    style=QSTYLE,
                )
            )
            if not approved:
                console.print("Skipped.")
                continue

            payload = {
                int(user["id"]): displayed
                for _row, user, _label, displayed in planned
            }

            console.print(
                f"\n[bold]Posting[/bold] {len(payload)} grade(s) to "
                f"[cyan]{assignment.get('name')}[/cyan]…"
            )

            try:
                with console.status("Sending the grade request to Canvas…", spinner="dots"):
                    kind, progress = client.update_grades(
                        course_id,
                        int(assignment["id"]),
                        payload,
                        max_workers=concurrency,
                    )

                job_completed = False
                if kind == "bulk":
                    progress_id = progress.get("id")
                    console.print(
                        f"[green]✓ Canvas accepted the bulk grade request[/green] "
                        f"for {len(payload)} student(s)."
                    )

                    if progress_id is not None and wait_timeout > 0:
                        console.print(
                            f"[bold]Waiting[/bold] up to [cyan]{wait_timeout:g}s[/cyan] "
                            "for Canvas to apply the grades…"
                        )
                        with console.status(
                            "Waiting for Canvas to finish the grade job…",
                            spinner="dots",
                        ):
                            final = client.wait_for_progress(
                                int(progress_id), timeout=wait_timeout
                            )

                        if final is None:
                            console.print(
                                f"[yellow]⚠ Canvas is still applying the grades after "
                                f"{wait_timeout:g}s.[/yellow] They will finish in the "
                                "background; verification was skipped. Increase "
                                "--canvas-wait to wait longer."
                            )
                        elif str(final.get("workflow_state")) == "failed":
                            message = final.get("message") or "no details"
                            raise CanvasError(f"bulk grade job failed: {message}")
                        else:
                            console.print(
                                "[green]✓ Canvas finished applying the grades.[/green]"
                            )
                            job_completed = True
                    else:
                        console.print(
                            "[dim]Not waiting for Canvas to finish "
                            "(--canvas-wait 0).[/dim]"
                        )
                else:
                    console.print(
                        f"[green]✓ Posted {len(payload)} grade(s) individually "
                        f"using {concurrency} worker(s)[/green] "
                        "(the bulk endpoint is unavailable on this Canvas)."
                    )
                    job_completed = True

                pushed += len(payload)

                if verify:
                    points_possible = assignment.get("points_possible")
                    if not job_completed:
                        console.print(
                            "[yellow]Verification skipped because Canvas is still "
                            "processing the grades.[/yellow]"
                        )
                    elif mode == "percentage" and not isinstance(
                        points_possible, (int, float)
                    ):
                        console.print(
                            "[yellow]Verification skipped: the assignment's points are unknown.[/yellow]"
                        )
                    else:
                        console.print(
                            f"[bold]Verifying[/bold] {len(planned)} grade(s) "
                            "against Canvas…"
                        )
                        mismatches = verify_posted_grades(
                            console,
                            client,
                            course_id,
                            int(assignment["id"]),
                            planned,
                            points_possible,
                        )
                        if mismatches is not None:
                            confirmed += len(planned) - mismatches
                            verification_failures += mismatches
            except CanvasError as exc:
                console.print(f"[red]✗ {exc}[/red]")
                failures += 1

        summary = f"Grades posted: [green]{pushed}[/green]"
        if confirmed:
            summary += f"\nGrades confirmed in Canvas: [green]{confirmed}[/green]"
        if verification_failures:
            summary += (
                f"\nGrades not confirmed by Canvas: "
                f"[yellow]{verification_failures}[/yellow]"
            )
        if failures:
            summary += f"\nCourses with errors: [red]{failures}[/red]"
        console.print(
            Panel.fit(summary, title="Summary", border_style="cyan", padding=(0, 2))
        )
        return 0 if failures == 0 and verification_failures == 0 else 1


def push_grades(
    grades_by_section: dict[str, list[dict[str, Any]]],
    config: GradeConfig,
    *,
    console: Console | None = None,
    dry_run: bool = False,
    grade_mode: str = "auto",
    show_archived: bool = False,
    wait_timeout: float = 60.0,
    concurrency: int = 4,
    verify: bool = True,
) -> int:
    console = console or Console()
    try:
        return _push_grades(
            grades_by_section,
            config,
            console=console,
            dry_run=dry_run,
            grade_mode=grade_mode,
            show_archived=show_archived,
            wait_timeout=wait_timeout,
            concurrency=concurrency,
            verify=verify,
        )
    except Cancelled:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 1
