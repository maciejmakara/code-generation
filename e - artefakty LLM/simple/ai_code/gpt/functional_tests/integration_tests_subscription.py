#!/usr/bin/env python3
"""
Implementation-aware integration tests for DELETE /subscriptions/{subscriptionId}.

Run:
  py -3 integration_tests_subscription_2.py
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient


OWNER = "11111111-1111-1111-1111-111111111111"


def load_target_module():
    path = Path(__file__).resolve().parents[1] / "script.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load target module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


class Suite:
    def __init__(self, module: Any):
        self.module = module
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self._baseline = {
            sub_id: copy.deepcopy(sub)
            for sub_id, sub in module.InMemorySubscriptionRepository()._data.items()
        }

    def reset_state(self) -> None:
        self.module.repository._data = {
            sub_id: copy.deepcopy(sub) for sub_id, sub in self._baseline.items()
        }
        self.seed_additional_subscriptions()

    def seed_additional_subscriptions(self) -> None:
        customer_id = self.module.uuid.UUID(OWNER)
        fixtures = {
            self.module.uuid.UUID("33333333-3333-3333-3333-333333333333"): self.module.Subscription(
                id=self.module.uuid.UUID("33333333-3333-3333-3333-333333333333"),
                customer_id=customer_id,
                status=self.module.SubscriptionStatus.ACTIVE,
                has_outstanding_invoice=False,
                current_billing_cycle_locked=False,
            ),
            self.module.uuid.UUID("44444444-4444-4444-4444-444444444444"): self.module.Subscription(
                id=self.module.uuid.UUID("44444444-4444-4444-4444-444444444444"),
                customer_id=customer_id,
                status=self.module.SubscriptionStatus.ACTIVE,
                has_outstanding_invoice=False,
                current_billing_cycle_locked=False,
            ),
            self.module.uuid.UUID("55555555-5555-5555-5555-555555555555"): self.module.Subscription(
                id=self.module.uuid.UUID("55555555-5555-5555-5555-555555555555"),
                customer_id=customer_id,
                status=self.module.SubscriptionStatus.CANCELED,
                has_outstanding_invoice=False,
                current_billing_cycle_locked=False,
                cancel_reason="Already canceled",
                canceled_at=self.module.datetime.now(self.module.timezone.utc),
            ),
            self.module.uuid.UUID("66666666-6666-6666-6666-666666666666"): self.module.Subscription(
                id=self.module.uuid.UUID("66666666-6666-6666-6666-666666666666"),
                customer_id=customer_id,
                status=self.module.SubscriptionStatus.PAST_DUE,
                has_outstanding_invoice=True,
                current_billing_cycle_locked=False,
            ),
            self.module.uuid.UUID("77777777-7777-7777-7777-777777777777"): self.module.Subscription(
                id=self.module.uuid.UUID("77777777-7777-7777-7777-777777777777"),
                customer_id=customer_id,
                status=self.module.SubscriptionStatus.ACTIVE,
                has_outstanding_invoice=False,
                current_billing_cycle_locked=True,
            ),
        }
        self.module.repository._data.update(fixtures)

    def patch_attr(self, obj: Any, attr: str, new_value: Any) -> Callable[[], None]:
        original = getattr(obj, attr)
        setattr(obj, attr, new_value)
        return lambda: setattr(obj, attr, original)

    def delete(
        self,
        subscription_id: str,
        *,
        auth: str | None = f"Bearer customer:{OWNER}",
        cancel_reason: str | None = None,
    ):
        headers = {}
        if auth is not None:
            headers["Authorization"] = auth
        body = {"cancelReason": cancel_reason} if cancel_reason is not None else None
        return self.client.request(
            "DELETE", f"/subscriptions/{subscription_id}", headers=headers, json=body
        )

    def assert_equal(self, actual: Any, expected: Any, message: str) -> None:
        if actual != expected:
            raise AssertionError(f"{message}. Expected={expected!r}, actual={actual!r}")

    def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def install_spies(self) -> dict[str, Any]:
        captures = {"events": [], "emails": []}
        orig_pub = self.module.event_bus.publish_subscription_canceled
        orig_email = self.module.email_provider.send_cancellation_email

        def pub_spy(subscription) -> bool:
            captures["events"].append(subscription.id)
            return True

        async def email_spy(customer_id, subscription_id) -> bool:
            captures["emails"].append(
                {"customer_id": customer_id, "subscription_id": subscription_id}
            )
            return True

        restores = [
            self.patch_attr(
                self.module.event_bus, "publish_subscription_canceled", pub_spy
            ),
            self.patch_attr(
                self.module.email_provider, "send_cancellation_email", email_spy
            ),
        ]
        captures["restore"] = lambda: [restore() for restore in reversed(restores)]
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

    def test_t1a(self) -> None:
        self.reset_state()
        captures = self.install_spies()
        try:
            response = self.delete(
                "22222222-2222-2222-2222-222222222222", cancel_reason="too_expensive"
            )
        finally:
            captures["restore"]()
        self.assert_equal(response.status_code, 204, "T1a should return 204")
        sub = self.module.repository._data[
            self.module.uuid.UUID("22222222-2222-2222-2222-222222222222")
        ]
        self.assert_equal(
            sub.status,
            self.module.SubscriptionStatus.CANCELED,
            "T1a should cancel subscription",
        )
        self.assert_equal(len(captures["events"]), 1, "T1a should publish event")
        self.assert_equal(len(captures["emails"]), 1, "T1a should send email")

    def test_t1b(self) -> None:
        self.reset_state()
        response = self.delete("33333333-3333-3333-3333-333333333333")
        self.assert_equal(response.status_code, 204, "T1b should return 204")

    def test_t2a(self) -> None:
        self.reset_state()
        response = self.delete("55555555-5555-5555-5555-555555555555", cancel_reason="test")
        self.assert_equal(response.status_code, 422, "T2a should return 422")
        self.assert_equal(
            response.json()["detail"]["error"],
            "unprocessable_entity",
            "T2a should reject cancellation",
        )

    def test_t2b(self) -> None:
        self.reset_state()
        response = self.delete("66666666-6666-6666-6666-666666666666", cancel_reason="test")
        self.assert_equal(response.status_code, 422, "T2b should return 422")

    def test_t2c(self) -> None:
        self.reset_state()
        response = self.delete("77777777-7777-7777-7777-777777777777", cancel_reason="test")
        self.assert_equal(response.status_code, 422, "T2c should return 422 in current implementation")

    def test_t3a(self) -> None:
        self.reset_state()
        response = self.delete("22222222-2222-2222-2222-222222222222", auth="")
        self.assert_equal(response.status_code, 401, "T3a should return 401")

    def test_t3b(self) -> None:
        self.reset_state()
        response = self.delete(
            "22222222-2222-2222-2222-222222222222",
            auth="Bearer customer:33333333-3333-3333-3333-333333333333",
        )
        self.assert_equal(response.status_code, 403, "T3b should return 403")

    def test_t3c(self) -> None:
        self.reset_state()
        response = self.delete(
            "ffffffff-ffff-ffff-ffff-ffffffffffff", auth=f"Bearer customer:{OWNER}"
        )
        self.assert_equal(response.status_code, 404, "T3c should return 404")

    def run_all(self) -> int:
        scenarios = [
            ("T1a", self.test_t1a),
            ("T1b", self.test_t1b),
            ("T2a", self.test_t2a),
            ("T2b", self.test_t2b),
            ("T2c", self.test_t2c),
            ("T3a", self.test_t3a),
            ("T3b", self.test_t3b),
            ("T3c", self.test_t3c),
        ]
        results = {name: self.run_named(name, fn) for name, fn in scenarios}
        passed = sum(1 for ok in results.values() if ok)
        print(f"\nOverall: {passed}/{len(results)} passed")
        return 0 if passed == len(results) else 1


def main() -> int:
    return Suite(load_target_module()).run_all()


if __name__ == "__main__":
    sys.exit(main())
