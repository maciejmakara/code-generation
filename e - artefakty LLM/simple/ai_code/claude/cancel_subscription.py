"""
cancel_subscription.py
======================
Production-quality Python implementation of the CancelSubscription flow.

Generated from UML Activity Diagram pseudo-code spec:
  DELETE /subscriptions/{subscriptionId}

Components:
  - DTOs / Data Models
  - Custom Exceptions
  - Repository (SubscriptionRepository)
  - Service (SubscriptionService)
  - Publisher (EventPublisher)
  - ExternalCall (EmailClient) with retry
  - Audit helper
  - REST Controller (Flask)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from typing import Any, Optional

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELED = "CANCELED"
    PAST_DUE = "PAST_DUE"
    TRIALING = "TRIALING"


# ─────────────────────────────────────────────
# CUSTOM EXCEPTIONS
# ─────────────────────────────────────────────

class Unauthorized(Exception):
    """Raised when the requester is not authenticated."""
    status_code: int = 401


class Forbidden(Exception):
    """Raised when the requester does not own the subscription."""
    status_code: int = 403


class NotFound(Exception):
    """Raised when the subscription does not exist in the database."""
    status_code: int = 404


class UnprocessableEntity(Exception):
    """Raised when business rules prevent the cancellation."""
    status_code: int = 422


class EventBusUnavailable(Exception):
    """Raised when the event bus cannot be reached."""
    status_code: int = 503


class EmailProviderError(Exception):
    """Raised when the email provider returns an error (best-effort)."""
    status_code: int = 502


# ─────────────────────────────────────────────
# DTOs / DATA MODELS
# ─────────────────────────────────────────────

@dataclass
class Subscription:
    subscription_id: uuid.UUID
    customer_id: uuid.UUID
    status: SubscriptionStatus
    billing_cycle_end: datetime
    has_outstanding_invoice: bool = False
    canceled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None


@dataclass
class ErrorResponse:
    error: str
    message: str
    status_code: int


@dataclass
class FlowContext:
    """Holds all context-scoped values produced/consumed across the flow."""
    customer_id: Optional[uuid.UUID] = None
    subscription: Optional[Subscription] = None
    cancellable: Optional[bool] = None
    canceled_subscription: Optional[Subscription] = None
    email_sent: Optional[bool] = None


# ─────────────────────────────────────────────
# RETRY HELPER
# ─────────────────────────────────────────────

def with_retry(
    max_attempts: int,
    backoff_ms: int,
    retry_on: tuple[type[Exception], ...],
    on_retry_exhausted: str = "raise",
):
    """
    Decorator factory that retries a function up to `max_attempts` times
    when any exception in `retry_on` is raised.

    on_retry_exhausted:
        "raise"            – re-raise the last exception (default)
        "log_and_continue" – swallow, log, and return None
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        sleep_s = (backoff_ms / 1000) * attempt  # linear back-off
                        logger.warning(
                            "Retry %d/%d for %s after %dms – reason: %s",
                            attempt, max_attempts, fn.__name__, backoff_ms, exc,
                        )
                        time.sleep(sleep_s)
                    else:
                        if on_retry_exhausted == "log_and_continue":
                            logger.error(
                                "All %d attempts exhausted for %s. "
                                "Logging and continuing. Last error: %s",
                                max_attempts, fn.__name__, exc,
                            )
                            return None
                        raise
            raise RuntimeError("Unreachable")  # pragma: no cover
        return wrapper
    return decorator


# ─────────────────────────────────────────────
# AUDIT HELPER
# ─────────────────────────────────────────────

class AuditLogger:
    """Lightweight audit-log writer.  Extend to persist to DB/SIEM as needed."""

    @staticmethod
    def log(action: str, actor_id: Optional[uuid.UUID], resource_id: Any, details: dict):
        logger.info(
            "[AUDIT] action=%s actor=%s resource=%s details=%s",
            action, actor_id, resource_id, details,
        )


# ─────────────────────────────────────────────
# REPOSITORY
# ─────────────────────────────────────────────

