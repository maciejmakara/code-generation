from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Path, Response, status
from pydantic import BaseModel


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# Exceptions derived from @meta.exceptions
# =============================================================================

class ApplicationError(Exception):
  """Base application exception."""


class Unauthorized(ApplicationError):
  """Raised when requester is not authenticated."""


class Forbidden(ApplicationError):
  """Raised when requester is authenticated but not allowed to access resource."""


class NotFound(ApplicationError):
  """Raised when requested resource does not exist."""


class UnprocessableEntity(ApplicationError):
  """Raised when business rules make the request invalid for processing."""


class EventBusUnavailable(ApplicationError):
  """Raised when event publishing fails."""


class EmailProviderError(ApplicationError):
  """Raised when email provider interaction fails."""


class PreConditionViolation(ApplicationError):
  """Raised when an action precondition fails."""


class PostConditionViolation(ApplicationError):
  """Raised when an action postcondition fails."""


# =============================================================================
# Domain models / DTOs
# =============================================================================

class SubscriptionStatus(str, Enum):
  ACTIVE = "ACTIVE"
  CANCELED = "CANCELED"
  PAST_DUE = "PAST_DUE"
  PENDING = "PENDING"


@dataclass
class Subscription:
  id: uuid.UUID
  customer_id: uuid.UUID
  status: SubscriptionStatus
  has_outstanding_invoice: bool
  current_billing_cycle_locked: bool
  cancel_reason: Optional[str] = None
  canceled_at: Optional[datetime] = None


class ErrorResponse(BaseModel):
  error: str
  message: str


class CancelSubscriptionRequestBody(BaseModel):
  cancelReason: Optional[str] = None


@dataclass
class RequestData:
  auth_token: str
  subscription_id: uuid.UUID
  cancel_reason: Optional[str]


@dataclass
class CancellationContext:
  customer_id: Optional[uuid.UUID] = None
  subscription: Optional[Subscription] = None
  cancellable: Optional[bool] = None
  canceled_subscription: Optional[Subscription] = None
  email_sent: Optional[bool] = None
  response: Optional[ErrorResponse] = None
  event_published: Optional[bool] = None


# =============================================================================
# Infrastructure helpers
# =============================================================================

class RetryExhaustedError(Exception):
  """Internal helper exception for retry orchestration."""


@dataclass
class RetryPolicy:
  max_attempts: int
  backoff_ms: int
  retry_on: List[str]
  on_retry_exhausted: str


class RetryHelper:
  """
  Implements retry logic exactly as requested by @meta.retryPolicy.
  """

  @staticmethod
  async def execute_async(
      operation: Callable[[], Any],
      retry_policy: RetryPolicy,
      classify_error: Callable[[Exception], str],
  ) -> Any:
    attempt = 0
    last_error: Optional[Exception] = None

    while attempt < retry_policy.max_attempts:
      try:
        return await operation()
      except Exception as exc:
        last_error = exc
        label = classify_error(exc)
        attempt += 1

        if label not in retry_policy.retry_on or attempt >= retry_policy.max_attempts:
          break

        backoff_seconds = retry_policy.backoff_ms / 1000.0
        logger.warning(
            "Retryable failure detected. attempt=%s/%s label=%s error=%s",
            attempt,
            retry_policy.max_attempts,
            label,
            repr(exc),
        )
        await asyncio.sleep(backoff_seconds)

    if retry_policy.on_retry_exhausted == "log_and_continue":
      logger.exception(
          "Retry exhausted; configured to log_and_continue. last_error=%s",
          repr(last_error),
      )
      return None

    raise RetryExhaustedError from last_error


class AuditLogger:
  @staticmethod
  def record(action_name: str, detail: Dict[str, Any]) -> None:
    logger.info("AUDIT action=%s detail=%s", action_name, detail)


class TransactionManager:
  """
  Best-effort explicit transaction boundary for single-file demo.
  """

  @contextmanager
  def transaction(self, name: str):
    logger.info("BEGIN TRANSACTION: %s", name)
    try:
      yield
      logger.info("COMMIT TRANSACTION: %s", name)
    except Exception:
      logger.info("ROLLBACK TRANSACTION: %s", name)
      raise


# =============================================================================
# In-memory repository/demo integrations
# =============================================================================

