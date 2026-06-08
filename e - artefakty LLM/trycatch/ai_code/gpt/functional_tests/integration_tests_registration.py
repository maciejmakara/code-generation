#!/usr/bin/env python3
"""
Integration tests for POST /users/register.

Run:
  py -3 integration_tests_registration_2.py
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


class IntegrationTestSuite:
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
        request_id: str | None = "req-123",
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

    def get_user_by_email(self, email: str):
        return self.module.repository._users_by_email.get(email.lower())

    def install_audit_capture(self) -> dict[str, Any]:
        captures: dict[str, Any] = {"audit_entries": []}
        original_write = self.module.audit_service.write

        def write_spy(action_name: str, details: dict[str, Any]) -> None:
            captures["audit_entries"].append({"action_name": action_name, "details": details})
            original_write(action_name, details)

        restore = self.patch_attr(self.module.audit_service, "write", write_spy)
        captures["restore"] = restore
        return captures

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
            response = self.make_request(email="t1a-2@example.com")
        finally:
            captures["restore"]()

        self.assert_equal(response.status_code, 201, "T1a should return 201")
        body = response.json()
        self.assert_true(bool(body.get("requestId")), "T1a should return requestId")

        user = self.get_user_by_email("t1a-2@example.com")
        self.assert_true(user is not None, "T1a user should be created")
        self.assert_equal(user["status"], "ACTIVE", "T1a should satisfy userStatus_Active")

        action_names = [entry["action_name"] for entry in captures["audit_entries"]]
        self.assert_true("FilterDisposableEmailDomains" in action_names, "T1a should audit FilterDisposableEmailDomains")
        self.assert_true("CreateUserAccount" in action_names, "T1a should audit CreateUserAccount")

    def test_t1b_success_via_okcreate_branch_after_email_retry(self) -> None:
        self.reset_state()
        captures = self.install_audit_capture()
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
                if attempts["count"] < 2:
                    raise self.module.TimeoutException("Transient timeout while sending activation email.")
                return True

            return await self.module.execute_with_retry(do_send, retry_policy, classify_error)

        restores.append(self.patch_attr(self.module.registration_service, "SendActivationEmail", send_email_retry_then_success))

        try:
            response = self.make_request(email="t1b-2@example.com")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 201, "T1b should return 201")
        body = response.json()
        self.assert_true(bool(body.get("requestId")), "T1b should return requestId")
        self.assert_true(attempts["count"] <= 3, "T1b should send email in at most 3 attempts")
        self.assert_equal(attempts["count"], 2, "T1b should succeed on the second attempt")

        user = self.get_user_by_email("t1b-2@example.com")
        self.assert_true(user is not None, "T1b user should be created")
        self.assert_equal(user["status"], "ACTIVE", "T1b should satisfy postCondition userStatus_Active")

    def test_t2a_timeout_after_create_user_step_without_user_id(self) -> None:
        self.reset_state()
        restores: list[Callable[[], None]] = []

        def create_timeout(validatedUserData):
            raise self.module.TimeoutException("Timeout while creating user account.")

        restores.append(self.patch_attr(self.module.registration_service, "CreateUserAccount", create_timeout))

        try:
            response = self.make_request(email="t2a-2@example.com")
        finally:
            for restore in reversed(restores):
                restore()

        self.assert_equal(response.status_code, 504, "T2a should return 504")
        detail = response.json()["detail"]
        self.assert_equal(detail["code"], "REGISTRATION_TIMEOUT", "T2a should go through HandleRegistrationTimeout")
        self.assert_equal(detail["request_id"], None, "T2a should not expose userId in error response")
        self.assert_equal(detail["details"]["registrationStatus"], "Failed", "T2a should set registrationStatus_Failed")

        user = self.get_user_by_email("t2a-2@example.com")
        self.assert_true(user is None, "T2a should not create an active user")

    def test_t2b_timeout_after_email_retry_with_user_id(self) -> None:
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
            response = self.make_request(email="t2b-2@example.com")
        finally:
            for restore in reversed(restores):
                restore()

        self.assert_equal(response.status_code, 504, "T2b should return 504")
        detail = response.json()["detail"]
        self.assert_equal(detail["code"], "REGISTRATION_TIMEOUT", "T2b should go through HandleRegistrationTimeout")
        self.assert_true(bool(detail["request_id"]), "T2b should include userId in timeout response")
        self.assert_equal(detail["details"]["registrationStatus"], "Failed", "T2b should set registrationStatus_Failed")
        self.assert_equal(attempts["count"], 3, "T2b should exhaust 3 email retry attempts")

        user = self.get_user_by_email("t2b-2@example.com")
        self.assert_true(user is not None, "T2b should leave created user record")
        self.assert_true(user["status"] != "ACTIVE", "T2b should not activate user")

    def test_t3a_conflict_before_decision(self) -> None:
        self.reset_state()
        self.module.repository.create_user(
            self.module.ValidatedUserData(
                email="t3a-2@example.com",
                password_hash="hashed::seed",
                first_name="Seed",
                last_name="User",
            )
        )

        response = self.make_request(email="t3a-2@example.com", request_id="t3a-req")

        self.assert_equal(response.status_code, 409, "T3a should return 409")
        detail = response.json()["detail"]
        self.assert_equal(detail["code"], "EMAIL_ALREADY_EXISTS", "T3a should propagate ConflictException directly")

    def test_t3b_disposable_email_before_decision(self) -> None:
        self.reset_state()
        response = self.make_request(email="t3b-2@mailinator.com", request_id="t3b-req")

        self.assert_equal(response.status_code, 400, "T3b should return 400 in the current implementation")
        detail = response.json()["detail"]
        self.assert_equal(detail["code"], "DISPOSABLE_EMAIL_REJECTED", "T3b should propagate DisposableEmailDetected directly")

    def test_t3c_validation_before_decision(self) -> None:
        self.reset_state()
        response = self.make_request(email="t3c-2@example.com", password="short", request_id="t3c-req")

        self.assert_equal(response.status_code, 422, "T3c should return 422 in the current implementation")
        detail = response.json()["detail"]
        if isinstance(detail, list):
            self.assert_true(len(detail) > 0, "T3c should return FastAPI validation details")
        else:
            self.assert_equal(detail["code"], "VALIDATION_ERROR", "T3c should propagate ValidationException directly")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a_success),
            ("T1b", self.test_t1b_success_via_okcreate_branch_after_email_retry),
            ("T2a", self.test_t2a_timeout_after_create_user_step_without_user_id),
            ("T2b", self.test_t2b_timeout_after_email_retry_with_user_id),
            ("T3a", self.test_t3a_conflict_before_decision),
            ("T3b", self.test_t3b_disposable_email_before_decision),
            ("T3c", self.test_t3c_validation_before_decision),
        ]
        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        print(f"\nOverall: {passed}/{len(results)} passed")
        return 0 if passed == len(results) else 1


def main() -> int:
    module = load_target_module()
    suite = IntegrationTestSuite(module)
    return suite.run_all()


if __name__ == "__main__":
    sys.exit(main())