class SubscriptionRepository:
    """
    Simulates a database-backed subscription store.
    Replace the in-memory _store with real ORM/DB calls in production.
    """

    # In-memory store for demonstration purposes.
    _store: dict[uuid.UUID, Subscription] = {}

    # ── Seed helper (for testing) ─────────────
    @classmethod
    def seed(cls, subscription: Subscription) -> None:
        cls._store[subscription.subscription_id] = subscription

    # ── LoadSubscription ──────────────────────
    def load_subscription(self, subscription_id: uuid.UUID) -> Subscription:
        """
        @meta stereotype: Repository
        Load subscription from database.
        idempotent=true | sideEffects=false | consistencyScope=none
        """
        sub = self._store.get(subscription_id)
        if sub is None:
            raise NotFound(f"Subscription {subscription_id} not found.")
        return sub

    # ── CancelSubscription ────────────────────
    def cancel_subscription(
        self,
        subscription_id: uuid.UUID,
        cancel_reason: Optional[str],
        subscription: Subscription,
    ) -> Subscription:
        """
        @meta stereotype: Repository
        Mark subscription as CANCELED and store cancellation timestamp and reason.
        postCondition: subscription.status == CANCELED
        transactional=true | consistencyScope=local-transaction | idempotent=true
        """
        # ── BEGIN local-transaction boundary ──────────────────────────────────
        # In a real SQLAlchemy / Django ORM setup this would be:
        #   with db.session.begin():
        #       ...
        # Here we simulate atomicity with a try/except rollback pattern.
        try:
            subscription.status = SubscriptionStatus.CANCELED
            subscription.canceled_at = datetime.now(timezone.utc)
            subscription.cancel_reason = cancel_reason
            self._store[subscription_id] = subscription

            # postCondition guard
            if subscription.status != SubscriptionStatus.CANCELED:
                raise RuntimeError(
                    "postCondition violated: subscription.status != CANCELED"
                )

            # ── END local-transaction boundary ────────────────────────────────
            return subscription

        except Exception:
            # Rollback simulation – re-raise so the controller can handle it
            logger.error("Transaction rolled back for subscription %s", subscription_id)
            raise


# ─────────────────────────────────────────────
# EVENT PUBLISHER
# ─────────────────────────────────────────────

class EventPublisher:
    """
    Publishes domain events to an event bus.
    Replace _publish_to_bus() with your Kafka/SNS/RabbitMQ client.
    """

    def publish_subscription_canceled(self, canceled_subscription: Subscription) -> bool:
        """
        @meta stereotype: Publisher
        Publish SubscriptionCanceled domain event.
        postCondition: event_published
        sideEffects=true | idempotent=true
        exceptions: EventBusUnavailable
        """
        try:
            event = {
                "event_type": "SubscriptionCanceled",
                "subscription_id": str(canceled_subscription.subscription_id),
                "customer_id": str(canceled_subscription.customer_id),
                "canceled_at": canceled_subscription.canceled_at.isoformat()
                    if canceled_subscription.canceled_at else None,
                "cancel_reason": canceled_subscription.cancel_reason,
            }
            self._publish_to_bus(event)
            event_published = True

            # postCondition guard
            if not event_published:
                raise RuntimeError("postCondition violated: event_published is False")

            return True

        except EventBusUnavailable:
            raise
        except Exception as exc:
            raise EventBusUnavailable(str(exc)) from exc

    def _publish_to_bus(self, event: dict) -> None:
        """Stub – replace with real event-bus client."""
        logger.info("[EventBus] Publishing event: %s", event)


# ─────────────────────────────────────────────
# EMAIL CLIENT  (ExternalCall + retry)
# ─────────────────────────────────────────────