class InMemorySubscriptionRepository:
  def __init__(self) -> None:
    sample_customer_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    sample_subscription_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    
    # Additional test subscriptions for integration tests
    sub_002_id = uuid.UUID("33333333-3333-3333-3333-333333333333")  # T1b - ACTIVE
    sub_003_id = uuid.UUID("44444444-4444-4444-4444-444444444444")  # T1c - TRIAL
    sub_010_id = uuid.UUID("55555555-5555-5555-5555-555555555555")  # T2a - CANCELED
    sub_011_id = uuid.UUID("66666666-6666-6666-6666-666666666666")  # T2b - PAST_DUE
    sub_012_id = uuid.UUID("77777777-7777-7777-7777-777777777777")  # T2c - PENDING with locked billing
    
    self._data: Dict[uuid.UUID, Subscription] = {
      # Original sample subscription (T1a)
      sample_subscription_id: Subscription(
          id=sample_subscription_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.ACTIVE,
          has_outstanding_invoice=False,
          current_billing_cycle_locked=False,
      ),
      # Additional test subscriptions
      sub_002_id: Subscription(
          id=sub_002_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.ACTIVE,
          has_outstanding_invoice=False,
          current_billing_cycle_locked=False,
      ),
      sub_003_id: Subscription(
          id=sub_003_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.ACTIVE,
          has_outstanding_invoice=False,
          current_billing_cycle_locked=False,
      ),
      sub_010_id: Subscription(
          id=sub_010_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.CANCELED,
          has_outstanding_invoice=False,
          current_billing_cycle_locked=False,
          cancel_reason="Already canceled",
          canceled_at=datetime.now(timezone.utc),
      ),
      sub_011_id: Subscription(
          id=sub_011_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.PAST_DUE,
          has_outstanding_invoice=True,
          current_billing_cycle_locked=False,
      ),
      sub_012_id: Subscription(
          id=sub_012_id,
          customer_id=sample_customer_id,
          status=SubscriptionStatus.ACTIVE,
          has_outstanding_invoice=False,
          current_billing_cycle_locked=True,
      ),
    }

  def get_by_id(self, subscription_id: uuid.UUID) -> Subscription:
    subscription = self._data.get(subscription_id)
    if subscription is None:
      raise NotFound("Subscription not found.")
    return dataclasses.replace(subscription)

  def save(self, subscription: Subscription) -> Subscription:
    self._data[subscription.id] = dataclasses.replace(subscription)
    return dataclasses.replace(subscription)


class EventBus:
  def publish_subscription_canceled(self, subscription: Subscription) -> bool:
    # Assumption: in this self-contained example the bus is available unless an
    # implementation-specific failure is raised manually.
    logger.info("Published SubscriptionCanceled event for subscription_id=%s", subscription.id)
    return True


class EmailProvider:
  async def send_cancellation_email(self, customer_id: uuid.UUID, subscription_id: uuid.UUID) -> bool:
    # Assumption: this demo implementation succeeds by default.
    # The retry mechanism is still fully implemented and would apply if this
    # method raised retry-eligible failures.
    await asyncio.sleep(0)
    logger.info(
        "Sent cancellation email to customer_id=%s for subscription_id=%s",
        customer_id,
        subscription_id,
    )
    return True


# =============================================================================
# Services
# =============================================================================

