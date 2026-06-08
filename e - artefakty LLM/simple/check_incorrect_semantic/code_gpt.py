from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field


# =========================
# Exceptions (@meta.exceptions)
# =========================

class DomainException(Exception):
  pass


class Unauthorized(DomainException):
  pass


class Forbidden(DomainException):
  pass


class NotFound(DomainException):
  pass


class UnprocessableEntity(DomainException):
  pass


class EventBusUnavailable(DomainException):
  pass


class EmailProviderError(DomainException):
  pass


class TimeoutException(DomainException):
  pass


class PostConditionFailed(DomainException):
  pass


# =========================
# DTOs / Models
# =========================

class Subscription(BaseModel):
  id: UUID
  customer_id: UUID
  status: str
  outstanding_invoice: bool = False
  billing_cycle_locked: bool = False
  canceled_at: Optional[datetime] = None
  cancel_reason: Optional[str] = None


class ErrorResponse(BaseModel):
  code: str
  message: str
  details: Optional[Dict[str, Any]] = None


@dataclass
class CancelSubscriptionContext:
  subscription: Optional[Subscription] = None
  cancellable: Optional[bool] = None
  canceledSubscription: Optional[Subscription] = None
  customerId: Optional[UUID] = None
  eventPublished: Optional[bool] = None
  response: Optional[ErrorResponse] = None


# =========================
# Infrastructure helpers
# =========================

@contextmanager
def transaction_boundary(name: str):
  # Best-effort transaction boundary for single-file demo.
  try:
    yield
  except Exception:
    raise


class RetryExecutor:
  async def run_with_retry(
      self,
      fn,
      *,
      max_attempts: int,
      backoff_ms: int,
      retry_on: list[str],
  ):
    attempt = 0
    while True:
      attempt += 1
      try:
        return await fn()
      except Exception as exc:  # noqa: BLE001
        label = self._label(exc)
        if attempt < max_attempts and label in retry_on:
          await asyncio.sleep(backoff_ms / 1000.0)
          continue
        raise

  @staticmethod
  def _label(exc: Exception) -> str:
    if isinstance(exc, TimeoutException):
      return "Timeout"
    if isinstance(exc, EmailProviderError):
      msg = str(exc)
      if msg in {"502", "503"}:
        return msg
    return exc.__class__.__name__


# =========================
# Repository / external adapters
# =========================

class SubscriptionRepository:
  def __init__(self):
    customer_a = uuid4()
    sub_id = uuid4()
    self._subscriptions: Dict[UUID, Subscription] = {
      sub_id: Subscription(
          id=sub_id,
          customer_id=customer_a,
          status="ACTIVE",
          outstanding_invoice=False,
          billing_cycle_locked=False,
      )
    }

  def load(self, subscription_id: UUID) -> Subscription:
    sub = self._subscriptions.get(subscription_id)
    if sub is None:
      raise NotFound("Subscription not found.")
    return sub

  def cancel(self, subscription_id: UUID, cancel_reason: Optional[str], existing: Subscription) -> Subscription:
    # Idempotent write: if already CANCELED, keep as-is and return.
    if existing.status == "CANCELED":
      return existing

    updated = existing.model_copy(deep=True)
    updated.status = "CANCELED"
    updated.canceled_at = datetime.now(timezone.utc)
    updated.cancel_reason = cancel_reason
    self._subscriptions[subscription_id] = updated
    return updated