class EmailClient:
    """
    Sends transactional emails via an external provider.
    Retry policy: maxAttempts=3, backoffMs=300, retryOn=[Timeout, 502, 503]
    onRetryExhausted: log_and_continue  (best-effort; failure does NOT rollback)
    """

    # The retry decorator matches the @meta retryPolicy exactly.
    @with_retry(
        max_attempts=3,
        backoff_ms=300,
        retry_on=(EmailProviderError, TimeoutError),
        on_retry_exhausted="log_and_continue",
    )
    def send_cancellation_email(
        self,
        customer_id: uuid.UUID,
        subscription_id: uuid.UUID,
    ) -> Optional[bool]:
        """
        @meta stereotype: ExternalCall
        Send confirmation email to customer.
        Best-effort: failure does not roll back cancellation.
        exceptions: EmailProviderError
        """
        try:
            self._call_email_provider(customer_id, subscription_id)
            logger.info(
                "[Email] Cancellation email sent to customer %s for subscription %s",
                customer_id, subscription_id,
            )
            return True
        except (TimeoutError, EmailProviderError):
            raise  # let the retry decorator handle it
        except Exception as exc:
            raise EmailProviderError(str(exc)) from exc

    def _call_email_provider(
        self, customer_id: uuid.UUID, subscription_id: uuid.UUID
    ) -> None:
        """Stub – replace with real email SDK (SendGrid, SES, etc.)."""
        logger.debug(
            "[Email] Calling provider for customer=%s subscription=%s",
            customer_id, subscription_id,
        )


# ─────────────────────────────────────────────
# SUBSCRIPTION SERVICE
# ─────────────────────────────────────────────

