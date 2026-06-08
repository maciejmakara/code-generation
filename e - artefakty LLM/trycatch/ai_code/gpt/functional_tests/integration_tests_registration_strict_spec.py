#!/usr/bin/env python3
"""
Strict metacode-conformance integration tests for POST /users/register.

These tests assert what the metacode guarantees:
- success returns 201 and activates user
- timeouts return 504 and result in registrationStatus_Failed (observed via state)
- exceptions propagate with HTTP codes: 409, 422, 400

Run:
  py -3 integration_tests_registration_strict_spec_2.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient


DEFAULT_PASSWORD = "VerySecret123"


def load_target_module():
    path = Path(__file__).resolve().parents[1] / "script.py"
    if not path.exists():
        raise FileNotFoundError(f"Target module not found: {path}")

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load target module from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


class StrictSpecSuite:
    def __init__(self, module: Any):
        self.module = module
        self.client = TestClient(module.app, raise_server_exceptions=False)

    def assert_equal(self, actual: Any, expected: Any, message: str) -> None:
        if actual != expected:
            raise AssertionError(f"{message}. Expected={expected!r}, actual={actual!r}")

    def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def patch_attr(self, obj: Any, attr: str, new_value: Any) -> Callable[[], None]:
        original = getattr(obj, attr)
        setattr(obj, attr, new_value)
        return lambda: setattr(obj, attr, original)

    def reset_state(self) -> None:
        self.module.repository._users_by_email.clear()
        self.module.repository._users_by_id.clear()

    def make_request(
        self,
        *,
        email: str,
        password: str = DEFAULT_PASSWORD,
        first_name: str = "John",
        last_name: str = "Doe",
        request_id: str | None = "req-spec2",
    ):
        headers: dict[str, str] = {}
        if request_id is not None:
            headers["x-request-id"] = request_id

        return self.client.post(
            "/users/register",
            headers=headers,
            json={
                "email": email,
                "password": password,
                "first_name": first_name,
                "last_name": last_name,
            },
        )

    def get_user(self, email: str):
        return self.module.repository._users_by_email.get(email.lower())

    def install_audit_capture(self) -> dict[str, Any]:
        captures: dict[str, Any] = {"entries": []}
        original_write = self.module.audit_service.write

        def write_spy(action_name: str, details: dict[str, Any]) -> None:
            captures["entries"].append({"action_name": action_name, "details": details})
            original_write(action_name, details)

        captures["restore"] = self.patch_attr(self.module.audit_service, "write", write_spy)
        return captures

    def ensure_no_active_user(self, email: str) -> None:
        user = self.get_user(email)
        if user is None:
            return
        self.assert_true(user.get("status") != "ACTIVE", "No user should be ACTIVE in timeout scenarios")

    def run_named(self, name: str, fn: Callable[[], None]) -> bool:
        print(f"\n=== {name} ===")
        try:
            fn()
            print(f"PASS {name}")
            return True
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            return False

    def test_t1a_success(self) -> None:
        self.reset_state()
        captures = self.install_audit_capture()

        try:
            response = self.make_request(email="strict2-t1a@example.com")
        finally:
            captures["restore"]()

        self.assert_equal(response.status_code, 201, "T1a should return 201 Created")
        body = response.json()
        self.assert_true(bool(body.get("requestId")), "T1a body should contain requestId")

        user = self.module.repository._users_by_email.get("strict2-t1a@example.com")
        self.assert_true(user is not None, "T1a should create a user")
        self.assert_equal(user["status"], "ACTIVE", "T1a must satisfy postCondition userStatus_Active")

        action_names = [entry["action_name"] for entry in captures["entries"]]
        self.assert_true("FilterDisposableEmailDomains" in action_names, "T1a should audit FilterDisposableEmailDomains")
        self.assert_true("CreateUserAccount" in action_names, "T1a should audit CreateUserAccount")

    def test_t1b_success_with_retry(self) -> None:
        self.reset_state()
        restores: list[Callable[[], None]] = []
        attempts = {"count": 0}

        async def send_email_retry_then_success(userId: str, email: str) -> bool:
            retry_policy = self.module.RetryPolicy(
                max_attempts=3,
                backoff_ms=1,
                retry_on=["Timeout", "502", "503"],
                on_retry_exhausted="raise",
            )

            def classify_error(exc: Exception) -> str:
                if isinstance(exc, self.module.TimeoutException):
                    return "Timeout"
                return exc.__class__.__name__

            async def do_send() -> bool:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise self.module.TimeoutException("Transient timeout during activation email.")
                return True

            return await self.module.execute_with_retry(do_send, retry_policy, classify_error)

        restores.append(self.patch_attr(self.module.registration_service, "SendActivationEmail", send_email_retry_then_success))

        try:
            response = self.make_request(email="strict2-t1b@example.com")
        finally:
            for restore in reversed(restores):
                restore()

        self.assert_equal(response.status_code, 201, "T1b should return 201 Created")
        body = response.json()
        self.assert_true(bool(body.get("requestId")), "T1b body should contain requestId")
        self.assert_true(1 <= attempts["count"] <= 3, "T1b email should be sent within max 3 attempts")

        user = self.module.repository._users_by_email.get("strict2-t1b@example.com")
        self.assert_true(user is not None, "T1b should create a user")
        self.assert_equal(user["status"], "ACTIVE", "T1b must satisfy postCondition userStatus_Active")

    def test_t2a_timeout_without_user_id(self) -> None:
        self.reset_state()
        restores: list[Callable[[], None]] = []

        def create_timeout(validatedUserData):
            raise self.module.TimeoutException("Timeout while creating user account.")

        restores.append(self.patch_attr(self.module.registration_service, "CreateUserAccount", create_timeout))

        try:
            response = self.make_request(email="strict2-t2a@example.com")
        finally:
            for restore in reversed(restores):
                restore()

        self.assert_equal(response.status_code, 504, "T2a should return 504")
        self.ensure_no_active_user("strict2-t2a@example.com")

    def test_t2b_timeout_with_user_id(self) -> None:
        self.reset_state()
        restores: list[Callable[[], None]] = []
        attempts = {"count": 0}

        async def send_email_timeout_after_retries(userId: str, email: str) -> bool:
            retry_policy = self.module.RetryPolicy(
                max_attempts=3,
                backoff_ms=1,
                retry_on=["Timeout", "502", "503"],
                on_retry_exhausted="raise",
            )

            def classify_error(exc: Exception) -> str:
                if isinstance(exc, self.module.TimeoutException):
                    return "Timeout"
                return exc.__class__.__name__

            async def do_send() -> bool:
                attempts["count"] += 1
                raise self.module.TimeoutException("Timeout while sending activation email.")

            return await self.module.execute_with_retry(do_send, retry_policy, classify_error)

        restores.append(self.patch_attr(self.module.registration_service, "SendActivationEmail", send_email_timeout_after_retries))

        try:
            response = self.make_request(email="strict2-t2b@example.com")
        finally:
            for restore in reversed(restores):
                restore()

        self.assert_equal(response.status_code, 504, "T2b should return 504")
        self.assert_equal(attempts["count"], 3, "T2b should exhaust 3 attempts")
        self.ensure_no_active_user("strict2-t2b@example.com")

    def test_t3a_conflict(self) -> None:
        self.reset_state()
        self.module.repository.create_user(
            self.module.ValidatedUserData(
                email="strict2-t3a@example.com",
                password_hash="hashed::seed",
                first_name="Seed",
                last_name="User",
            )
        )

        response = self.make_request(email="strict2-t3a@example.com", request_id="t3a")
        self.assert_equal(response.status_code, 409, "T3a should return 409")

    def test_t3b_disposable_email(self) -> None:
        self.reset_state()
        response = self.make_request(email="strict2-t3b@mailinator.com", request_id="t3b")
        self.assert_equal(response.status_code, 422, "T3b should return 422 by metacode")

    def test_t3c_validation_exception(self) -> None:
        self.reset_state()
        response = self.make_request(email="strict2-t3c@example.com", password="short", request_id="t3c")
        self.assert_equal(response.status_code, 400, "T3c should return 400 by metacode")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a_success),
            ("T1b", self.test_t1b_success_with_retry),
            ("T2a", self.test_t2a_timeout_without_user_id),
            ("T2b", self.test_t2b_timeout_with_user_id),
            ("T3a", self.test_t3a_conflict),
            ("T3b", self.test_t3b_disposable_email),
            ("T3c", self.test_t3c_validation_exception),
        ]
        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        print(f"\nOverall: {passed}/{len(results)} passed")
        return 0 if passed == len(results) else 1


def main() -> int:
    return StrictSpecSuite(load_target_module()).run_all()


if __name__ == "__main__":
    sys.exit(main())
