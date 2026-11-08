from __future__ import annotations

import io
import unittest

import httpx
from rich.console import Console

from attendance_grades.canvas import (
    CanvasClient,
    GradeConfig,
    auto_section,
    canvas_grade,
    expected_score,
    is_archived,
    match_students,
    verify_posted_grades,
)


class CanvasClientTests(unittest.TestCase):
    def test_pagination_follows_link_header(self) -> None:
        seen_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_pages.append(request.url.query.decode())
            if "page=2" in request.url.query.decode():
                return httpx.Response(200, json=[{"id": 2}])
            next_url = str(request.url.copy_with(query=b"page=2"))
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={"Link": f'<{next_url}>; rel="next"'},
            )

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual([item["id"] for item in client.paginate("/api/v1/courses")], [1, 2])

    def test_bulk_grade_update_uses_one_request(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(202, json={"id": 7, "workflow_state": "queued"})

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )
        kind, progress = client.update_grades(1, 2, {10: "3.13", 11: "5.00"})

        self.assertEqual(kind, "bulk")
        self.assertEqual(progress["id"], 7)
        self.assertEqual(len(requests), 1)
        self.assertIn("grade_data%5B10%5D%5Bposted_grade%5D=3.13", requests[0].content.decode())

    def test_grade_update_falls_back_to_individual_requests(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/update_grades"):
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"id": 1})

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )
        kind, _progress = client.update_grades(1, 2, {10: "3.13", 11: "5.00"})

        self.assertEqual(kind, "individual")
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(
            sorted(request.url.path for request in requests[1:]),
            [
                "/api/v1/courses/1/assignments/2/submissions/10",
                "/api/v1/courses/1/assignments/2/submissions/11",
            ],
        )

    def test_list_submission_scores(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"user_id": 10, "score": 4.0},
                    {"user_id": 11, "score": None},
                    {"user_id": None, "score": 1.0},
                ],
            )

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(
            client.list_submission_scores(1, 2),
            {10: 4.0, 11: None},
        )

    def test_wait_for_progress_times_out_without_raising(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 5, "workflow_state": "running"})

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )
        self.assertIsNone(client.wait_for_progress(5, timeout=0))

    def test_archived_courses_are_hidden_unless_requested(self) -> None:
        courses = [
            {"id": 1, "name": "Current", "workflow_state": "available"},
            {"id": 2, "name": "Archived", "workflow_state": "completed"},
            {"id": 3, "name": "Unpublished", "workflow_state": "unpublished"},
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=courses)

        client = CanvasClient(
            "https://canvas.example.edu",
            "token",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(
            {course["id"] for course in client.list_courses()},
            {1, 3},
        )
        self.assertEqual(
            {course["id"] for course in client.list_courses(include_archived=True)},
            {1, 2, 3},
        )
        self.assertTrue(is_archived(courses[1]))
        self.assertFalse(is_archived(courses[0]))


class MatchingTests(unittest.TestCase):
    def test_grn_matches_login_id_and_email_local_part(self) -> None:
        users = [
            {"id": 10, "name": "Student One", "login_id": "f2300072@ku.edu"},
            {"id": 11, "name": "Student Two", "sis_user_id": "F2400085"},
        ]
        rows = [
            {"name": "Student One", "grn": "F2300072"},
            {"name": "Student Two", "grn": "F2400085"},
            {"name": "Nobody", "grn": "X0000000"},
        ]

        matches, unmatched = match_students(rows, users)

        self.assertEqual([(row["grn"], user["id"]) for row, user, _label in matches], [
            ("F2300072", 10),
            ("F2400085", 11),
        ])
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0][0]["grn"], "X0000000")

    def test_duplicate_canvas_match_is_not_sent_twice(self) -> None:
        users = [{"id": 10, "name": "Shared", "login_id": "same@ku.edu"}]
        rows = [
            {"name": "One", "grn": "SAME"},
            {"name": "Two", "grn": "same"},
        ]

        matches, unmatched = match_students(rows, users)

        self.assertEqual(len(matches), 1)
        self.assertEqual(len(unmatched), 1)
        self.assertIn("already matched", unmatched[0][1])


class HelperTests(unittest.TestCase):
    def test_expected_score_for_points_and_percentage(self) -> None:
        self.assertEqual(expected_score("4.17", 5), 4.17)
        self.assertEqual(expected_score("80.00%", 5), 4.0)
        self.assertIsNone(expected_score("80.00%", None))

    def test_verification_reports_mismatches(self) -> None:
        class StubClient:
            def list_submission_scores(self, _course_id: int, _assignment_id: int):
                return {10: 4.0, 11: 3.0}

        planned = [
            ({"name": "Student One", "grn": "G1"}, {"id": 10}, "email", "4.00"),
            ({"name": "Student Two", "grn": "G2"}, {"id": 11}, "email", "5.00"),
        ]
        console = Console(file=io.StringIO(), width=120)

        mismatches = verify_posted_grades(
            console,
            StubClient(),  # type: ignore[arg-type]
            1,
            2,
            planned,
            5.0,
        )

        self.assertEqual(mismatches, 1)

    def test_verification_returns_none_when_unavailable(self) -> None:
        class StubClient:
            def list_submission_scores(self, _course_id: int, _assignment_id: int):
                return {}

        planned = [
            ({"name": "Student One", "grn": "G1"}, {"id": 10}, "email", "4.00"),
        ]
        console = Console(file=io.StringIO(), width=120)

        self.assertIsNone(
            verify_posted_grades(
                console,
                StubClient(),  # type: ignore[arg-type]
                1,
                2,
                planned,
                5.0,
            )
        )

    def test_auto_section_matches_section_code(self) -> None:
        course = {
            "name": "CMPE 270L-2 (Digital Systems Laboratory)",
            "course_code": "CMPE270L-2",
        }
        sections = [
            "CMPE 270L-1 (Digital Systems Laboratory)",
            "CMPE 270L-2 (Digital Systems Laboratory)",
        ]
        self.assertEqual(auto_section(course, sections), sections[1])

    def test_canvas_grade_modes(self) -> None:
        config = GradeConfig(max_grade=5, decimals=2)
        self.assertEqual(canvas_grade(4.0, "points", config), "4.00")
        self.assertEqual(canvas_grade(4.0, "percentage", config), "80.00%")


if __name__ == "__main__":
    unittest.main()