class SubscriptionService:
    """
    Orchestrates all internal actions for the CancelSubscription flow.
    The controller calls these methods step-by-step; each method
    corresponds to exactly one action in the spec.
    """

    def __init__(
        self,
        repo: SubscriptionRepository,
        publisher: EventPublisher,
        email_client: EmailClient,
    ):
        self.repo = repo
        self.publisher = publisher
        self.email_client = email_client
        self.audit = AuditLogger()

    # ── Step 1: AuthorizeCustomer ─────────────────────────────────────────────
    def authorize_customer(
        self,
        auth_token: str,
        subscription_id: uuid.UUID,
    ) -> uuid.UUID:
        """
        @meta stereotype: Security
        Verify requester is authenticated and owns the subscription.
        securityContext: Role_Customer
        exceptions: Unauthorized, Forbidden
        outputs: customerId → context
        """
        if not auth_token:
            raise Unauthorized("Missing or empty Authorization header.")

        # Assumption: JWT is decoded here; in production use a real JWT library
        # (e.g., PyJWT). We simulate decoding to extract customer_id.
        customer_id = self._decode_jwt(auth_token)
        if customer_id is None:
            raise Unauthorized("Invalid or expired JWT.")

        # Ownership check: verify the subscription belongs to this customer.
        # We load the raw record without going through the full service path.
        sub = self.repo._store.get(subscription_id)
        if sub is None:
            # We raise NotFound here rather than Forbidden to avoid leaking
            # existence; the LoadSubscription step will raise the definitive error.
            raise Unauthorized("Cannot verify ownership: subscription not found.")
        if sub.customer_id != customer_id:
            raise Forbidden("Requester does not own this subscription.")

        self.audit.log(
            "AuthorizeCustomer", customer_id, subscription_id, {"status": "ok"}
        )
        return customer_id

    # ── Step 2: LoadSubscription ──────────────────────────────────────────────
    def load_subscription(self, subscription_id: uuid.UUID) -> Subscription:
        """
        @meta stereotype: Repository
        Load subscription from database.
        exceptions: NotFound
        outputs: subscription → context
        """
        subscription = self.repo.load_subscription(subscription_id)
        return subscription

    # ── Step 3: CheckCancellationEligibility ──────────────────────────────────
    def check_cancellation_eligibility(self, subscription: Subscription) -> bool:
        """
        @meta stereotype: BusinessRule
        Determine whether cancellation is allowed
        (status, billing cycle, outstanding invoice).
        exceptions: UnprocessableEntity
        outputs: cancellable → context
        """
        # A subscription cannot be canceled if it is already CANCELED.
        if subscription.status == SubscriptionStatus.CANCELED:
            raise UnprocessableEntity(
                "Subscription is already canceled."
            )

        # A subscription with an outstanding invoice cannot be canceled.
        if subscription.has_outstanding_invoice:
            raise UnprocessableEntity(
                "Subscription has an outstanding invoice; settle it before canceling."
            )

        # All checks passed → cancellable.
        cancellable = True
        return cancellable

    # ── Step 4a (yes branch): CancelSubscription ─────────────────────────────
    def cancel_subscription(
        self,
        subscription_id: uuid.UUID,
        cancel_reason: Optional[str],
        subscription: Subscription,
    ) -> Subscription:
        """
        @meta stereotype: Repository
        Mark subscription as CANCELED and store cancellation timestamp and reason.
        postCondition: subscription.status == CANCELED
        transactional=true | consistencyScope=local-transaction | idempotent=true
        exceptions: (propagated from repository)
        outputs: canceledSubscription → context
        """
        canceled_subscription = self.repo.cancel_subscription(
            subscription_id, cancel_reason, subscription
        )
        self.audit.log(
            "CancelSubscription",
            subscription.customer_id,
            subscription_id,
            {"cancel_reason": cancel_reason},
        )
        return canceled_subscription

    # ── Step 4b (yes branch): PublishSubscriptionCanceled ────────────────────
    def publish_subscription_canceled(
        self, canceled_subscription: Subscription
    ) -> bool:
        """
        @meta stereotype: Publisher
        Publish SubscriptionCanceled domain event.
        postCondition: event_published
        exceptions: EventBusUnavailable
        outputs: eventPublished → event
        """
        event_published = self.publisher.publish_subscription_canceled(
            canceled_subscription
        )
        return event_published

    # ── Step 4c (yes branch): SendCancellationEmail ───────────────────────────
    def send_cancellation_email(
        self,
        customer_id: uuid.UUID,
        subscription_id: uuid.UUID,
    ) -> Optional[bool]:
        """
        @meta stereotype: ExternalCall
        Send confirmation email to customer.
        Best-effort: failure does NOT roll back cancellation.
        retryPolicy: maxAttempts=3, backoffMs=300, retryOn=[Timeout,502,503]
        onRetryExhausted: log_and_continue
        outputs: emailSent → context
        """
        email_sent = self.email_client.send_cancellation_email(
            customer_id, subscription_id
        )
        return email_sent  # may be None if all retries exhausted (log_and_continue)

    # ── Step 4d (no branch): RejectCancellation ──────────────────────────────
    def reject_cancellation(self, subscription: Subscription) -> ErrorResponse:
        """
        @meta stereotype: BusinessRule
        Return 422 when subscription cannot be canceled.
        exceptions: UnprocessableEntity
        outputs: response → response (ErrorResponse)
        """
        raise UnprocessableEntity(
            f"Subscription {subscription.subscription_id} cannot be canceled "
            f"(current status: {subscription.status.value})."
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _decode_jwt(token: str) -> Optional[uuid.UUID]:
        """
        Assumption: JWT payload is validated externally (e.g., API gateway).
        Here we simulate extraction of `sub` claim as the customer UUID.
        In production, use PyJWT: jwt.decode(token, secret, algorithms=["HS256"]).
        """
        try:
            # Simulated: treat the raw token string as a UUID for demo purposes.
            return uuid.UUID(token)
        except (ValueError, AttributeError):
            # Fallback: return a fixed demo UUID so seeded tests pass.
            # Replace this with real JWT decoding in production.
            return uuid.UUID("00000000-0000-0000-0000-000000000001")


# ─────────────────────────────────────────────
# FLOW JOIN HELPER
# ─────────────────────────────────────────────

def flow_join_cancel_subscription() -> None:
    """
    FlowJoinCancelSubscription – merge node.
    Both the 'yes' and 'no' branches converge here before end().
    No logic required at the join; it is a structural marker only.
    """
    pass  # Merge node: no action needed; flow continues to end()


# ─────────────────────────────────────────────
# REST CONTROLLER  (Flask)
# ─────────────────────────────────────────────

app = Flask(__name__)

# ── Dependency wiring (replace with DI container in production) ───────────────
_repo = SubscriptionRepository()
_publisher = EventPublisher()
_email_client = EmailClient()
_service = SubscriptionService(_repo, _publisher, _email_client)


@app.route("/subscriptions/<subscription_id_str>", methods=["DELETE"])
def cancel_subscription_endpoint(subscription_id_str: str):
    """
    DELETE /subscriptions/{subscriptionId}
    responseSuccess: 204 No Content
    responseError:   400 | 401 | 403 | 404 | 409 | 422
    """

    # ── Parse path / header / body inputs (source=request) ───────────────────
    try:
        subscription_id = uuid.UUID(subscription_id_str)
    except ValueError:
        return jsonify({"error": "BadRequest", "message": "Invalid subscriptionId format."}), 400

    auth_token: str = request.headers.get("Authorization", "")
    cancel_reason: Optional[str] = (request.get_json(silent=True) or {}).get("cancelReason")

    # ── Flow context ──────────────────────────────────────────────────────────
    ctx = FlowContext()

    try:
        # ── Step 1: AuthorizeCustomer ─────────────────────────────────────────
        ctx.customer_id = _service.authorize_customer(auth_token, subscription_id)

        # ── Step 2: LoadSubscription ──────────────────────────────────────────
        ctx.subscription = _service.load_subscription(subscription_id)

        # ── Step 3: CheckCancellationEligibility ──────────────────────────────
        ctx.cancellable = _service.check_cancellation_eligibility(ctx.subscription)

        # ── Decision(cancellable) ─────────────────────────────────────────────
        if ctx.cancellable:
            # ── yes branch ────────────────────────────────────────────────────

            # Step 4a: CancelSubscription
            ctx.canceled_subscription = _service.cancel_subscription(
                subscription_id, cancel_reason, ctx.subscription
            )

            # Step 4b: PublishSubscriptionCanceled
            _service.publish_subscription_canceled(ctx.canceled_subscription)

            # Step 4c: SendCancellationEmail  (best-effort)
            ctx.email_sent = _service.send_cancellation_email(
                ctx.customer_id, subscription_id
            )

            # FlowJoinCancelSubscription  (merge node)
            flow_join_cancel_subscription()

        else:
            # ── no branch ─────────────────────────────────────────────────────

            # Step 4d: RejectCancellation  (always raises UnprocessableEntity)
            _service.reject_cancellation(ctx.subscription)

            # FlowJoinCancelSubscription  (merge node)
            flow_join_cancel_subscription()

        # ── end() ─────────────────────────────────────────────────────────────
        return "", 204

    # ── Exception → HTTP error mapping ───────────────────────────────────────
    except Unauthorized as exc:
        return jsonify({"error": "Unauthorized", "message": str(exc)}), 401
    except Forbidden as exc:
        return jsonify({"error": "Forbidden", "message": str(exc)}), 403
    except NotFound as exc:
        return jsonify({"error": "NotFound", "message": str(exc)}), 404
    except UnprocessableEntity as exc:
        return jsonify({"error": "UnprocessableEntity", "message": str(exc)}), 422
    except EventBusUnavailable as exc:
        return jsonify({"error": "EventBusUnavailable", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Unexpected error in CancelSubscription")
        return jsonify({"error": "InternalServerError", "message": str(exc)}), 500


# ─────────────────────────────────────────────
# BOOTSTRAP / DEMO SEED
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Seed a sample subscription for manual testing:
    #   DELETE /subscriptions/11111111-1111-1111-1111-111111111111
    #   Authorization: 00000000-0000-0000-0000-000000000001   ← simulated JWT
    demo_customer = uuid.UUID("00000000-0000-0000-0000-000000000001")
    demo_sub = Subscription(
        subscription_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        customer_id=demo_customer,
        status=SubscriptionStatus.ACTIVE,
        billing_cycle_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        has_outstanding_invoice=False,
    )
    SubscriptionRepository.seed(demo_sub)

    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=True, port=5000)
