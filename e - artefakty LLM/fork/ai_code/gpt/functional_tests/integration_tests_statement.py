#!/usr/bin/env python3
"""
Integration tests for POST /accounts/{accountId}/statements.

Run:
  py -3 integration_tests_statement_2.py
  py -3 integration_tests_statement_2.py --module-path C:\path\to\script.py
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient


DEFAULT_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
DEFAULT_AUTH = "Bearer aaa.bbb.ccc"
DEFAULT_PERIOD_START = "2026-02-01"
DEFAULT_PERIOD_END = "2026-02-28"


def load_target_module(module_path: str | None):
    path = Path(module_path).resolve() if module_path else (Path(__file__).resolve().parents[1] / "script.py")
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
        self.module.db.statement_jobs.clear()
        self.module.db.idempotency_index.clear()
        self.module.db.job_progress.clear()
        self.module.db.transactions.clear()
        self.module.db.seed()

    def make_request(
        self,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        idempotency_key: str | None = "idem-default",
        period_start: str = DEFAULT_PERIOD_START,
        period_end: str = DEFAULT_PERIOD_END,
        fmt: str = "PDF",
    ):
        headers = {"Authorization": DEFAULT_AUTH}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        direct_body = {
            "periodStart": period_start,
            "periodEnd": period_end,
            "format": fmt,
        }
        wrapped_body = {"statementRequest": direct_body}

        response = self.client.post(
            f"/accounts/{account_id}/statements",
            headers=headers,
            json=wrapped_body,
        )
        if response.status_code == 422:
            response = self.client.post(
                f"/accounts/{account_id}/statements",
                headers=headers,
                json=direct_body,
            )
        return response

    def install_spies(self) -> dict[str, Any]:
        captures: dict[str, Any] = {
            "stored_documents": [],
            "published_events": [],
            "render_calls": [],
        }

        original_store = self.module.storage_client.store
        original_publish = self.module.event_bus.publish
        original_render = self.module.mapper_service.RenderStatementDocument

        async def store_spy(statement_id: str, payload: bytes) -> str:
            captures["stored_documents"].append({"statement_id": statement_id, "payload": payload})
            return await original_store(statement_id, payload)

        async def publish_spy(event_name: str, payload: dict[str, Any]) -> bool:
            captures["published_events"].append({"event_name": event_name, "payload": payload})
            return True

        async def render_spy(account_profile, formatting_preferences, transactions, statement_totals, ctx) -> None:
            captures["render_calls"].append(
                {
                    "account_profile": account_profile,
                    "formatting_preferences": formatting_preferences,
                    "transactions": transactions,
                    "statement_totals": statement_totals,
                }
            )
            await original_render(
                account_profile=account_profile,
                formatting_preferences=formatting_preferences,
                transactions=transactions,
                statement_totals=statement_totals,
                ctx=ctx,
            )

        restores = [
            self.patch_attr(self.module.storage_client, "store", store_spy),
            self.patch_attr(self.module.event_bus, "publish", publish_spy),
            self.patch_attr(self.module.mapper_service, "RenderStatementDocument", render_spy),
        ]
        captures["restore"] = lambda: [restore() for restore in reversed(restores)]
        return captures

    def seed_existing_job(
        self,
        *,
        statement_id: str,
        status: str,
        period_start: str = DEFAULT_PERIOD_START,
        period_end: str = DEFAULT_PERIOD_END,
        failure_reason: str | None = None,
        stalled: bool | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.module.db.statement_jobs[statement_id] = {
            "status": status,
            "account_id": DEFAULT_ACCOUNT_ID,
            "period_start": period_start,
            "period_end": period_end,
        }
        if failure_reason is not None:
            self.module.db.statement_jobs[statement_id]["failure_reason"] = failure_reason
        if stalled is not None:
            self.module.db.job_progress[statement_id] = self.module.JobProgress(stalled=stalled, progress_percent=25)
        if idempotency_key is not None:
            self.module.db.idempotency_index[(DEFAULT_ACCOUNT_ID, idempotency_key)] = statement_id

    def run_named(self, name: str, fn: Callable[[], None]) -> bool:
        print(f"\n=== {name} ===")
        try:
            fn()
            print(f"PASS {name}")
            return True
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            return False

    def test_t1a_not_found_profile_available(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []

        async def custom_profile(account_id: str):
            return self.module.AccountProfile(
                account_id=account_id,
                customer_name="Jan Kowalski",
                locale="pl_PL",
                formatting_preference="ALT",
            )

        restores.append(self.patch_attr(self.module.profile_client, "fetch_profile", custom_profile))

        try:
            response = self.make_request(idempotency_key="t1a-idem")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T1a should return 202")
        body = response.json()
        self.assert_true(bool(body.get("statementId")), "T1a should return statementId")
        self.assert_true(bool(body.get("downloadUrl")), "T1a should return downloadUrl")
        self.assert_equal(len(captures["render_calls"]), 1, "T1a should render exactly one document")
        formatting = captures["render_calls"][0]["formatting_preferences"]
        self.assert_equal(formatting.locale, "pl_PL", "T1a should use formatting from account profile")
        self.assert_equal(formatting.date_format, "DD/MM/YYYY", "T1a should use ALT profile formatting")
        self.assert_equal(len(captures["published_events"]), 1, "T1a should publish StatementReady")
        self.assert_equal(captures["published_events"][0]["event_name"], "StatementReady", "T1a event mismatch")
        self.assert_equal(len(captures["stored_documents"]), 1, "T1a should store one document")

    def test_t1b_not_found_profile_default_formatting(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []

        async def fetch_profile_without_profile(account_id: str, ctx) -> None:
            ctx.account_profile = None

        restores.append(self.patch_attr(self.module.external_service, "FetchAccountProfile", fetch_profile_without_profile))

        try:
            response = self.make_request(idempotency_key="t1b-idem")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T1b should return 202")
        body = response.json()
        self.assert_true(bool(body.get("statementId")), "T1b should return statementId")
        self.assert_true(bool(body.get("downloadUrl")), "T1b should return downloadUrl")
        self.assert_equal(len(captures["render_calls"]), 1, "T1b should render one document")
        render_call = captures["render_calls"][0]
        self.assert_true(render_call["account_profile"] is None, "T1b should render with missing account profile")
        formatting = render_call["formatting_preferences"]
        self.assert_equal(formatting.locale, "en_US", "T1b should use default formatting locale")
        payload_text = captures["stored_documents"][0]["payload"].decode("utf-8")
        self.assert_true("Unknown Customer" in payload_text, "T1b document should use fallback customer")
        self.assert_true("Locale: en_US" in payload_text, "T1b document should use default locale")
        self.assert_equal(len(captures["published_events"]), 1, "T1b should publish StatementReady")

    def test_t1c_not_found_data_corruption(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []

        async def corrupt_transactions(account_id: str, validated_statement, ctx) -> None:
            raise self.module.DataCorruption("Corrupted transaction payload.")

        restores.append(self.patch_attr(self.module.repository_service, "LoadTransactions", corrupt_transactions))

        try:
            response = self.make_request(idempotency_key="t1c-idem")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 409, "T1c should propagate DataCorruption as 409")
        detail = response.json()["detail"]
        self.assert_equal(detail["error"], "data_corruption", "T1c should return data_corruption")
        self.assert_equal(len(captures["stored_documents"]), 0, "T1c should not store document")
        self.assert_equal(len(captures["published_events"]), 0, "T1c should not publish StatementReady")

    def test_t2a_completed(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        self.seed_existing_job(statement_id="stmt-completed", status="completed")

        try:
            response = self.make_request(idempotency_key="t2a-idem")
        finally:
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T2a should return 202")
        self.assert_equal(response.json()["statementId"], "stmt-completed", "T2a should return existingStatementId")
        self.assert_equal(len(captures["stored_documents"]), 0, "T2a should not generate document")
        self.assert_equal(len(captures["published_events"]), 0, "T2a should not publish event")

    def test_t2b_running_not_stalled(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        self.seed_existing_job(statement_id="stmt-running", status="running", stalled=False)

        try:
            response = self.make_request(idempotency_key="t2b-idem")
        finally:
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T2b should return 202")
        self.assert_equal(response.json()["statementId"], "stmt-running", "T2b should return existingStatementId")
        self.assert_equal(len(captures["stored_documents"]), 0, "T2b should not generate document")
        self.assert_equal(len(captures["published_events"]), 0, "T2b should not publish event")

    def test_t2c_running_stalled(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        self.seed_existing_job(statement_id="stmt-stalled", status="running", stalled=True)

        try:
            response = self.make_request(idempotency_key="t2c-idem")
        finally:
            captures["restore"]()

        body = response.json()
        self.assert_equal(response.status_code, 202, "T2c should return 202")
        self.assert_true(body["statementId"] != "stmt-stalled", "T2c should return restartedJobId")
        self.assert_equal(len(captures["stored_documents"]), 0, "T2c should not generate document")
        self.assert_equal(len(captures["published_events"]), 0, "T2c should not publish event")

    def test_t2d_failed_retryable(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        self.seed_existing_job(
            statement_id="stmt-failed-retryable",
            status="failed",
            failure_reason="timeout",
        )

        try:
            response = self.make_request(idempotency_key="t2d-idem")
        finally:
            captures["restore"]()

        body = response.json()
        self.assert_equal(response.status_code, 202, "T2d should return 202")
        self.assert_true(body["statementId"] != "stmt-failed-retryable", "T2d should return recoveryResult id")
        self.assert_equal(len(captures["stored_documents"]), 0, "T2d should not generate document")
        self.assert_equal(len(captures["published_events"]), 0, "T2d should not publish event")

    def test_t2e_failed_non_retryable(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        self.seed_existing_job(
            statement_id="stmt-failed-non-retryable",
            status="failed",
            failure_reason="schema_error",
        )

        before_job_count = len(self.module.db.statement_jobs)
        try:
            response = self.make_request(idempotency_key="t2e-idem")
        finally:
            captures["restore"]()

        body = response.json()
        self.assert_equal(response.status_code, 202, "T2e should return 202")
        self.assert_true(body["statementId"] != "stmt-failed-non-retryable", "T2e should return newStatementId")
        self.assert_equal(len(captures["stored_documents"]), 0, "T2e should not generate document")
        self.assert_equal(len(captures["published_events"]), 0, "T2e should not publish event")
        self.assert_equal(len(self.module.db.statement_jobs), before_job_count + 1, "T2e should create one new job only")

    def test_t3a_join_waits_for_all_parallel_paths(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []
        marks: dict[str, float] = {}

        original_load_transactions = self.module.repository_service.LoadTransactions
        original_load_fees = self.module.cache_service.LoadFeesAndRates
        original_fetch_profile = self.module.external_service.FetchAccountProfile
        original_compute_totals = self.module.business_rule_service.ComputeStatementTotals

        async def slow_transactions(account_id: str, validated_statement, ctx) -> None:
            await self.module.asyncio.sleep(0.35)
            marks["transactions_done"] = time.perf_counter()
            await original_load_transactions(account_id, validated_statement, ctx)

        async def fast_fees(validated_statement, ctx) -> None:
            await self.module.asyncio.sleep(0.05)
            marks["fees_done"] = time.perf_counter()
            await original_load_fees(validated_statement, ctx)

        async def medium_profile(account_id: str, ctx) -> None:
            await self.module.asyncio.sleep(0.10)
            marks["profile_done"] = time.perf_counter()
            await original_fetch_profile(account_id, ctx)

        async def compute_spy(transactions, fee_schedule, preliminary_calculations, ctx) -> None:
            marks["compute_started"] = time.perf_counter()
            await original_compute_totals(transactions, fee_schedule, preliminary_calculations, ctx)

        restores.extend(
            [
                self.patch_attr(self.module.repository_service, "LoadTransactions", slow_transactions),
                self.patch_attr(self.module.cache_service, "LoadFeesAndRates", fast_fees),
                self.patch_attr(self.module.external_service, "FetchAccountProfile", medium_profile),
                self.patch_attr(self.module.business_rule_service, "ComputeStatementTotals", compute_spy),
            ]
        )

        started = time.perf_counter()
        try:
            response = self.make_request(idempotency_key="t3a-idem")
        finally:
            elapsed = time.perf_counter() - started
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T3a should return 202")
        self.assert_true("compute_started" in marks, "T3a should reach ComputeStatementTotals")
        self.assert_true(
            marks["compute_started"] >= max(marks["transactions_done"], marks["fees_done"], marks["profile_done"]),
            "T3a should start ComputeStatementTotals only after all fork branches finish",
        )
        self.assert_true(elapsed >= 0.32, "T3a response time should be dominated by the slowest branch")

    def test_t3b_default_formatting_without_profile(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []

        async def fetch_profile_without_profile(account_id: str, ctx) -> None:
            ctx.account_profile = None

        restores.append(self.patch_attr(self.module.external_service, "FetchAccountProfile", fetch_profile_without_profile))

        try:
            response = self.make_request(idempotency_key="t3b-idem")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 202, "T3b should return 202")
        render_call = captures["render_calls"][0]
        self.assert_true(render_call["account_profile"] is None, "T3b should allow null account profile")
        self.assert_equal(render_call["formatting_preferences"].locale, "en_US", "T3b should use default formatting")
        payload_text = captures["stored_documents"][0]["payload"].decode("utf-8")
        self.assert_true("Unknown Customer" in payload_text, "T3b should render fallback customer name")

    def test_t3c_store_statement_retry_exhausted(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores: list[Callable[[], None]] = []
        attempts = {"count": 0}

        async def failing_store(statement_id: str, payload: bytes) -> str:
            attempts["count"] += 1
            raise self.module.StorageUnavailable("Simulated storage outage")

        restores.append(self.patch_attr(self.module.storage_client, "store", failing_store))

        try:
            response = self.make_request(idempotency_key="t3c-idem")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()

        self.assert_equal(response.status_code, 503, "T3c should return 503")
        detail = response.json()["detail"]
        self.assert_equal(detail["error"], "storage_unavailable", "T3c should return storage_unavailable")
        self.assert_equal(attempts["count"], 3, "T3c should retry store 3 times")
        self.assert_equal(len(captures["published_events"]), 0, "T3c should not publish event")
        self.assert_equal(len(captures["stored_documents"]), 0, "T3c should not record successful storage")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a_not_found_profile_available),
            ("T1b", self.test_t1b_not_found_profile_default_formatting),
            ("T1c", self.test_t1c_not_found_data_corruption),
            ("T2a", self.test_t2a_completed),
            ("T2b", self.test_t2b_running_not_stalled),
            ("T2c", self.test_t2c_running_stalled),
            ("T2d", self.test_t2d_failed_retryable),
            ("T2e", self.test_t2e_failed_non_retryable),
            ("T3a", self.test_t3a_join_waits_for_all_parallel_paths),
            ("T3b", self.test_t3b_default_formatting_without_profile),
            ("T3c", self.test_t3c_store_statement_retry_exhausted),
        ]

        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        total = len(results)

        print("\n=== Summary ===")
        for name, ok in results.items():
            print(f"{name}: {'PASS' if ok else 'FAIL'}")
        print(f"\nOverall: {passed}/{total} passed")
        return 0 if passed == total else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Integration tests for statement generation flow")
    parser.add_argument("--module-path", help="Path to target FastAPI module")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module = load_target_module(args.module_path)
    suite = IntegrationTestSuite(module)
    return suite.run_all()


if __name__ == "__main__":
    sys.exit(main())
