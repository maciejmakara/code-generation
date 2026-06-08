#!/usr/bin/env python3
"""
Strict metacode-conformance integration tests for POST /accounts/{accountId}/statements.

Run:
  py -3 integration_tests_statement_strict_spec_2.py
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient


DEFAULT_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"


def load_target_module():
    path = Path(__file__).resolve().parents[1] / "script.py"
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

    def reset_state(self) -> None:
        self.module.db.statement_jobs.clear()
        self.module.db.idempotency_index.clear()
        self.module.db.job_progress.clear()
        self.module.db.transactions.clear()
        self.module.db.seed()

    def patch_attr(self, obj: Any, attr: str, new_value: Any) -> Callable[[], None]:
        original = getattr(obj, attr)
        setattr(obj, attr, new_value)
        return lambda: setattr(obj, attr, original)

    def make_request(self, *, idempotency_key: str):
        body = {"periodStart": "2026-02-01", "periodEnd": "2026-02-28", "format": "PDF"}
        response = self.client.post(
            f"/accounts/{DEFAULT_ACCOUNT_ID}/statements",
            headers={"Authorization": "Bearer aaa.bbb.ccc", "Idempotency-Key": idempotency_key},
            json={"statementRequest": body},
        )
        if response.status_code == 422:
            response = self.client.post(
                f"/accounts/{DEFAULT_ACCOUNT_ID}/statements",
                headers={"Authorization": "Bearer aaa.bbb.ccc", "Idempotency-Key": idempotency_key},
                json=body,
            )
        return response

    def install_spies(self) -> dict[str, Any]:
        captures = {"stored_documents": [], "published_events": [], "render_calls": []}
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
            captures["render_calls"].append({"account_profile": account_profile, "formatting_preferences": formatting_preferences})
            await original_render(account_profile, formatting_preferences, transactions, statement_totals, ctx)

        captures["restore"] = lambda: [
            self.patch_attr(self.module.storage_client, "store", original_store),
            self.patch_attr(self.module.event_bus, "publish", original_publish),
            self.patch_attr(self.module.mapper_service, "RenderStatementDocument", original_render),
        ]
        self.patch_attr(self.module.storage_client, "store", store_spy)
        self.patch_attr(self.module.event_bus, "publish", publish_spy)
        self.patch_attr(self.module.mapper_service, "RenderStatementDocument", render_spy)
        return captures

    def seed_existing_job(self, *, statement_id: str, status: str, failure_reason: str | None = None, stalled: bool | None = None):
        self.module.db.statement_jobs[statement_id] = {
            "status": status,
            "account_id": DEFAULT_ACCOUNT_ID,
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
        }
        if failure_reason is not None:
            self.module.db.statement_jobs[statement_id]["failure_reason"] = failure_reason
        if stalled is not None:
            self.module.db.job_progress[statement_id] = self.module.JobProgress(stalled=stalled, progress_percent=25)

    def assert_equal(self, actual: Any, expected: Any, message: str) -> None:
        if actual != expected:
            raise AssertionError(f"{message}. Expected={expected!r}, actual={actual!r}")

    def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def run_named(self, name: str, fn: Callable[[], None]) -> bool:
        print(f"\n=== {name} ===")
        try:
            fn()
            print(f"PASS {name}")
            return True
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            return False

    async def _profile_alt(self, account_id: str):
        return self.module.AccountProfile(
            account_id=account_id,
            customer_name="Jan Kowalski",
            locale="pl_PL",
            formatting_preference="ALT",
        )

    async def _profile_none(self, account_id: str, ctx) -> None:
        ctx.account_profile = None

    async def _corrupt_transactions(self, account_id: str, validated_statement, ctx) -> None:
        raise self.module.DataCorruption("Corrupted transaction payload.")

    def test_t1a(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restores = [self.patch_attr(self.module.profile_client, "fetch_profile", self._profile_alt)]
        try:
            response = self.make_request(idempotency_key="strict2-t1a")
        finally:
            for restore in reversed(restores):
                restore()
            captures["restore"]()
        self.assert_equal(response.status_code, 202, "T1a should return 202")
        body = response.json()
        self.assert_true(bool(body.get("statementId")), "T1a should contain statementId")
        self.assert_true(bool(body.get("downloadUrl")), "T1a should contain downloadUrl")
        self.assert_equal(captures["render_calls"][0]["formatting_preferences"].locale, "pl_PL", "T1a should use profile formatting")
        self.assert_equal(len(captures["published_events"]), 1, "T1a should publish StatementReady")
        self.assert_equal(len(captures["stored_documents"]), 1, "T1a should store document")

    def test_t1b(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restore = self.patch_attr(self.module.external_service, "FetchAccountProfile", self._profile_none)
        try:
            response = self.make_request(idempotency_key="strict2-t1b")
        finally:
            restore()
            captures["restore"]()
        self.assert_equal(response.status_code, 202, "T1b should return 202")
        body = response.json()
        self.assert_true(bool(body.get("statementId")), "T1b should contain statementId")
        self.assert_true(bool(body.get("downloadUrl")), "T1b should contain downloadUrl")
        self.assert_equal(captures["render_calls"][0]["formatting_preferences"].locale, "en_US", "T1b should use default formatting")
        self.assert_equal(len(captures["published_events"]), 1, "T1b should publish StatementReady")
        self.assert_equal(len(captures["stored_documents"]), 1, "T1b should store document")

    def test_t1c(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restore = self.patch_attr(self.module.repository_service, "LoadTransactions", self._corrupt_transactions)
        try:
            response = self.make_request(idempotency_key="strict2-t1c")
        finally:
            restore()
            captures["restore"]()
        self.assert_equal(response.status_code, 409, "T1c should propagate DataCorruption")
        self.assert_equal(response.json()["detail"]["error"], "data_corruption", "T1c should expose DataCorruption")
        self.assert_equal(len(captures["stored_documents"]), 0, "T1c should not store document")
        self.assert_equal(len(captures["published_events"]), 0, "T1c should not publish event")

    def test_t2a(self) -> None:
        self.reset_state()
        self.seed_existing_job(statement_id="stmt-completed", status="completed")
        response = self.make_request(idempotency_key="strict2-t2a")
        self.assert_equal(response.status_code, 202, "T2a should return 202")
        self.assert_equal(response.json()["statementId"], "stmt-completed", "T2a should return existingStatementId")

    def test_t2b(self) -> None:
        self.reset_state()
        self.seed_existing_job(statement_id="stmt-running", status="running", stalled=False)
        response = self.make_request(idempotency_key="strict2-t2b")
        self.assert_equal(response.status_code, 202, "T2b should return 202")
        self.assert_equal(response.json()["statementId"], "stmt-running", "T2b should return existingStatementId")

    def test_t2c(self) -> None:
        self.reset_state()
        self.seed_existing_job(statement_id="stmt-stalled", status="running", stalled=True)
        response = self.make_request(idempotency_key="strict2-t2c")
        self.assert_equal(response.status_code, 202, "T2c should return 202")
        self.assert_true(response.json()["statementId"] != "stmt-stalled", "T2c should return restartedJobId")

    def test_t2d(self) -> None:
        self.reset_state()
        self.seed_existing_job(statement_id="stmt-failed-retryable", status="failed", failure_reason="timeout")
        response = self.make_request(idempotency_key="strict2-t2d")
        self.assert_equal(response.status_code, 202, "T2d should return 202")
        self.assert_true(response.json()["statementId"] != "stmt-failed-retryable", "T2d should return recoveryResult id")

    def test_t2e(self) -> None:
        self.reset_state()
        self.seed_existing_job(statement_id="stmt-failed-non-retryable", status="failed", failure_reason="schema_error")
        jobs_before_request = len(self.module.db.statement_jobs)
        response = self.make_request(idempotency_key="strict2-t2e")
        self.assert_equal(response.status_code, 202, "T2e should return 202")
        self.assert_true(response.json()["statementId"] != "stmt-failed-non-retryable", "T2e should return newStatementId")
        self.assert_equal(len(self.module.db.statement_jobs), jobs_before_request + 1, "T2e should create one new job only")

    def test_t3a(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        marks = {}
        original_load_transactions = self.module.repository_service.LoadTransactions
        original_compute_totals = self.module.business_rule_service.ComputeStatementTotals

        async def slow_transactions(account_id: str, validated_statement, ctx) -> None:
            await self.module.asyncio.sleep(0.35)
            marks["transactions_done"] = time.perf_counter()
            await original_load_transactions(account_id, validated_statement, ctx)

        async def compute_spy(transactions, fee_schedule, preliminary_calculations, ctx) -> None:
            marks["compute_started"] = time.perf_counter()
            await original_compute_totals(transactions, fee_schedule, preliminary_calculations, ctx)

        restores = [
            self.patch_attr(self.module.repository_service, "LoadTransactions", slow_transactions),
            self.patch_attr(self.module.business_rule_service, "ComputeStatementTotals", compute_spy),
        ]
        started = time.perf_counter()
        try:
            response = self.make_request(idempotency_key="strict2-t3a")
        finally:
            elapsed = time.perf_counter() - started
            for restore in reversed(restores):
                restore()
            captures["restore"]()
        self.assert_equal(response.status_code, 202, "T3a should return 202")
        self.assert_true("compute_started" in marks, "T3a should reach ComputeStatementTotals")
        self.assert_true(
            marks["compute_started"] >= marks["transactions_done"],
            "T3a should start ComputeStatementTotals only after all fork branches finish",
        )
        self.assert_true(elapsed >= 0.32, "T3a response time should be dominated by the slowest branch")

    def test_t3b(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        restore = self.patch_attr(self.module.external_service, "FetchAccountProfile", self._profile_none)
        try:
            response = self.make_request(idempotency_key="strict2-t3b")
        finally:
            restore()
            captures["restore"]()
        self.assert_equal(response.status_code, 202, "T3b should return 202")
        render_call = captures["render_calls"][0]
        self.assert_true(render_call["account_profile"] is None, "T3b should allow null account profile")
        self.assert_equal(render_call["formatting_preferences"].locale, "en_US", "T3b should use default formatting")

    def test_t3c(self) -> None:
        self.reset_state()
        attempts = {"count": 0}

        async def fail_store(statement_id: str, payload: bytes) -> str:
            attempts["count"] += 1
            raise self.module.StorageUnavailable("Simulated storage outage")

        restore = self.patch_attr(self.module.storage_client, "store", fail_store)
        try:
            response = self.make_request(idempotency_key="strict2-t3c")
        finally:
            restore()
        self.assert_equal(response.status_code, 503, "T3c should return StorageUnavailable")
        self.assert_equal(attempts["count"], 3, "T3c should exhaust 3 retry attempts")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a),
            ("T1b", self.test_t1b),
            ("T1c", self.test_t1c),
            ("T2a", self.test_t2a),
            ("T2b", self.test_t2b),
            ("T2c", self.test_t2c),
            ("T2d", self.test_t2d),
            ("T2e", self.test_t2e),
            ("T3a", self.test_t3a),
            ("T3b", self.test_t3b),
            ("T3c", self.test_t3c),
        ]
        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        print(f"\nOverall: {passed}/{len(results)} passed")
        return 0 if passed == len(results) else 1


def main() -> int:
    return StrictSpecSuite(load_target_module()).run_all()


if __name__ == "__main__":
    sys.exit(main())
