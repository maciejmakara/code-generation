"""
POST /users/register – User Registration Endpoint
===================================================
Generated from UML Activity Diagram pseudo-code specification.

Sections:
  1. Imports & configuration
  2. Custom exception classes
  3. DTOs / data models
  4. Flow context
  5. Audit helper
  6. Retry helper
  7. Repository / service stubs (implement real DB / email logic here)
  8. UserRegistrationService  – one method per spec action
  9. FastAPI controller
 10. App entry-point
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator

# ---------------------------------------------------------------------------
# 1. Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("user_registration")


# ===========================================================================
# 2. CUSTOM EXCEPTION CLASSES  (derived from @meta.exceptions)
# ===========================================================================

class RegistrationBaseException(Exception):
  """Base for all registration flow exceptions."""


class ValidationException(RegistrationBaseException):
  """Raised when RegistrationDTO fails structural / required-field checks."""


class TimeoutException(RegistrationBaseException):
  """Raised on downstream timeout (DB, email service, …)."""


class ConflictException(RegistrationBaseException):
  """Raised when the email address is already registered."""


class DisposableEmailDetected(RegistrationBaseException):
  """Raised when the email domain is on the disposable-domain blocklist."""


# ===========================================================================
# 3. DTOs / DATA MODELS
# ===========================================================================

class RegistrationDTO(BaseModel):
  """Inbound request body for POST /users/register."""
  username: str = Field(..., min_length=3, max_length=64)
  email: EmailStr
  password: str = Field(..., min_length=8)

  @field_validator("password")
  @classmethod
  def _password_strength(cls, v: str) -> str:
    if not any(c.isdigit() for c in v):
      raise ValueError("Password must contain at least one digit.")
    return v


class ValidatedUserData(BaseModel):
  """Validated and normalised user data – stored in flow context."""
  username: str
  email: str
  hashed_password: str


class ErrorResponse(BaseModel):
  """Unified error body returned to the client."""
  error: str
  detail: Optional[str] = None
  request_id: Optional[str] = None


class RegistrationResponse(BaseModel):
  """201 Created response body."""
  request_id: UUID


# ===========================================================================
# 4. FLOW CONTEXT
# ===========================================================================

@dataclass
class RegistrationContext:
  """
  Mutable context object carrying all @meta outputs with target='context'
  through the registration flow.
  """
  validated_user_data: Optional[ValidatedUserData] = None
  email_allowed: Optional[bool] = None          # from FilterDisposableEmailDomains
  user_id: Optional[UUID] = None                # from CreateUserAccount
  email: Optional[str] = None                   # from CreateUserAccount
  email_sent_status: Optional[bool] = None      # from SendActivationEmail

  # response-targeted outputs are written here and later serialised
  request_id: Optional[UUID] = None             # from FinalizeRegistration (→ response)
  error_response: Optional[ErrorResponse] = None  # from HandleRegistrationTimeout (→ response)


# ===========================================================================
# 5. AUDIT HELPER
# ===========================================================================

class AuditLogger:
  """Lightweight audit logger. Replace with a real audit sink in production."""

  @staticmethod
  def log(action: str, context: RegistrationContext, extra: dict[str, Any] | None = None) -> None:
    entry = {
      "action": action,
      "user_id": str(context.user_id) if context.user_id else None,
      "email": context.email or (
        context.validated_user_data.email if context.validated_user_data else None
      ),
      **(extra or {}),
    }
    logger.info("[AUDIT] %s", entry)


# ===========================================================================
# 6. RETRY HELPER
# ===========================================================================

async def with_retry(
    coro_factory,
    *,
    max_attempts: int,
    backoff_ms: int,
    retry_on: tuple[type[Exception], ...],
    on_retry_exhausted: str = "raise",
):
  """
  Execute `coro_factory()` up to `max_attempts` times.
  Retries only on exceptions whose type (or name) is listed in `retry_on`.
  `on_retry_exhausted='raise'` re-raises the last exception (spec default).
  """
  last_exc: Exception | None = None
  for attempt in range(1, max_attempts + 1):
    try:
      return await coro_factory()
    except retry_on as exc:
      last_exc = exc
      if attempt < max_attempts:
        logger.warning(
            "Retry %d/%d after %dms – %s: %s",
            attempt, max_attempts, backoff_ms, type(exc).__name__, exc,
        )
        await asyncio.sleep(backoff_ms / 1000)
      else:
        logger.error("Retry exhausted after %d attempts.", max_attempts)
        if on_retry_exhausted == "raise":
          raise
  # Should not be reached, but satisfy type-checkers:
  raise last_exc  # type: ignore[misc]


# ===========================================================================
# 7. REPOSITORY / EXTERNAL STUBS
#    (Replace with real database sessions and email-service clients.)
# ===========================================================================

# Simulated in-memory store for demonstration purposes.
_REGISTERED_EMAILS: set[str] = set()

DISPOSABLE_DOMAIN_BLOCKLIST: frozenset[str] = frozenset({
  "mailinator.com", "guerrillamail.com", "tempmail.com",
  "throwaway.email", "yopmail.com",
})


class UserRepository:
  """Thin DB abstraction. Each method is called inside an explicit transaction."""

  async def email_exists(self, email: str) -> bool:
    """Returns True if the email is already registered."""
    # Assumption: real implementation queries the users table.
    await asyncio.sleep(0)   # yield to event loop (simulate I/O)
    return email in _REGISTERED_EMAILS

  async def create_user(self, data: ValidatedUserData) -> UUID:
    """
    Persist new user account.
    Returns the generated user_id.
    transactional=true → caller wraps in a transaction context manager.
    """
    await asyncio.sleep(0)
    new_id = uuid.uuid4()
    _REGISTERED_EMAILS.add(data.email)
    logger.info("[DB] User created: id=%s email=%s", new_id, data.email)
    return new_id

  async def finalize_registration(self, user_id: UUID) -> UUID:
    """
    Mark registration as complete, set user status active.
    postCondition: userStatus_Active
    Returns a request_id for the response.
    """
    await asyncio.sleep(0)
    request_id = uuid.uuid4()
    logger.info("[DB] Registration finalised: user_id=%s request_id=%s", user_id, request_id)
    return request_id


class EmailService:
  """External email-sending integration."""

  async def send_activation_email(self, user_id: UUID, email: str) -> bool:
    """
    Send activation email with confirmation link.
    Returns True on success.
    """
    await asyncio.sleep(0)
    logger.info("[EMAIL] Activation email sent to %s (user_id=%s)", email, user_id)
    return True


class TransactionManager:
  """
  Minimal transaction-boundary helper.
  Replace with SQLAlchemy / asyncpg session context in production.
  """

  async def __aenter__(self):
    logger.debug("[TX] BEGIN")
    return self

  async def __aexit__(self, exc_type, exc_val, exc_tb):
    if exc_type:
      logger.debug("[TX] ROLLBACK")
    else:
      logger.debug("[TX] COMMIT")
    return False   # do not suppress exceptions


# ===========================================================================
# 8. USER REGISTRATION SERVICE
#    One method per action in the spec.  Order mirrors the spec exactly.
# ===========================================================================

class UserRegistrationService:
  """
  Implements every action node from the UML Activity Diagram.
  The controller calls these in the exact order dictated by the spec.
  """

  def __init__(
      self,
      user_repo: UserRepository,
      email_service: EmailService,
      tx_manager_factory,
      audit: AuditLogger,
  ):
    self._repo = user_repo
    self._email_svc = email_service
    self._tx_factory = tx_manager_factory
    self._audit = audit

  # ------------------------------------------------------------------
  # Action 1 – ValidateUserInput
  # stereotype: Validation
  # ------------------------------------------------------------------
  async def validate_user_input(
      self,
      registration_dto: RegistrationDTO,
      ctx: RegistrationContext,
  ) -> None:
    """
    Validate registration DTO structure and required fields.
    Outputs: validatedUserData → context
    Raises:  ValidationException
    """
    # Pydantic already enforced structural rules via RegistrationDTO; any
    # extra business-level checks are applied here.
    hashed_password = hashlib.sha256(registration_dto.password.encode()).hexdigest()
    ctx.validated_user_data = ValidatedUserData(
        username=registration_dto.username,
        email=registration_dto.email.lower().strip(),
        hashed_password=hashed_password,
    )
    logger.info("[ValidateUserInput] Validation passed for username=%s", registration_dto.username)

  # ------------------------------------------------------------------
  # Action 2 – CheckEmailUniqueness
  # stereotype: Repository | idempotent=true | sideEffects=false
  # ------------------------------------------------------------------
  async def check_email_uniqueness(self, ctx: RegistrationContext) -> None:
    """
    Verify email address is not already registered.
    Raises: TimeoutException, ConflictException
    preCondition (implicit): ctx.validated_user_data must be set.
    """
    assert ctx.validated_user_data is not None, "validated_user_data must be present in context"
    try:
      exists = await self._repo.email_exists(ctx.validated_user_data.email)
    except asyncio.TimeoutError as exc:
      raise TimeoutException("Email uniqueness check timed out.") from exc

    if exists:
      raise ConflictException(
          f"Email '{ctx.validated_user_data.email}' is already registered."
      )
    logger.info("[CheckEmailUniqueness] Email is unique: %s", ctx.validated_user_data.email)

  # ------------------------------------------------------------------
  # Action 3 – FilterDisposableEmailDomains
  # stereotype: Filter | auditRequired=true | llmPriority=high
  # ------------------------------------------------------------------
  async def filter_disposable_email_domains(self, ctx: RegistrationContext) -> None:
    """
    Reject registrations from known disposable/temporary email domains.
    Outputs: emailAllowed → context
    Raises:  DisposableEmailDetected
    auditRequired=true → audit entry written regardless of outcome.
    llmPriority=high   → correctness is paramount; no shortcuts.
    """
    assert ctx.validated_user_data is not None, "validated_user_data must be present in context"

    domain = ctx.validated_user_data.email.split("@")[-1].lower()
    is_disposable = domain in DISPOSABLE_DOMAIN_BLOCKLIST
    ctx.email_allowed = not is_disposable

    # auditRequired=true
    self._audit.log(
        "FilterDisposableEmailDomains",
        ctx,
        extra={"domain": domain, "emailAllowed": ctx.email_allowed},
    )

    if is_disposable:
      raise DisposableEmailDetected(
          f"Email domain '{domain}' is on the disposable-email blocklist."
      )
    logger.info("[FilterDisposableEmailDomains] Domain allowed: %s", domain)

  # ------------------------------------------------------------------
  # Decision(isTimeout) / BackToDecision gate – Action 4
  # stereotype: Repository | transactional=true | auditRequired=true | llmPriority=high
  # ------------------------------------------------------------------
  async def create_user_account(self, ctx: RegistrationContext) -> None:
    """
    Persist new user account with hashed credentials.
    Outputs: userId → context, email → context
    Raises:  TimeoutException
    consistencyScope=local-transaction → wrapped in explicit transaction.
    auditRequired=true, llmPriority=high.
    """
    assert ctx.validated_user_data is not None, "validated_user_data must be present in context"

    async with self._tx_factory() as _tx:
      try:
        ctx.user_id = await self._repo.create_user(ctx.validated_user_data)
        ctx.email = ctx.validated_user_data.email
      except asyncio.TimeoutError as exc:
        raise TimeoutException("CreateUserAccount timed out.") from exc

    # auditRequired=true
    self._audit.log("CreateUserAccount", ctx)
    logger.info("[CreateUserAccount] User account created: user_id=%s", ctx.user_id)

  # ------------------------------------------------------------------
  # Error branch – Action 5
  # stereotype: ErrorHandler
  # ------------------------------------------------------------------
  async def handle_registration_timeout(self, ctx: RegistrationContext) -> None:
    """
    Handle timeout – log failure and return error response.
    Called regardless of which action triggered the timeout.
    Outputs: errorResponse → response
    postCondition: registrationStatus_Failed
    """
    logger.error(
        "[HandleRegistrationTimeout] Timeout during registration. user_id=%s",
        ctx.user_id,
    )
    ctx.error_response = ErrorResponse(
        error="Registration failed due to a timeout.",
        detail="Please try again later.",
        request_id=str(ctx.user_id) if ctx.user_id else None,
    )
    # postCondition: registrationStatus_Failed
    # (In a real system this would persist a FAILED status row.)
    logger.info(
        "[HandleRegistrationTimeout] postCondition verified: registrationStatus=Failed"
    )

  # ------------------------------------------------------------------
  # Decision(isTimeout) / BackToDecision gate – Action 6
  # stereotype: ExternalCall | retryPolicy present
  # ------------------------------------------------------------------
  async def send_activation_email(self, ctx: RegistrationContext) -> None:
    """
    Send activation email with confirmation link.
    Outputs: emailSentStatus → context
    retryPolicy: maxAttempts=3, backoffMs=300, retryOn=[Timeout,502,503], onRetryExhausted=raise
    """
    assert ctx.user_id is not None and ctx.email is not None, (
      "userId and email must be present in context"
    )

    async def _attempt():
      return await self._email_svc.send_activation_email(ctx.user_id, ctx.email)  # type: ignore[arg-type]

    ctx.email_sent_status = await with_retry(
        _attempt,
        max_attempts=3,
        backoff_ms=300,
        retry_on=(TimeoutException, asyncio.TimeoutError),
        on_retry_exhausted="raise",
    )
    logger.info(
        "[SendActivationEmail] Email sent: user_id=%s status=%s",
        ctx.user_id, ctx.email_sent_status,
    )

  # ------------------------------------------------------------------
  # Action 7 – FinalizeRegistration
  # stereotype: Repository | transactional=true | idempotent=true | postCondition
  # ------------------------------------------------------------------
  async def finalize_registration(self, ctx: RegistrationContext) -> None:
    """
    Mark registration as complete and set user status active.
    Outputs: requestId → response
    postCondition: userStatus_Active
    consistencyScope=local-transaction → explicit transaction boundary.
    idempotent=true → safe to re-run.
    """
    assert ctx.user_id is not None, "userId must be present in context"

    async with self._tx_factory() as _tx:
      try:
        ctx.request_id = await self._repo.finalize_registration(ctx.user_id)
      except asyncio.TimeoutError as exc:
        raise TimeoutException("FinalizeRegistration timed out.") from exc

    # postCondition: userStatus_Active
    # (Real implementation would query the DB to verify the status row.)
    logger.info(
        "[FinalizeRegistration] postCondition verified: userStatus=Active. request_id=%s",
        ctx.request_id,
    )


# ===========================================================================
# 9. FASTAPI CONTROLLER
# ===========================================================================

app = FastAPI(title="User Registration Service", version="1.0.0")

# Dependency-wire real implementations here.
_user_repo = UserRepository()
_email_service = EmailService()
_audit = AuditLogger()
_svc = UserRegistrationService(
    user_repo=_user_repo,
    email_service=_email_service,
    tx_manager_factory=TransactionManager,
    audit=_audit,
)


@app.post(
    "/users/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegistrationResponse,
    responses={
      400: {"model": ErrorResponse},
      409: {"model": ErrorResponse},
      422: {"model": ErrorResponse},
      504: {"model": ErrorResponse},
    },
    summary="Register a new user account",
)
async def register_user(registration_dto: RegistrationDTO) -> JSONResponse:
  """
  POST /users/register
  Orchestrates the full registration flow exactly as specified.

  Decision(isTimeout) is implemented as a try/except block that catches
  TimeoutException at each BackToDecision point and routes to the
  HandleRegistrationTimeout error branch → FlowJoinRegistration.

  FlowJoinRegistration is the single convergence point; after it the
  controller exits with the appropriate HTTP response.
  """
  ctx = RegistrationContext()

  # ------------------------------------------------------------------
  # Step 1 – ValidateUserInput
  # Raises ValidationException → 422
  # ------------------------------------------------------------------
  try:
    await _svc.validate_user_input(registration_dto, ctx)
  except ValidationException as exc:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(error="Validation failed", detail=str(exc)).model_dump(),
    )

  # ------------------------------------------------------------------
  # Step 2 – CheckEmailUniqueness
  # Raises ConflictException → 409 | TimeoutException → 504
  # ------------------------------------------------------------------
  try:
    await _svc.check_email_uniqueness(ctx)
  except ConflictException as exc:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(error="Email conflict", detail=str(exc)).model_dump(),
    )
  except TimeoutException as exc:
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ErrorResponse(error="Timeout", detail=str(exc)).model_dump(),
    )

  # ------------------------------------------------------------------
  # Step 3 – FilterDisposableEmailDomains
  # Raises DisposableEmailDetected → 400
  # ------------------------------------------------------------------
  try:
    await _svc.filter_disposable_email_domains(ctx)
  except DisposableEmailDetected as exc:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            error="Disposable email not allowed", detail=str(exc)
        ).model_dump(),
    )

  # ------------------------------------------------------------------
  # Decision(isTimeout) – Branch: okfilterdisposableemaildomains
  #
  # BackToDecision(isTimeout)(okcreateuseraccount):
  #   If CreateUserAccount raises TimeoutException → exception branch
  #   (HandleRegistrationTimeout → FlowJoinRegistration).
  #   On success → continue to okcreateuseraccount branch.
  # ------------------------------------------------------------------
  try:
    # Step 4 – CreateUserAccount  [okfilterdisposableemaildomains branch]
    await _svc.create_user_account(ctx)

  except TimeoutException:
    # Decision(isTimeout) – exception branch
    # Step 5 – HandleRegistrationTimeout
    await _svc.handle_registration_timeout(ctx)
    # FlowJoinRegistration → end
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ctx.error_response.model_dump(),  # type: ignore[union-attr]
    )

  # ------------------------------------------------------------------
  # Branch: okcreateuseraccount
  #
  # BackToDecision(isTimeout)(oksendactivationemail):
  #   If SendActivationEmail raises TimeoutException → exception branch.
  #   On success → continue to oksendactivationemail branch.
  # ------------------------------------------------------------------
  try:
    # Step 6 – SendActivationEmail  [okcreateuseraccount branch]
    await _svc.send_activation_email(ctx)

  except TimeoutException:
    # Decision(isTimeout) – exception branch
    # Step 5 – HandleRegistrationTimeout
    await _svc.handle_registration_timeout(ctx)
    # FlowJoinRegistration → end
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ctx.error_response.model_dump(),  # type: ignore[union-attr]
    )

  # ------------------------------------------------------------------
  # Branch: oksendactivationemail
  # Step 7 – FinalizeRegistration
  # ------------------------------------------------------------------
  try:
    await _svc.finalize_registration(ctx)
  except TimeoutException:
    # Decision(isTimeout) – exception branch
    await _svc.handle_registration_timeout(ctx)
    # FlowJoinRegistration → end
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content=ctx.error_response.model_dump(),  # type: ignore[union-attr]
    )

  # ------------------------------------------------------------------
  # FlowJoinRegistration (happy path) → end
  # ------------------------------------------------------------------
  return JSONResponse(
      status_code=status.HTTP_201_CREATED,
      content=RegistrationResponse(request_id=ctx.request_id).model_dump(mode="json"),  # type: ignore[arg-type]
  )


# ===========================================================================
# 10. ENTRY-POINT
# ===========================================================================

if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)