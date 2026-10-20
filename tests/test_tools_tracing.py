"""Verify tools.dispatch_tool records correct span status for every outcome.

Run from the project root:
    uv run python -m unittest discover -s tests -t .
"""
import unittest
from unittest import mock

import requests

import tools
import tracer


def _response(status_code, body=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = body or {}
    return resp


class DispatchToolSpanStatusTests(unittest.TestCase):
    def _dispatch(self, name="get_booking", response=None, side_effect=None):
        """Run dispatch_tool with requests.post mocked; return (result, spans)."""
        with tracer.trace_scope("test-trace") as collector:
            with mock.patch("requests.post") as post:
                if side_effect is not None:
                    post.side_effect = side_effect
                else:
                    post.return_value = response
                result = tools.dispatch_tool(name, {}, token="test-token")
        return result, collector.events

    def _assert_single_span(self, spans, status, operation):
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].status, status)
        self.assertEqual(spans[0].operation, operation)
        self.assertEqual(spans[0].type, "service_http")
        self.assertEqual(spans[0].trace_id, "test-trace")

    def test_success(self):
        result, spans = self._dispatch(response=_response(200, {"result": {}}))
        self.assertEqual(result, {"result": {}})
        self._assert_single_span(spans, "success", "Tool Dispatch: get_booking")
        self.assertEqual(spans[0].details["status_code"], 200)
        self.assertNotIn("error", spans[0].details)

    def test_unauthorized_is_error_span(self):
        result, spans = self._dispatch(response=_response(401))
        self.assertEqual(result, {"error": "Unauthorized"})
        self._assert_single_span(spans, "error", "Tool Dispatch: get_booking")
        self.assertEqual(spans[0].details["status_code"], 401)
        self.assertEqual(spans[0].details["error"], "Unauthorized")

    def test_http_500_is_error_span(self):
        result, spans = self._dispatch(response=_response(500))
        self.assertEqual(result, {"error": "Agent service error: HTTP 500"})
        self._assert_single_span(spans, "error", "Tool Dispatch: get_booking")
        self.assertEqual(spans[0].details["status_code"], 500)

    def test_timeout_is_error_span(self):
        result, spans = self._dispatch(side_effect=requests.Timeout())
        self.assertEqual(result, {"error": "Agent timed out"})
        self._assert_single_span(spans, "error", "Tool Dispatch: get_booking")
        self.assertEqual(spans[0].details["error"], "Agent timed out")

    def test_unreachable_is_error_span(self):
        result, spans = self._dispatch(side_effect=requests.ConnectionError("refused"))
        self.assertIn("Agent unreachable", result["error"])
        self._assert_single_span(spans, "error", "Tool Dispatch: get_booking")
        self.assertEqual(spans[0].details["error"], "Agent unreachable")

    def test_unknown_tool_is_error_span(self):
        result, spans = self._dispatch(name="bogus_tool", response=_response(200))
        self.assertEqual(result, {"error": "Unknown tool: bogus_tool"})
        self._assert_single_span(spans, "error", "Tool Dispatch: bogus_tool")

    def test_remote_agent_spans_are_merged(self):
        remote = {
            "source": "booking_read_service",
            "target": "postgres",
            "operation": "SELECT bookings",
            "type": "db_query",
            "duration_ms": 3.5,
            "status": "success",
            "span_id": "abc123",
        }
        _, spans = self._dispatch(response=_response(200, {"result": {}, "traces": [remote]}))
        self.assertEqual([s.operation for s in spans], ["SELECT bookings", "Tool Dispatch: get_booking"])
        self.assertEqual(spans[0].span_id, "abc123")


class SpanContextManagerTests(unittest.TestCase):
    def test_exception_marks_error_and_reraises(self):
        with tracer.trace_scope("t") as collector:
            with self.assertRaises(ValueError):
                with tracer.span("op", "a", "b", "db_query"):
                    raise ValueError("boom")
        self.assertEqual(collector.events[0].status, "error")
        self.assertEqual(collector.events[0].details["error_type"], "ValueError")

    def test_handled_failure_via_details_error(self):
        details = {}
        with tracer.trace_scope("t") as collector:
            with tracer.span("op", "a", "b", "service_http", details=details):
                details["error"] = "handled"
        self.assertEqual(collector.events[0].status, "error")

    def test_clean_exit_is_success(self):
        with tracer.trace_scope("t") as collector:
            with tracer.span("op", "a", "b", "service_http"):
                pass
        self.assertEqual(collector.events[0].status, "success")


if __name__ == "__main__":
    unittest.main()