class SecurityService:
  def __init__(self, repository: InMemorySubscriptionRepository) -> None:
    self.repository = repository

  def AuthorizeCustomer(self, request_data: RequestData, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "Security",
      "desc": "Verify requester is authenticated and owns the subscription.",
      "inputs": [
        {"name": "authToken", "type": "JWT", "source": "request"},
        {"name": "subscriptionId", "type": "UUID", "source": "request"}
      ],
      "outputs": [
        {"name": "customerId", "type": "UUID", "target": "context"}
      ],
      "exceptions": ["Unauthorized", "Forbidden"],
      "securityContext": "Role_Customer"
    }
    """
    if not request_data.auth_token or not request_data.auth_token.strip():
      raise Unauthorized("Missing or empty auth token.")

    # Assumption: JWT parsing is simplified for this single-file demo.
    # Expected format: "Bearer customer:<uuid>" or "customer:<uuid>".
    token = request_data.auth_token.strip()
    if token.startswith("Bearer "):
      token = token[len("Bearer "):].strip()

    if not token.startswith("customer:"):
      raise Unauthorized("Invalid authentication token format.")

    try:
      token_customer_id = uuid.UUID(token.split("customer:", 1)[1])
    except (ValueError, IndexError) as exc:
      raise Unauthorized("Invalid customer identifier in token.") from exc

    subscription = self.repository.get_by_id(request_data.subscription_id)
    if subscription.customer_id != token_customer_id:
      raise Forbidden("Requester does not own the subscription.")

    context.customer_id = token_customer_id


class SubscriptionRepositoryService:
  def __init__(self, repository: InMemorySubscriptionRepository, tx_manager: TransactionManager) -> None:
    self.repository = repository
    self.tx_manager = tx_manager

  def LoadSubscription(self, request_data: RequestData, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "Repository",
      "desc": "Load subscription from database.",
      "inputs": [{"name": "subscriptionId", "type": "UUID", "source": "request"}],
      "outputs": [{"name": "subscription", "type": "Subscription", "target": "context"}],
      "exceptions": ["NotFound"],
      "sideEffects": false,
      "idempotent": true,
      "consistencyScope": "none"
    }
    """
    subscription = self.repository.get_by_id(request_data.subscription_id)
    context.subscription = subscription

  def CancelSubscription(self, request_data: RequestData, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "Repository",
      "desc": "Mark subscription as CANCELED and store cancellation timestamp and reason.",
      "inputs": [
        {"name": "subscriptionId", "type": "UUID", "source": "request"},
        {"name": "cancelReason", "type": "String?", "source": "request"},
        {"name": "subscription", "type": "Subscription", "source": "context"}
      ],
      "outputs": [
        {"name": "canceledSubscription", "type": "Subscription", "target": "context"}
      ],
      "postCondition": "subscription.status == CANCELED",
      "sideEffects": true,
      "idempotent": true,
      "consistencyScope": "local-transaction",
      "transactional": true
    }
    """
    if context.subscription is None:
      raise PreConditionViolation("CancelSubscription requires context.subscription.")

    with self.tx_manager.transaction("CancelSubscription"):
      subscription = dataclasses.replace(context.subscription)

      # Idempotent behavior required by @meta.idempotent=true:
      # repeated execution keeps a canceled subscription consistently canceled.
      subscription.status = SubscriptionStatus.CANCELED
      if subscription.canceled_at is None:
        subscription.canceled_at = datetime.now(timezone.utc)
      if request_data.cancel_reason is not None:
        subscription.cancel_reason = request_data.cancel_reason

      saved = self.repository.save(subscription)
      context.canceled_subscription = saved

      if saved.status != SubscriptionStatus.CANCELED:
        raise PostConditionViolation("PostCondition failed: subscription.status == CANCELED")


class CancellationBusinessService:
  def CheckCancellationEligibility(self, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "BusinessRule",
      "desc": "Determine whether cancellation is allowed (status, billing cycle, outstanding invoice).",
      "inputs": [{"name": "subscription", "type": "Subscription", "source": "context"}],
      "outputs": [{"name": "cancellable", "type": "bool", "target": "context"}],
      "exceptions": ["UnprocessableEntity"]
    }
    """
    if context.subscription is None:
      raise PreConditionViolation("CheckCancellationEligibility requires context.subscription.")

    subscription = context.subscription

    cancellable = (
        subscription.status != SubscriptionStatus.CANCELED
        and not subscription.current_billing_cycle_locked
        and not subscription.has_outstanding_invoice
    )
    context.cancellable = cancellable

  def RejectCancellation(self, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "BusinessRule",
      "desc": "Return 422 when subscription cannot be canceled.",
      "inputs": [{"name": "subscription", "type": "Subscription", "source": "context"}],
      "outputs": [{"name": "response", "type": "ErrorResponse", "target": "response"}],
      "exceptions": ["UnprocessableEntity"]
    }
    """
    if context.subscription is None:
      raise PreConditionViolation("RejectCancellation requires context.subscription.")

    context.response = ErrorResponse(
        error="unprocessable_entity",
        message=(
          "Subscription cannot be canceled due to its current status, "
          "billing cycle state, or outstanding invoice."
        ),
    )
    raise UnprocessableEntity(context.response.message)


class PublisherService:
  def __init__(self, event_bus: EventBus) -> None:
    self.event_bus = event_bus

  def PublishSubscriptionCanceled(self, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "Publisher",
      "desc": "Publish SubscriptionCanceled domain event.",
      "inputs": [{"name": "canceledSubscription", "type": "Subscription", "source": "context"}],
      "outputs": [{"name": "eventPublished", "type": "bool", "target": "event"}],
      "postCondition": "event_published",
      "sideEffects": true,
      "exceptions": ["EventBusUnavailable"],
      "idempotent": true
    }
    """
    if context.canceled_subscription is None:
      raise PreConditionViolation(
          "PublishSubscriptionCanceled requires context.canceled_subscription."
      )

    try:
      event_published = self.event_bus.publish_subscription_canceled(context.canceled_subscription)
    except Exception as exc:
      raise EventBusUnavailable("Failed to publish SubscriptionCanceled event.") from exc

    context.event_published = event_published

    if not event_published:
      raise PostConditionViolation("PostCondition failed: event_published")