class EventPublisher:
  def __init__(self):
    self.published_events: list[dict[str, Any]] = []

  def publish_subscription_canceled(self, sub: Subscription) -> bool:
    # In real code this could fail with EventBusUnavailable.
    self.published_events.append(
        {
          "type": "SubscriptionCanceled",
          "subscriptionId": str(sub.id),
          "customerId": str(sub.customer_id),
          "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return True


class EmailGateway:
  async def send_cancellation_email(self, customer_id: UUID, subscription_id: UUID) -> None:
    # In real code integrate provider and map provider failures to EmailProviderError/TimeoutException.
    await asyncio.sleep(0.01)


# =========================
# Services (one method per action)
# =========================

class SecurityService:
  def AuthorizeCustomer(self, authToken: Optional[str], subscriptionId: UUID) -> UUID:
    # Assumption: auth token format is "Bearer <customer_uuid>".
    if not authToken or not authToken.startswith("Bearer "):
      raise Unauthorized("Missing or invalid auth token.")
    token_value = authToken.removeprefix("Bearer ").strip()
    try:
      requester_customer_id = UUID(token_value)
    except ValueError as exc:
      raise Unauthorized("Invalid auth token.") from exc
    return requester_customer_id


class SubscriptionQueryService:
  def __init__(self, repo: SubscriptionRepository):
    self.repo = repo

  def LoadSubscription(self, subscriptionId: UUID) -> Subscription:
    return self.repo.load(subscriptionId)


class CancellationRuleService:
  def CheckCancellationEligibility(self, subscription: Subscription) -> bool:
    # desc: status, billing cycle, outstanding invoice.
    if subscription.status == "CANCELED":
      return True  # idempotent cancellation allowed
    if subscription.billing_cycle_locked:
      return False
    if subscription.outstanding_invoice:
      return False
    if subscription.status not in {"ACTIVE", "TRIAL"}:
      return False
    return True

  def RejectCancellation(self, subscription: Subscription) -> ErrorResponse:
    # Return 422 response payload.
    return ErrorResponse(
        code="UNPROCESSABLE_ENTITY",
        message="Subscription cannot be canceled.",
        details={
          "subscriptionId": str(subscription.id),
          "status": subscription.status,
          "billingCycleLocked": subscription.billing_cycle_locked,
          "outstandingInvoice": subscription.outstanding_invoice,
        },
    )


class CancellationCommandService:
  def __init__(self, repo: SubscriptionRepository):
    self.repo = repo

  def CancelSubscription(
      self,
      subscriptionId: UUID,
      cancelReason: Optional[str],
      subscription: Subscription,
  ) -> Subscription:
    with transaction_boundary("CancelSubscription"):
      canceled = self.repo.cancel(subscriptionId, cancelReason, subscription)
    # postCondition: subscription.status == CANCELED
    if canceled.status != "CANCELED":
      raise PostConditionFailed("postCondition failed: subscription.status == CANCELED")
    return canceled


class SubscriptionEventService:
  def __init__(self, publisher: EventPublisher):
    self.publisher = publisher

  def PublishSubscriptionCanceled(self, canceledSubscription: Subscription) -> bool:
    published = self.publisher.publish_subscription_canceled(canceledSubscription)
    # postCondition: event_published
    if not published:
      raise PostConditionFailed("postCondition failed: event_published")
    return published


class NotificationService:
  def __init__(self, gateway: EmailGateway, retry_executor: RetryExecutor):
    self.gateway = gateway
    self.retry_executor = retry_executor

  async def SendCancellationEmail(self, customerId: UUID, subscriptionId: UUID) -> None:
    await self.retry_executor.run_with_retry(
        lambda: self.gateway.send_cancellation_email(customerId, subscriptionId),
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
    )


# =========================
# FlowJoin
# =========================

def FlowJoinCancelSubscription() -> None:
  # function FlowJoinCancelSubscription(): end()
  return


# =========================
# Controller
# REST @meta:
# endpoint: DELETE /subscriptions/{subscriptionId}
# responseSuccess: 204 No Content
# responseError: 400|401|403|404|409|422
# =========================

app = FastAPI(title="Subscription API", version="1.0.0")

repo = SubscriptionRepository()
security_service = SecurityService()
query_service = SubscriptionQueryService(repo)
rule_service = CancellationRuleService()
command_service = CancellationCommandService(repo)
event_service = SubscriptionEventService(EventPublisher())
notification_service = NotificationService(EmailGateway(), RetryExecutor())


@app.delete("/subscriptions/{subscriptionId}", status_code=status.HTTP_204_NO_CONTENT)
async def main_Cancelsubscription(
    subscriptionId: UUID = Path(...),
    cancelReason: Optional[str] = Query(default=None),  # Assumption: optional reason comes from query.
    authToken: Optional[str] = Header(default=None, alias="Authorization"),
):
  ctx = CancelSubscriptionContext()

  try:
    # 1) AuthorizeCustomer()
    requester_customer_id = security_service.AuthorizeCustomer(authToken, subscriptionId)

    # 2) LoadSubscription()
    ctx.subscription = query_service.LoadSubscription(subscriptionId)
    ctx.customerId = ctx.subscription.customer_id

    # Ownership check stays in security action semantics.
    if requester_customer_id != ctx.subscription.customer_id:
      raise Forbidden("Requester does not own the subscription.")

    # 3) CheckCancellationEligibility()
    ctx.cancellable = rule_service.CheckCancellationEligibility(ctx.subscription)

    # 4) Decision(cancellable)
    if ctx.cancellable is True:
      # if yes:
      # 4.1) CancelSubscription()
      ctx.canceledSubscription = command_service.CancelSubscription(
          subscriptionId=subscriptionId,
          cancelReason=cancelReason,
          subscription=ctx.subscription,
      )

      # 4.2) PublishSubscriptionCanceled()
      ctx.eventPublished = event_service.PublishSubscriptionCanceled(ctx.canceledSubscription)

      # 4.3) SendCancellationEmail()
      await notification_service.SendCancellationEmail(
          customerId=ctx.customerId,
          subscriptionId=subscriptionId,
      )

      # 4.4) FlowJoinCancelSubscription()
      FlowJoinCancelSubscription()
      return Response(status_code=status.HTTP_204_NO_CONTENT)

    else:
      # if no:
      # 4.5) RejectCancellation()
      ctx.response = rule_service.RejectCancellation(ctx.subscription)

      # 4.6) FlowJoinCancelSubscription()
      FlowJoinCancelSubscription()
      return Response(
          status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
          content=ctx.response.model_dump_json(),
          media_type="application/json",
      )

  except Unauthorized as exc:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
  except Forbidden as exc:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
  except NotFound as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
  except UnprocessableEntity as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
  except EventBusUnavailable as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
  except EmailProviderError as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
  except PostConditionFailed as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
  except ValueError as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
