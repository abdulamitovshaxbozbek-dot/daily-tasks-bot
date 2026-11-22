"""Regression checks for the legacy HTTP routes without a live database or bot."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import Mock


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def load_routes():
    source = Path(__file__).resolve().parents[1] / "api" / "routes.py"
    tree = ast.parse(source.read_text())
    names = {
        "verify_api_key", "get_miniapp_user", "get_miniapp_chat_id",
        "require_matching_chat_id", "start_api", "morning_time_api",
        "tasks_api", "task_status_api", "daily_report_api",
        "weekly_report_api", "monthly_report_api", "yearly_report_api",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    handlers = {
        name: Mock(return_value={"ok": True})
        for name in (
            "handle_telegram_start", "handle_morning_time", "handle_create_tasks",
            "handle_task_status", "handle_daily_report", "handle_weekly_report",
            "handle_monthly_report", "handle_yearly_report",
        )
    }
    ns = {
        "Optional": Optional, "Header": lambda default=None: default,
        "Query": lambda default=None, **kwargs: default,
        "Depends": lambda dependency: None, "HTTPException": HTTPError,
        "StartUserRequest": object, "MorningTimeRequest": object,
        "CreateTasksRequest": object, "TaskStatusRequest": object,
        "verify_telegram_init_data": Mock(return_value={"id": 123}),
        "API_KEY": None,
        **handlers,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), ns)
    return ns, handlers


class LegacyApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.handlers = load_routes()

    def test_routes_require_signed_telegram_dependency(self):
        source = Path(__file__).resolve().parents[1] / "api" / "routes.py"
        tree = ast.parse(source.read_text())
        expected = {
            "start_api": "get_miniapp_user",
            "morning_time_api": "get_miniapp_chat_id",
            "tasks_api": "get_miniapp_chat_id",
            "task_status_api": "get_miniapp_chat_id",
            "daily_report_api": "get_miniapp_chat_id",
            "weekly_report_api": "get_miniapp_chat_id",
            "monthly_report_api": "get_miniapp_chat_id",
            "yearly_report_api": "get_miniapp_chat_id",
        }
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in expected:
                dependencies = [
                    call.args[0].id for call in ast.walk(node.args)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "Depends" and call.args
                    and isinstance(call.args[0], ast.Name)
                ]
                self.assertIn(expected[node.name], dependencies, node.name)

    def test_missing_or_wrong_api_key_fails_closed(self):
        with self.assertRaises(HTTPError) as error:
            self.ns["verify_api_key"](None)
        self.assertEqual(error.exception.status_code, 503)
        self.ns["API_KEY"] = "secret"
        with self.assertRaises(HTTPError) as error:
            self.ns["verify_api_key"]("wrong")
        self.assertEqual(error.exception.status_code, 401)
        self.assertIsNone(self.ns["verify_api_key"]("secret"))

    def test_invalid_telegram_identity_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.ns["get_miniapp_chat_id"]({"id": "not-a-number"})
        self.assertEqual(error.exception.status_code, 401)

    def test_legacy_task_and_report_routes_reject_other_chat_id(self):
        cases = [
            ("morning_time_api", (SimpleNamespace(chat_id=999, time="08:00"), 123)),
            ("tasks_api", (SimpleNamespace(chat_id=999, text="Task"), 123)),
            ("task_status_api", (SimpleNamespace(chat_id=999, task_id="t", status="done"), 123)),
            ("daily_report_api", (999, 123)),
            ("weekly_report_api", (999, 123)),
            ("monthly_report_api", (999, 123)),
            ("yearly_report_api", (999, 123)),
        ]
        for name, args in cases:
            with self.subTest(route=name), self.assertRaises(HTTPError) as error:
                self.ns[name](*args)
            self.assertEqual(error.exception.status_code, 403)
        for handler in self.handlers.values():
            handler.assert_not_called()

    def test_start_uses_signed_identity(self):
        request = SimpleNamespace(chat_id=123, username="forged", first_name="Forged")
        signed = {"id": 123, "username": "real", "first_name": "Real"}
        self.ns["start_api"](request, signed)
        self.handlers["handle_telegram_start"].assert_called_once_with(123, "real", "Real")

    def test_start_rejects_other_chat_id(self):
        request = SimpleNamespace(chat_id=999, username="forged", first_name="Forged")
        with self.assertRaises(HTTPError) as error:
            self.ns["start_api"](request, {"id": 123})
        self.assertEqual(error.exception.status_code, 403)
        self.handlers["handle_telegram_start"].assert_not_called()

    def test_authenticated_user_can_use_own_chat_id(self):
        self.ns["tasks_api"](SimpleNamespace(chat_id=123, text="Task"), 123)
        self.handlers["handle_create_tasks"].assert_called_once_with(123, "Task")
        self.ns["daily_report_api"](123, 123)
        self.handlers["handle_daily_report"].assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