class NotificationService:
  def __init__(self, email_provider: EmailProvider) -> None:
    self.email_provider = email_provider
    self.retry_policy = RetryPolicy(
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="log_and_continue",
    )

  @staticmethod
  def _classify_error(exc: Exception) -> str:
    message = str(exc)
    if "Timeout" in message:
      return "Timeout"
    if "502" in message:
      return "502"
    if "503" in message:
      return "503"
    return exc.__class__.__name__

  async def SendCancellationEmail(self, request_data: RequestData, context: CancellationContext) -> None:
    """
    @meta
    {
      "stereotype": "ExternalCall",
      "desc": "Send confirmation email to customer. Operation is best-effort – failure does not roll back cancellation.",
      "inputs": [
        {"name": "customerId", "type": "UUID", "source": "context"},
        {"name": "subscriptionId", "type": "UUID", "source": "request"}
      ],
      "outputs": [{"name": "emailSent", "type": "bool", "target": "context"}],
      "exceptions": ["EmailProviderError"],
      "retryPolicy": {
        "maxAttempts": 3,
        "backoffMs": 300,
        "retryOn": ["Timeout", "502", "503"],
        "onRetryExhausted": "log_and_continue"
      }
    }
    """
    if context.customer_id is None:
      raise PreConditionViolation("SendCancellationEmail requires context.customer_id.")

    async def operation() -> bool:
      return await self.email_provider.send_cancellation_email(
          customer_id=context.customer_id,
          subscription_id=request_data.subscription_id,
      )

    try:
      result = await RetryHelper.execute_async(
          operation=operation,
          retry_policy=self.retry_policy,
          classify_error=self._classify_error,
      )
    except Exception as exc:
      # Only non-retry-exhausted failures propagate. Retry exhausted with
      # log_and_continue returns None per spec.
      raise EmailProviderError("Email sending failed.") from exc

    context.email_sent = bool(result) if result is not None else False


# =============================================================================
# FlowJoin helper
# =============================================================================

def FlowJoinCancelSubscription() -> None:
  """
  Explicit merge node corresponding to both Decision(cancellable) branches.
  Per specification:
    function FlowJoinCancelSubscription():
      end()
  """
  return


# =============================================================================
# Controller orchestrator
# =============================================================================

class CancelSubscriptionController:
  def __init__(
      self,
      security_service: SecurityService,
      repository_service: SubscriptionRepositoryService,
      business_service: CancellationBusinessService,
      publisher_service: PublisherService,
      notification_service: NotificationService,
  ) -> None:
    self.security_service = security_service
    self.repository_service = repository_service
    self.business_service = business_service
    self.publisher_service = publisher_service
    self.notification_service = notification_service

  async def main_Cancelsubscription(self, request_data: RequestData) -> Response:
    """
    REST DEFINITION
    @meta {
      "endpoint": "DELETE /subscriptions/{subscriptionId}",
      "responseSuccess": "204 No Content",
      "responseError": "400|401|403|404|409|422"
    }
    """
    context = CancellationContext()

    # Step 1: AuthorizeCustomer()
    self.security_service.AuthorizeCustomer(request_data, context)

    # Step 2: LoadSubscription()
    self.repository_service.LoadSubscription(request_data, context)

    # Step 3: CheckCancellationEligibility()
    self.business_service.CheckCancellationEligibility(context)

    # Step 4: Decision(cancellable)
    if context.cancellable is True:
      # if yes:
      # Step 4.1: CancelSubscription()
      self.repository_service.CancelSubscription(request_data, context)

      # Step 4.2: PublishSubscriptionCanceled()
      self.publisher_service.PublishSubscriptionCanceled(context)

      # Step 4.3: SendCancellationEmail()
      # Best-effort behavior is implemented inside the service according to retryPolicy.
      await self.notification_service.SendCancellationEmail(request_data, context)

      # Step 4.4: FlowJoinCancelSubscription()
      FlowJoinCancelSubscription()

    else:
      # if no:
      # Step 4.5: RejectCancellation()
      self.business_service.RejectCancellation(context)

      # Step 4.6: FlowJoinCancelSubscription()
      # This line is unreachable because RejectCancellation() raises
      # UnprocessableEntity by specification, but the merge node is kept explicit.
      FlowJoinCancelSubscription()

    # Step 5: end()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =============================================================================
