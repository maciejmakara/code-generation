#!/usr/bin/env python3
"""
Integration tests for POST /contacts/import.

Run:
  py -3 integration_tests_bulk_import_contacts_2.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
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
        if not path.exists():
            raise FileNotFoundError(f"Module path does not exist: {path}")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load module from path: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        return module

    candidates = [name for name in [module_name, "bulk_import_contacts"] if name]
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return importlib.import_module(candidate)
        except Exception as exc:  # pragma: no cover
            last_error = exc

    raise RuntimeError("Could not import target module. Use --module-path or --module-name.") from last_error


class IntegrationTestSuite:
    def __init__(self, module: Any):
        self.module = module
        self.client = TestClient(module.app)

    def reset_state(self) -> None:
        self.module.db.idempotency_store.clear()
        self.module.db.contacts_by_tenant_key.clear()
        self.module.db.contact_records.clear()
        self.module.db.import_summaries.clear()
        self.module.event_bus.events.clear()
        self.module.storage.objects.clear()
        self.module.filter_service.seen_keys.clear()

    def seed_csv(self, file_ref: str, csv_text: str) -> str:
        data = csv_text.encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        self.module.storage.put_object(file_ref, data)
        return checksum

    def build_headers(
        self,
        *,
        auth_token: str = DEFAULT_AUTH,
        tenant_id: str = DEFAULT_TENANT_ID,
        idempotency_key: str,
    ) -> dict[str, str]:
        return {
            "Authorization": auth_token,
            "X-Tenant-Id": tenant_id,
            "Idempotency-Key": idempotency_key,
        }

    def post_import(
        self,
        *,
        file_ref: str,
        checksum_sha256: str,
        idempotency_key: str,
        auth_token: str = DEFAULT_AUTH,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        return self.client.post(
            "/contacts/import",
            headers=self.build_headers(
                auth_token=auth_token,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            ),
            json={
                "file_ref": file_ref,
                "format": "csv",
                "checksum_sha256": checksum_sha256,
            },
        )

    def get_summary(self, import_id: str):
        return self.client.get(f"/contacts/imports/{import_id}")

    def contact_count(self, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        tenant_uuid = UUID(tenant_id)
        return sum(1 for contact in self.module.db.contact_records.values() if contact.tenant_id == tenant_uuid)

    def event_count(self) -> int:
        return len(self.module.event_bus.events)

    def latest_event(self) -> dict[str, Any] | None:
        return self.module.event_bus.events[-1] if self.module.event_bus.events else None

    def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def assert_equal(self, actual: Any, expected: Any, message: str) -> None:
        if actual != expected:
            raise AssertionError(f"{message}. Expected={expected!r}, actual={actual!r}")

    def run_named(self, name: str, fn: Callable[[], None]) -> bool:
        print(f"\n=== {name} ===")
        try:
            fn()
            print(f"PASS {name}")
            return True
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            return False

    def extract_import_id(self, response) -> str:
        body = response.json()
        import_id = body.get("importId") or body.get("import_id")
        if not import_id:
            raise AssertionError("Response body must include importId")
        return import_id

    def test_t1a(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t1a.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "a@example.com,+1,A,A,en,x\n"
            "b@example.com,+2,B,B,en,y\n"
            "c@example.com,+3,C,C,en,z\n",
        )
        response = self.post_import(file_ref="t1a.csv", checksum_sha256=checksum, idempotency_key="t1a")
        self.assert_equal(response.status_code, 202, "T1a should return 202")
        import_id = self.extract_import_id(response)
        self.assert_equal(self.contact_count(), 3, "T1a should persist 3 contacts")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T1a errors should be empty")
        self.assert_equal(self.event_count(), 1, "T1a should publish one event")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 3, "T1a totalRows should be 3")

    def test_t1b_single_contact(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t1b.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "solo@example.com,+1,Solo,User,en,one\n",
        )
        response = self.post_import(file_ref="t1b.csv", checksum_sha256=checksum, idempotency_key="t1b")
        self.assert_equal(response.status_code, 202, "T1b should return 202")
        import_id = self.extract_import_id(response)
        self.assert_equal(self.contact_count(), 1, "T1b should persist 1 contact")
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T1b errors should be empty")
        self.assert_equal(self.event_count(), 1, "T1b should publish one event")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 1, "T1b totalRows should be 1")

    def test_t1c_idempotency(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t1c.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "a@example.com,+1,A,A,en,x\n"
            "b@example.com,+2,B,B,en,y\n"
            "c@example.com,+3,C,C,en,z\n",
        )
        first = self.post_import(file_ref="t1c.csv", checksum_sha256=checksum, idempotency_key="t1c")
        second = self.post_import(file_ref="t1c.csv", checksum_sha256=checksum, idempotency_key="t1c")
        self.assert_equal(first.status_code, 202, "T1c first call should return 202")
        self.assert_equal(second.status_code, 202, "T1c second call should return 202")
        self.assert_equal(self.extract_import_id(second), self.extract_import_id(first), "T1c should return same importId")
        self.assert_equal(self.contact_count(), 3, "T1c should not create extra contacts")
        self.assert_equal(self.event_count(), 1, "T1c should not publish second event")

    def test_t2a_mixed_valid_invalid(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t2a.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "alice@example.com,+1,Alice,Smith,en,customer\n"
            "broken-email,+2,Bad,Email,en,invalid\n"
            "carol@example.com,+3,Carol,Stone,pl,prospect\n"
            "alice@example.com,+1,Alice,Smith,en,duplicate\n"
            "dave@example.com,+4,Dave,White,en,lead\n",
        )
        response = self.post_import(file_ref="t2a.csv", checksum_sha256=checksum, idempotency_key="t2a")
        self.assert_equal(response.status_code, 202, "T2a should return 202")
        import_id = self.extract_import_id(response)
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 2, "T2a should contain 2 errors")
        self.assert_equal(self.contact_count(), 3, "T2a should persist 3 contacts")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 5, "T2a totalRows should be 5")

    def test_t2c_duplicates_in_batch(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t2c.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "dup@example.com,+1,A,A,en,x\n"
            "dup@example.com,+1,A,A,en,y\n",
        )
        response = self.post_import(file_ref="t2c.csv", checksum_sha256=checksum, idempotency_key="t2c")
        self.assert_equal(response.status_code, 202, "T2c should return 202")
        import_id = self.extract_import_id(response)
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 1, "T2c should contain 1 error")
        self.assert_equal(self.contact_count(), 1, "T2c should persist 1 contact")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 2, "T2c totalRows should be 2")

    def test_t3a_header_only(self) -> None:
        self.reset_state()
        checksum = self.seed_csv("t3a.csv", "email,phone,first_name,last_name,locale,tags\n")
        response = self.post_import(file_ref="t3a.csv", checksum_sha256=checksum, idempotency_key="t3a")
        self.assert_equal(response.status_code, 202, "T3a should return 202")
        import_id = self.extract_import_id(response)
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 0, "T3a errors should be empty")
        self.assert_equal(self.contact_count(), 0, "T3a should persist 0 contacts")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 0, "T3a totalRows should be 0")

    def test_t3b_storage_unavailable(self) -> None:
        self.reset_state()
        attempts = {"count": 0}
        original = self.module.storage.get_object

        def missing_file_retryable(file_ref: str) -> bytes:
            attempts["count"] += 1
            raise self.module.Retryable503Error("503")

        self.module.storage.get_object = missing_file_retryable
        try:
            response = self.post_import(file_ref="missing.csv", checksum_sha256="0" * 64, idempotency_key="t3b")
        finally:
            self.module.storage.get_object = original

        self.assert_equal(attempts["count"], 3, "T3b should retry storage 3 times")
        self.assert_equal(response.status_code, 503, "T3b should return 503")
        self.assert_equal(self.contact_count(), 0, "T3b should not persist contacts")
        self.assert_equal(len(self.module.db.import_summaries), 0, "T3b should not persist summary")
        self.assert_equal(self.event_count(), 0, "T3b should not publish event")

    def test_t2b_all_invalid_rows(self) -> None:
        self.reset_state()
        checksum = self.seed_csv(
            "t2b.csv",
            "email,phone,first_name,last_name,locale,tags\n"
            "not-an-email,,Bad,One,en,x\n"
            ",,Bad,Two,en,y\n"
            "wrong@@example,,Bad,Three,en,z\n",
        )
        response = self.post_import(file_ref="t2b.csv", checksum_sha256=checksum, idempotency_key="t2b")
        self.assert_equal(response.status_code, 202, "T2b should return 202")
        import_id = self.extract_import_id(response)
        summary = self.get_summary(import_id).json()
        self.assert_equal(len(summary["errors"]), 3, "T2b should contain 3 errors")
        self.assert_equal(self.contact_count(), 0, "T2b should persist 0 contacts")
        self.assert_equal(self.latest_event()["payload"]["totalRows"], 3, "T2b totalRows should be 3")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a),
            ("T1b", self.test_t1b_single_contact),
            ("T1c", self.test_t1c_idempotency),
            ("T2a", self.test_t2a_mixed_valid_invalid),
            ("T2b", self.test_t2b_all_invalid_rows),
            ("T2c", self.test_t2c_duplicates_in_batch),
            ("T3a", self.test_t3a_header_only),
            ("T3b", self.test_t3b_storage_unavailable),
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
    args = parse_args()
    script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "script.py")
    module = load_target_module(args.module_name, args.module_path or script_path)
    return IntegrationTestSuite(module).run_all()


if __name__ == "__main__":
    sys.exit(main())
