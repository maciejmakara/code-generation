#!/usr/bin/env python3
"""
Strict spec-conformance integration tests for POST /contacts/import.

Run:
  py -3 integration_tests_bulk_import_contacts_strict_spec_2.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from fastapi.testclient import TestClient


DEFAULT_AUTH = "Bearer operator:11111111-1111-1111-1111-111111111111"
DEFAULT_TENANT_ID = "22222222-2222-2222-2222-222222222222"


def load_target_module(module_name: str | None, module_path: str | None):
    if module_path:
        path = Path(module_path).resolve()
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load module from path: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        return module

    candidates = [name for name in [module_name, "bulk_import_contacts"] if name]
    for candidate in candidates:
        try:
            return importlib.import_module(candidate)
        except Exception:
            pass
    raise RuntimeError("Could not import target module. Use --module-path or --module-name.")


class StrictSpecSuite:
    def __init__(self, module: Any):
        self.module = module
        self.client = TestClient(module.app, raise_server_exceptions=False)

    def reset_state(self) -> None:
        self.module.db.idempotency_store.clear()
        self.module.db.contacts_by_tenant_key.clear()
        self.module.db.contact_records.clear()
        self.module.db.import_summaries.clear()
        self.module.event_bus.events.clear()
        self.module.storage.objects.clear()
        self.module.filter_service.seen_keys.clear()

    def seed_csv(self, file_ref: str, csv_text: str) -> str:
        payload = csv_text.encode("utf-8")
        checksum = hashlib.sha256(payload).hexdigest()
        self.module.storage.put_object(file_ref, payload)
        return checksum

    def post_import(self, *, file_ref: str, checksum_sha256: str, idempotency_key: str):
        return self.client.post(
            "/contacts/import",
            headers={
                "Authorization": DEFAULT_AUTH,
                "X-Tenant-Id": DEFAULT_TENANT_ID,
                "Idempotency-Key": idempotency_key,
            },
            json={
                "file_ref": file_ref,
                "format": "csv",
                "checksum_sha256": checksum_sha256,
            },
        )

    def get_summary(self, import_id: str):
        return self.client.get(f"/contacts/imports/{import_id}")

    def contact_count(self) -> int:
        tenant_uuid = UUID(DEFAULT_TENANT_ID)
        return sum(1 for c in self.module.db.contact_records.values() if c.tenant_id == tenant_uuid)

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

    def test_t1a(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t1a.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "a@example.com,+1,A,A,en,x\n"
            "b@example.com,+2,B,B,en,y\n"
            "c@example.com,+3,C,C,en,z\n",
        )
        response = self.post_import(file_ref="strict2-t1a.csv", checksum_sha256=checksum, idempotency_key="strict2-t1a")
        self.assert_equal(response.status_code, 202, "T1a should return 202 Accepted")
        body = response.json()
        import_id = body.get("importId") or body.get("import_id")
        self.assert_true(bool(import_id), "T1a body should contain importId")
        self.assert_true(bool(body.get("results_url") or body.get("resultsUrl")), "T1a should contain results link")
        self.assert_equal(self.contact_count(), 3, "T1a should persist all 3 contacts")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T1a importErrors should be empty")
        self.assert_equal(len(self.module.event_bus.events), 1, "T1a should publish one event")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 3, "T1a totalRows should be 3")

    def test_t1b(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t1b.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "solo@example.com,+1,Solo,User,en,one\n",
        )
        response = self.post_import(file_ref="strict2-t1b.csv", checksum_sha256=checksum, idempotency_key="strict2-t1b")
        self.assert_equal(response.status_code, 202, "T1b should return 202 Accepted")
        body = response.json()
        import_id = body.get("importId") or body.get("import_id")
        self.assert_true(bool(import_id), "T1b body should contain importId")
        self.assert_equal(self.contact_count(), 1, "T1b should persist 1 contact")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T1b importErrors should be empty")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 1, "T1b totalRows should be 1")

    def test_t1c(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t1c.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "a@example.com,+1,A,A,en,x\n"
            "b@example.com,+2,B,B,en,y\n"
            "c@example.com,+3,C,C,en,z\n",
        )
        first = self.post_import(file_ref="strict2-t1c.csv", checksum_sha256=checksum, idempotency_key="strict2-t1c")
        second = self.post_import(file_ref="strict2-t1c.csv", checksum_sha256=checksum, idempotency_key="strict2-t1c")
        self.assert_equal(first.status_code, 202, "T1c first call should return 202")
        self.assert_equal(second.status_code, 202, "T1c second call should return 202")
        first_id = first.json().get("importId") or first.json().get("import_id")
        second_id = second.json().get("importId") or second.json().get("import_id")
        self.assert_equal(second_id, first_id, "T1c should return same importId from cache")
        self.assert_equal(self.contact_count(), 3, "T1c should not create extra contacts")
        self.assert_equal(len(self.module.event_bus.events), 1, "T1c should not publish a second event")

    def test_t2a(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t2a.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "alice@example.com,+1,Alice,Smith,en,customer\n"
            "broken-email,+2,Bad,Email,en,invalid\n"
            "carol@example.com,+3,Carol,Stone,pl,prospect\n"
            "alice@example.com,+1,Alice,Smith,en,duplicate\n"
            "dave@example.com,+4,Dave,White,en,lead\n",
        )
        response = self.post_import(file_ref="strict2-t2a.csv", checksum_sha256=checksum, idempotency_key="strict2-t2a")
        self.assert_equal(response.status_code, 202, "T2a should return 202")
        import_id = response.json().get("importId") or response.json().get("import_id")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 2, "T2a should contain 2 importErrors")
        self.assert_equal(self.contact_count(), 3, "T2a should persist 3 contacts")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 5, "T2a totalRows should be 5")

    def test_t2b(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t2b.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "not-an-email,,Bad,One,en,x\n"
            ",,Bad,Two,en,y\n"
            "wrong@@example,,Bad,Three,en,z\n",
        )
        response = self.post_import(file_ref="strict2-t2b.csv", checksum_sha256=checksum, idempotency_key="strict2-t2b")
        self.assert_equal(response.status_code, 202, "T2b should return 202")
        import_id = response.json().get("importId") or response.json().get("import_id")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 3, "T2b should contain 3 importErrors")
        self.assert_equal(self.contact_count(), 0, "T2b should persist no contacts")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 3, "T2b totalRows should be 3")

    def test_t2c(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "strict2-t2c.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "dup@example.com,+1,A,A,en,x\n"
            "dup@example.com,+1,A,A,en,y\n",
        )
        response = self.post_import(file_ref="strict2-t2c.csv", checksum_sha256=checksum, idempotency_key="strict2-t2c")
        self.assert_equal(response.status_code, 202, "T2c should return 202")
        import_id = response.json().get("importId") or response.json().get("import_id")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 1, "T2c should contain 1 importError")
        self.assert_equal(self.contact_count(), 1, "T2c should persist 1 contact")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 2, "T2c totalRows should be 2")

    def test_t3a(self) -> None:
        self.reset_state()
        checksum = self.seed_csv("strict2-t3a.csv", "email,phone,first_name,last_name,locale,tags\n")
        response = self.post_import(file_ref="strict2-t3a.csv", checksum_sha256=checksum, idempotency_key="strict2-t3a")
        self.assert_equal(response.status_code, 202, "T3a should return 202")
        import_id = response.json().get("importId") or response.json().get("import_id")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T3a importErrors should be empty")
        self.assert_equal(self.module.event_bus.events[0]["payload"]["totalRows"], 0, "T3a totalRows should be 0")

    def test_t3b(self) -> None:
        self.reset_state()
        attempts = {"count": 0}
        original = self.module.storage.get_object

        def missing_file_retryable(file_ref: str) -> bytes:
            attempts["count"] += 1
            raise self.module.Retryable503Error("503")

        self.module.storage.get_object = missing_file_retryable
        try:
            response = self.post_import(file_ref="missing.csv", checksum_sha256="0" * 64, idempotency_key="strict2-t3b")
        finally:
            self.module.storage.get_object = original

        self.assert_equal(attempts["count"], 3, "T3b should retry storage 3 times")
        self.assert_equal(response.status_code, 503, "T3b should return StorageUnavailable")
        detail = response.json()["detail"]
        self.assert_true(detail.get("error") in {"StorageUnavailable", "storage_unavailable"}, "T3b should expose StorageUnavailable")
        self.assert_equal(self.contact_count(), 0, "T3b should not persist contacts")
        self.assert_equal(len(self.module.db.import_summaries), 0, "T3b should not persist summary")
        self.assert_equal(len(self.module.event_bus.events), 0, "T3b should not publish event")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a),
            ("T1b", self.test_t1b),
            ("T1c", self.test_t1c),
            ("T2a", self.test_t2a),
            ("T2b", self.test_t2b),
            ("T2c", self.test_t2c),
            ("T3a", self.test_t3a),
            ("T3b", self.test_t3b),
        ]
        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        print(f"\nOverall: {passed}/{len(results)} passed")
        return 0 if passed == len(results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-name")
    parser.add_argument("--module-path")
    return parser.parse_args()


def main() -> int:
    script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "script.py")
    module = load_target_module(None, script_path)
    return StrictSpecSuite(module).run_all()


if __name__ == "__main__":
    sys.exit(main())
