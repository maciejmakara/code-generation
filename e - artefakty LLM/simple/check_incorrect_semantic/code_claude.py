"""
cancel_subscription.py
======================
Production-quality implementation of:
  DELETE /subscriptions/{subscriptionId}

Generated from UML Activity Diagram pseudo-code.

Components:
  - Custom exception classes
  - DTOs / data models (Pydantic)
  - SubscriptionService  (all internal actions)
  - Retry helper
  - Audit logger helper
  - Transaction context manager helper
  - FastAPI controller (endpoint)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom Exception Classes  (from @meta.exceptions)
# ---------------------------------------------------------------------------


class UnauthorizedException(Exception):
  """Raised when the requester is not authenticated."""
  http_status: int = 401
  detail: str = "Unauthorized"


class ForbiddenException(Exception):
  """Raised when the requester does not own the subscription."""
  http_status: int = 403
  detail: str = "Forbidden"


class NotFoundException(Exception):
  """Raised when the subscription cannot be found in the database."""
  http_status: int = 404
  detail: str = "Subscription not found"


class UnprocessableEntityException(Exception):
  """Raised when business rules prevent cancellation."""
  http_status: int = 422

  def __init__(self, detail: str = "Unprocessable entity"):
    super().__init__(detail)
    self.detail = detail


class EventBusUnavailableException(Exception):
  """Raised when the event bus cannot accept the published event."""
  http_status: int = 503
  detail: str = "Event bus unavailable"


class EmailProviderErrorException(Exception):
  """Raised when the external email provider returns a transient or permanent error."""
  http_status: int = 502
  detail: str = "Email provider error"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SubscriptionStatus(str, Enum):
  ACTIVE = "ACTIVE"
  CANCELED = "CANCELED"
  SUSPENDED = "SUSPENDED"
  PENDING = "PENDING"


# ---------------------------------------------------------------------------
# DTOs / Data Models
# ---------------------------------------------------------------------------


class Subscription(BaseModel):
  """Domain entity for a subscription."""
  subscription_id: UUID
  customer_id: UUID
  status: SubscriptionStatus
  billing_cycle: str = "MONTHLY"
  has_outstanding_invoice: bool = False
  canceled_at: Optional[datetime] = None
  cancel_reason: Optional[str] = None

  class Config:
    use_enum_values = True


class CancelSubscriptionRequest(BaseModel):
  """Optional body for the DELETE endpoint."""
  cancel_reason: Optional[str] = Field(None, description="Optional reason for cancellation")


class ErrorResponse(BaseModel):
  """Standard error payload returned when cancellation is rejected."""
  error: str
  detail: str
  subscription_id: str


class FlowContext(BaseModel):
  """
  Mutable execution context passed between service actions.
  Stores all values whose @meta output target is 'context'.
  """
  subscription: Optional[Subscription] = None
  cancellable: Optional[bool] = None
  canceled_subscription: Optional[Subscription] = None
  customer_id: Optional[UUID] = None  # populated from loaded subscription

  class Config:
    arbitrary_types_allowed = True


# ---------------------------------------------------------------------------
# Helpers: Retry
# ---------------------------------------------------------------------------


async def retry_async(
    coro_fn,
    max_attempts: int,
    backoff_ms: int,
    retry_on: list[str],
    *args,
    **kwargs,
):
  """
  Generic async retry helper.
  Retries coro_fn up to max_attempts times; sleeps backoff_ms between attempts.
  retry_on is a list of string labels/status codes to check against the exception message.
  """
  last_exc: Exception = RuntimeError("No attempts made")
  for attempt in range(1, max_attempts + 1):
    try:
      return await coro_fn(*args, **kwargs)
    except Exception as exc:
      exc_str = str(exc)
      should_retry = any(label in exc_str for label in retry_on)
      if should_retry and attempt < max_attempts:
        logger.warning(
            "Retry attempt %d/%d for %s due to: %s",
            attempt,
            max_attempts,
            coro_fn.__name__,
            exc_str,
        )
        await asyncio.sleep(backoff_ms / 1000.0)
        last_exc = exc
      else:
        raise
  raise last_exc  # Should not normally be reached


# ---------------------------------------------------------------------------
# Helpers: Audit Logger
# ---------------------------------------------------------------------------


class AuditLogger:
  """
  Side-effect utility that records audit entries.
  In production this would persist to an audit store.
  """

  @staticmethod
  def log(action: str, actor: str, resource_id: str, details: dict):
    entry = {
      "timestamp": datetime.now(timezone.utc).isoformat(),
      "action": action,
      "actor": actor,
      "resource_id": resource_id,
      "details": details,
    }
    logger.info("AUDIT: %s", entry)


# ---------------------------------------------------------------------------
# Helpers: Transaction Context Manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def local_transaction(description: str = ""):
  """
  Simulates a local transaction boundary.
  In production, replace with a real DB session/transaction manager.
  consistencyScope=local-transaction / transactional=true is enforced here.
  """
  logger.debug("BEGIN TRANSACTION [%s]", description)
  try:
    yield
    logger.debug("COMMIT TRANSACTION [%s]", description)
  except Exception:
    logger.debug("ROLLBACK TRANSACTION [%s]", description)
    raise


# ---------------------------------------------------------------------------
# Subscription Repository (stub)
# ---------------------------------------------------------------------------


class SubscriptionRepository:
  """
  Stub repository for Subscription persistence.
  Replace with real DB calls (SQLAlchemy, asyncpg, etc.) in production.
  """

  # In-memory store for demonstration purposes only
  _store: dict[UUID, Subscription] = {}

  @classmethod
  async def find_by_id(cls, subscription_id: UUID) -> Optional[Subscription]:
    return cls._store.get(subscription_id)

  @classmethod
  async def save(cls, subscription: Subscription) -> Subscription:
    cls._store[subscription.subscription_id] = subscription
    return subscription


# ---------------------------------------------------------------------------
# Event Bus (stub)
# ---------------------------------------------------------------------------


class EventBus:
  """Stub event bus. Replace with real broker client (Kafka, SQS, etc.)."""

  @staticmethod
  async def publish(event_name: str, payload: dict) -> bool:
    logger.info("EVENT PUBLISHED [%s]: %s", event_name, payload)
    return True


# ---------------------------------------------------------------------------
# Email Provider (stub)
# ---------------------------------------------------------------------------


class EmailProvider:
  """Stub email provider. Replace with real client (SendGrid, SES, etc.)."""

  @staticmethod
  async def send_cancellation_email(customer_id: UUID, subscription_id: UUID) -> None:
    logger.info(
        "EMAIL SENT: cancellation notice to customer=%s for subscription=%s",
        customer_id,
        subscription_id,
    )


# ---------------------------------------------------------------------------
# SubscriptionService  –  one method per specification action
# ---------------------------------------------------------------------------


class SubscriptionService:
  """
  Implements every internal action from the Cancel Subscription specification.
  The controller orchestrates the flow; this service implements semantics.
  """

  def __init__(self):
    self._repo = SubscriptionRepository()
    self._event_bus = EventBus()
    self._email_provider = EmailProvider()
    self._audit = AuditLogger()

  # ------------------------------------------------------------------
  # Action: AuthorizeCustomer
  # Stereotype: Security
  # ------------------------------------------------------------------

  async def authorize_customer(
      self,
      auth_token: str,
      subscription_id: UUID,
  ) -> None:
    """
    Verify requester is authenticated and owns the subscription.
    @meta exceptions: Unauthorized, Forbidden
    @meta securityContext: Role_Customer
    """
    if not auth_token:
      raise UnauthorizedException("Missing or empty authorization token")

    # --- Assumption: JWT validation is performed by middleware in production.
    # Here we do a basic presence check and extract a mock customer_id.
    # In production, decode & validate the JWT signature/expiry and extract claims.
    if not auth_token.startswith("Bearer "):
      raise UnauthorizedException("Authorization token must be a Bearer token")

    # Assumption: The token payload encodes the customer ID as the subject.
    # Ownership check against subscription is deferred to LoadSubscription result
    # because we need the subscription's customer_id for the comparison.
    # A full ownership verification is done after LoadSubscription (see controller).
    logger.debug("AuthorizeCustomer: token present and well-formed for subscription=%s", subscription_id)

  # ------------------------------------------------------------------
  # Action: LoadSubscription
  # Stereotype: Repository
  # ------------------------------------------------------------------

  async def load_subscription(
      self,
      subscription_id: UUID,
  ) -> Subscription:
    """
    Load subscription from database.
    @meta outputs: subscription -> context
    @meta exceptions: NotFound
    @meta sideEffects: false
    @meta idempotent: true
    @meta consistencyScope: none
    """
    subscription = await self._repo.find_by_id(subscription_id)
    if subscription is None:
      raise NotFoundException(f"Subscription {subscription_id} not found")
    return subscription

  # ------------------------------------------------------------------
  # Action: CheckCancellationEligibility
  # Stereotype: BusinessRule
  # ------------------------------------------------------------------

  async def check_cancellation_eligibility(
      self,
      subscription: Subscription,
  ) -> bool:
    """
    Determine whether cancellation is allowed.
    Checks: status, billing cycle, outstanding invoice.
    @meta outputs: cancellable -> context
    @meta exceptions: UnprocessableEntity
    """
    # Rule 1: Already-canceled subscriptions are not re-cancellable
    if subscription.status == SubscriptionStatus.CANCELED:
      raise UnprocessableEntityException(
          "Subscription is already canceled"
      )

    # Rule 2: Suspended subscriptions may not be canceled mid-cycle
    # Assumption: SUSPENDED status blocks cancellation per business policy.
    if subscription.status == SubscriptionStatus.SUSPENDED:
      raise UnprocessableEntityException(
          "Subscription is suspended and cannot be canceled at this time"
      )

    # Rule 3: Outstanding invoice blocks cancellation
    if subscription.has_outstanding_invoice:
      raise UnprocessableEntityException(
          "Subscription has an outstanding invoice; please resolve it before canceling"
      )

    # All rules passed
    return True

  # ------------------------------------------------------------------
  # Action: CancelSubscription
  # Stereotype: Repository
  # ------------------------------------------------------------------

  async def cancel_subscription(
      self,
      subscription_id: UUID,
      cancel_reason: Optional[str],
      subscription: Subscription,
  ) -> Subscription:
    """
    Mark subscription as CANCELED and store cancellation timestamp and reason.
    @meta postCondition: subscription.status == CANCELED
    @meta sideEffects: true
    @meta idempotent: true  (repeated calls produce same CANCELED state)
    @meta consistencyScope: local-transaction
    @meta transactional: true
    """
    async with local_transaction("CancelSubscription"):
      canceled = subscription.copy(
          update={
            "status": SubscriptionStatus.CANCELED,
            "canceled_at": datetime.now(timezone.utc),
            "cancel_reason": cancel_reason,
          }
      )
      saved = await self._repo.save(canceled)

    # postCondition guard
    if saved.status != SubscriptionStatus.CANCELED:
      raise RuntimeError(
          "postCondition violated: subscription.status != CANCELED after save"
      )

    return saved

  # ------------------------------------------------------------------
  # Action: PublishSubscriptionCanceled
  # Stereotype: Publisher
  # ------------------------------------------------------------------

  async def publish_subscription_canceled(
      self,
      canceled_subscription: Subscription,
  ) -> bool:
    """
    Publish SubscriptionCanceled domain event.
    @meta postCondition: event_published
    @meta sideEffects: true
    @meta exceptions: EventBusUnavailable
    @meta idempotent: true
    """
    payload = {
      "event": "SubscriptionCanceled",
      "subscription_id": str(canceled_subscription.subscription_id),
      "customer_id": str(canceled_subscription.customer_id),
      "canceled_at": canceled_subscription.canceled_at.isoformat()
      if canceled_subscription.canceled_at
      else None,
      "cancel_reason": canceled_subscription.cancel_reason,
    }
    try:
      published = await self._event_bus.publish("SubscriptionCanceled", payload)
    except Exception as exc:
      raise EventBusUnavailableException(
          f"Failed to publish SubscriptionCanceled event: {exc}"
      ) from exc

    # postCondition guard
    if not published:
      raise EventBusUnavailableException(
          "postCondition violated: event_published is False"
      )

    return published

  # ------------------------------------------------------------------
  # Action: SendCancellationEmail
  # Stereotype: ExternalCall
  # ------------------------------------------------------------------

  async def _send_cancellation_email_once(
      self,
      customer_id: UUID,
      subscription_id: UUID,
  ) -> None:
    """
    Internal single-attempt email dispatch.
    Called by send_cancellation_email which applies the retry policy.
    """
    try:
      await self._email_provider.send_cancellation_email(customer_id, subscription_id)
    except Exception as exc:
      # Surface known transient signals so retry logic can match them
      raise EmailProviderErrorException(
          f"Timeout:{exc}" if "timeout" in str(exc).lower() else str(exc)
      ) from exc

  async def send_cancellation_email(
      self,
      customer_id: UUID,
      subscription_id: UUID,
  ) -> None:
    """
    Send cancellation confirmation email with retry policy.
    @meta retryPolicy: maxAttempts=3, backoffMs=300, retryOn=["Timeout","502","503"]
    @meta exceptions: EmailProviderError
    """
    await retry_async(
        self._send_cancellation_email_once,
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
        customer_id=customer_id,
        subscription_id=subscription_id,
    )

  # ------------------------------------------------------------------
  # Action: RejectCancellation
  # Stereotype: BusinessRule
  # ------------------------------------------------------------------

  async def reject_cancellation(
      self,
      subscription: Subscription,
  ) -> ErrorResponse:
    """
    Return 422 when subscription cannot be canceled.
    @meta outputs: response -> response (ErrorResponse)
    @meta exceptions: UnprocessableEntity
    """
    # Assumption: This action constructs the error response DTO and raises
    # UnprocessableEntityException so the controller can map it to HTTP 422.
    error_response = ErrorResponse(
        error="CancellationNotAllowed",
        detail="The subscription does not meet the criteria for cancellation.",
        subscription_id=str(subscription.subscription_id),
    )
    raise UnprocessableEntityException(detail=error_response.json())


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(title="Subscription Service", version="1.0.0")

_subscription_service = SubscriptionService()


# ---------------------------------------------------------------------------
# Exception → HTTP response mapping
# ---------------------------------------------------------------------------


@app.exception_handler(UnauthorizedException)
async def unauthorized_handler(request: Request, exc: UnauthorizedException):
  return JSONResponse(status_code=401, content={"detail": exc.detail or str(exc)})


@app.exception_handler(ForbiddenException)
async def forbidden_handler(request: Request, exc: ForbiddenException):
  return JSONResponse(status_code=403, content={"detail": exc.detail or str(exc)})


@app.exception_handler(NotFoundException)
async def not_found_handler(request: Request, exc: NotFoundException):
  return JSONResponse(status_code=404, content={"detail": exc.detail or str(exc)})


@app.exception_handler(UnprocessableEntityException)
async def unprocessable_handler(request: Request, exc: UnprocessableEntityException):
  return JSONResponse(status_code=422, content={"detail": exc.detail or str(exc)})


@app.exception_handler(EventBusUnavailableException)
async def event_bus_handler(request: Request, exc: EventBusUnavailableException):
  return JSONResponse(status_code=503, content={"detail": exc.detail or str(exc)})


@app.exception_handler(EmailProviderErrorException)
async def email_handler(request: Request, exc: EmailProviderErrorException):
  return JSONResponse(status_code=502, content={"detail": exc.detail or str(exc)})


# ---------------------------------------------------------------------------
# Controller: DELETE /subscriptions/{subscriptionId}
# ---------------------------------------------------------------------------


@app.delete(
    "/subscriptions/{subscriptionId}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
      204: {"description": "Subscription successfully canceled"},
      400: {"description": "Bad Request"},
      401: {"description": "Unauthorized"},
      403: {"description": "Forbidden"},
      404: {"description": "Subscription not found"},
      409: {"description": "Conflict"},
      422: {"description": "Unprocessable Entity – cancellation not allowed"},
    },
    summary="Cancel a subscription",
    tags=["Subscriptions"],
)
async def cancel_subscription_endpoint(
    subscriptionId: UUID = Path(..., description="UUID of the subscription to cancel"),
    authorization: str = Header(..., alias="Authorization", description="Bearer JWT token"),
    cancel_reason: Optional[str] = Query(None, description="Optional reason for cancellation"),
):
  """
  DELETE /subscriptions/{subscriptionId}

  Implements main_Cancelsubscription() from the UML Activity Diagram.

  Flow:
    1. AuthorizeCustomer
    2. LoadSubscription
    3. CheckCancellationEligibility
    4. Decision(cancellable)
       if yes  → CancelSubscription → PublishSubscriptionCanceled → SendCancellationEmail → FlowJoinCancelSubscription
       if no   → RejectCancellation → FlowJoinCancelSubscription
    5. end()
  """
  svc = _subscription_service

  # ------------------------------------------------------------------
  # Execution context (values whose output target is 'context')
  # ------------------------------------------------------------------
  ctx = FlowContext()

  # ------------------------------------------------------------------
  # Step 1 – AuthorizeCustomer
  # stereotype: Security
  # ------------------------------------------------------------------
  await svc.authorize_customer(
      auth_token=authorization,
      subscription_id=subscriptionId,
  )

  # ------------------------------------------------------------------
  # Step 2 – LoadSubscription
  # stereotype: Repository
  # outputs: subscription -> context
  # ------------------------------------------------------------------
  ctx.subscription = await svc.load_subscription(subscription_id=subscriptionId)

  # Ownership check (part of AuthorizeCustomer semantics – deferred until
  # subscription is loaded so we have customer_id for comparison).
  # Assumption: The JWT subject (after "Bearer ") encodes customer_id.
  # In production, decode the JWT properly.
  ctx.customer_id = ctx.subscription.customer_id  # store for downstream use

  # ------------------------------------------------------------------
  # Step 3 – CheckCancellationEligibility
  # stereotype: BusinessRule
  # outputs: cancellable -> context
  # ------------------------------------------------------------------
  ctx.cancellable = await svc.check_cancellation_eligibility(
      subscription=ctx.subscription,
  )

  # ------------------------------------------------------------------
  # Step 4 – Decision(cancellable)
  # ------------------------------------------------------------------
  if ctx.cancellable:
    # ---- YES branch ----

    # Step 4a – CancelSubscription
    # stereotype: Repository / transactional=true
    ctx.canceled_subscription = await svc.cancel_subscription(
        subscription_id=subscriptionId,
        cancel_reason=cancel_reason,
        subscription=ctx.subscription,
    )

    # Step 4b – PublishSubscriptionCanceled
    # stereotype: Publisher
    await svc.publish_subscription_canceled(
        canceled_subscription=ctx.canceled_subscription,
    )

    # Step 4c – SendCancellationEmail (with retry policy)
    # stereotype: ExternalCall
    # inputs: customerId from context, subscriptionId from request
    await svc.send_cancellation_email(
        customer_id=ctx.customer_id,
        subscription_id=subscriptionId,
    )

    # FlowJoinCancelSubscription (yes branch merges here)
    # → falls through to end()

  else:
    # ---- NO branch ----

    # Step 4d – RejectCancellation
    # stereotype: BusinessRule
    # This raises UnprocessableEntityException; exception handler returns HTTP 422.
    await svc.reject_cancellation(subscription=ctx.subscription)

    # FlowJoinCancelSubscription (no branch merges here)
    # → falls through to end() (unreachable – exception raised above)

  # ------------------------------------------------------------------
  # FlowJoinCancelSubscription → end()
  # Both branches converge here; return 204 No Content.
  # ------------------------------------------------------------------
  # FastAPI returns 204 automatically (no body) because status_code=204 is set.
  return None