# Exception mapping
# =============================================================================

def to_http_exception(exc: Exception) -> HTTPException:
  if isinstance(exc, Unauthorized):
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthorized", "message": str(exc)},
    )
  if isinstance(exc, Forbidden):
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "forbidden", "message": str(exc)},
    )
  if isinstance(exc, NotFound):
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": str(exc)},
    )
  if isinstance(exc, UnprocessableEntity):
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"error": "unprocessable_entity", "message": str(exc)},
    )
  if isinstance(exc, EventBusUnavailable):
    # Assumption: responseError does not explicitly include 503, but the
    # specification includes EventBusUnavailable. Best reasonable mapping is 409
    # for operation completion conflict in this constrained response set.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "event_bus_unavailable", "message": str(exc)},
    )
  if isinstance(exc, EmailProviderError):
    # Per spec, email failure is best-effort and should not roll back cancellation.
    # This mapping is used only for non-best-effort failures outside retry handling.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "email_provider_error", "message": str(exc)},
    )
  if isinstance(exc, (PreConditionViolation, PostConditionViolation, ValueError)):
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "bad_request", "message": str(exc)},
    )

  logger.exception("Unhandled application error", exc_info=exc)
  return HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail={"error": "internal_server_error", "message": "Unexpected error."},
  )


# =============================================================================
# Wiring
# =============================================================================

repository = InMemorySubscriptionRepository()
transaction_manager = TransactionManager()
event_bus = EventBus()
email_provider = EmailProvider()

security_service = SecurityService(repository)
repository_service = SubscriptionRepositoryService(repository, transaction_manager)
business_service = CancellationBusinessService()
publisher_service = PublisherService(event_bus)
notification_service = NotificationService(email_provider)

controller = CancelSubscriptionController(
    security_service=security_service,
    repository_service=repository_service,
    business_service=business_service,
    publisher_service=publisher_service,
    notification_service=notification_service,
)

app = FastAPI(title="Subscription Cancellation API")


# =============================================================================
# REST endpoint
# =============================================================================

@app.delete(
    "/subscriptions/{subscriptionId}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
      400: {"model": ErrorResponse, "description": "Bad Request"},
      401: {"model": ErrorResponse, "description": "Unauthorized"},
      403: {"model": ErrorResponse, "description": "Forbidden"},
      404: {"model": ErrorResponse, "description": "Not Found"},
      409: {"model": ErrorResponse, "description": "Conflict"},
      422: {"model": ErrorResponse, "description": "Unprocessable Entity"},
    },
)
async def delete_subscription(
    subscriptionId: uuid.UUID = Path(..., description="Subscription identifier"),
    body: CancelSubscriptionRequestBody = None,
    authorization: str = Header(..., alias="Authorization"),
) -> Response:
  """
  Controller method mapped from:
    DELETE /subscriptions/{subscriptionId}

  Parses request/path/body/header values required by @meta.inputs(source=request):
    - authToken <- Authorization header
    - subscriptionId <- path
    - cancelReason <- request body
  """
  try:
    request_data = RequestData(
        auth_token=authorization,
        subscription_id=subscriptionId,
        cancel_reason=body.cancelReason if body is not None else None,
    )
    return await controller.main_Cancelsubscription(request_data)
  except Exception as exc:
    raise to_http_exception(exc) from exc


if __name__ == "__main__":
  import uvicorn
  print("Starting FastAPI server on http://localhost:8000")
  uvicorn.run(app, host="0.0.0.0", port=8004)
