"""
POST /users/register — User Registration Endpoint
Generated from UML Activity Diagram pseudo-code specification.

Components:
  - DTOs / data models
  - Custom exception classes
  - AuditLogger helper
  - UserRegistrationService  (all internal actions)
  - UserRegistrationController (REST orchestration + Decision/FlowJoin logic)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# DTOs / Data Models
# ──────────────────────────────────────────────

@dataclass
class RegistrationDTO:
    """Incoming registration payload from the HTTP request body."""
    email: str
    password: str
    full_name: str


@dataclass
class ValidatedUserData:
    """Sanitised, validated representation of the registration fields."""
    email: str
    password_hash: str
    full_name: str


@dataclass
class ErrorResponse:
    """Unified error shape returned to the caller."""
    error_code: str
    message: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class RegistrationContext:
    """
    Mutable per-request context object.
    Outputs targeting "context" are stored here; outputs targeting
    "response" are placed in response_payload.
    """
    validated_user_data: Optional[ValidatedUserData] = None
    email_allowed: Optional[bool] = None
    user_id: Optional[uuid.UUID] = None
    email: Optional[str] = None
    email_sent_status: Optional[bool] = None
    # response-targeted outputs
    request_id: Optional[uuid.UUID] = None
    error_response: Optional[ErrorResponse] = None


# ──────────────────────────────────────────────
# Custom Exceptions
# ──────────────────────────────────────────────

class ValidationException(Exception):
    """Raised when the registration DTO fails structural or field validation."""


class TimeoutException(Exception):
    """Raised when an operation times out (DB or external call)."""


class ConflictException(Exception):
    """Raised when the supplied e-mail address is already registered."""


class DisposableEmailDetected(Exception):
    """Raised when the e-mail domain is on the disposable-email blocklist."""


# ──────────────────────────────────────────────
# Audit Logger Helper
# ──────────────────────────────────────────────

class AuditLogger:
    """
    Lightweight audit helper.
    auditRequired=true actions call log_event().
    In production this would write to a dedicated audit store.
    """

    @staticmethod
    def log_event(action: str, context: RegistrationContext, extra: dict | None = None) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user_id": str(context.user_id) if context.user_id else None,
            "email": context.email or (
                context.validated_user_data.email if context.validated_user_data else None
            ),
            **(extra or {}),
        }
        logger.info("[AUDIT] %s", entry)


# ──────────────────────────────────────────────
# Retry Helper
# ──────────────────────────────────────────────

async def with_retry(
    coro_factory,
    *,
    max_attempts: int,
    backoff_ms: int,
    retry_on: tuple[type[Exception], ...],
    on_retry_exhausted: str = "raise",
) -> Any:
    """
    Generic async retry wrapper.
    retryPolicy: {maxAttempts, backoffMs, retryOn, onRetryExhausted}.
    onRetryExhausted="raise" re-raises the last exception.
    """
    last_exc: Exception = RuntimeError("retry wrapper: no attempts made")
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except retry_on as exc:
            last_exc = exc
            if attempt < max_attempts:
                wait_s = (backoff_ms / 1000) * attempt  # linear back-off
                logger.warning(
                    "Retry %d/%d after %.3fs — %s", attempt, max_attempts, wait_s, exc
                )
                await asyncio.sleep(wait_s)
            else:
                logger.error("All %d retry attempts exhausted: %s", max_attempts, exc)
                if on_retry_exhausted == "raise":
                    raise last_exc
    return None  # unreachable, satisfies type-checker


# ──────────────────────────────────────────────
# Service: UserRegistrationService
# ──────────────────────────────────────────────

# Inline disposable-email blocklist (in production: external maintained list).
_DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {"mailinator.com", "guerrillamail.com", "tempmail.com", "throwam.com", "yopmail.com"}
)


class UserRegistrationService:
    """
    Implements every internal action from the specification.
    Each method maps 1-to-1 to an action node.
    """

    # ------------------------------------------------------------------
    # Action: ValidateUserInput
    # Stereotype: Validation
    # @meta desc: Validate registration DTO structure and required fields.
    # @meta exceptions: ValidationException
    # ------------------------------------------------------------------
    async def validate_user_input(
        self, registration_dto: RegistrationDTO, ctx: RegistrationContext
    ) -> None:
        """
        Validates that all required fields are present and well-formed.
        Output: ctx.validated_user_data (ValidatedUserData)
        """
        errors: list[str] = []

        if not registration_dto.email or "@" not in registration_dto.email:
            errors.append("email: missing or malformed")
        if not registration_dto.password or len(registration_dto.password) < 8:
            errors.append("password: must be at least 8 characters")
        if not registration_dto.full_name or not registration_dto.full_name.strip():
            errors.append("full_name: required")

        if errors:
            raise ValidationException(f"Validation failed: {'; '.join(errors)}")

        password_hash = hashlib.sha256(registration_dto.password.encode()).hexdigest()

        ctx.validated_user_data = ValidatedUserData(
            email=registration_dto.email.lower().strip(),
            password_hash=password_hash,
            full_name=registration_dto.full_name.strip(),
        )

    # ------------------------------------------------------------------
    # Action: CheckEmailUniqueness
    # Stereotype: Repository
    # @meta desc: Verify email address is not already registered.
    # @meta exceptions: TimeoutException, ConflictException
    # @meta sideEffects: false  idempotent: true  consistencyScope: none
    # ------------------------------------------------------------------
    async def check_email_uniqueness(self, ctx: RegistrationContext) -> None:
        """
        Reads from the user store to verify the email is not taken.
        Raises ConflictException if already registered.
        Raises TimeoutException on DB timeout.
        (Simulated: in production query the DB.)
        """
        assert ctx.validated_user_data is not None  # pre-condition: prior step succeeded

        # Simulation: a real DB query would go here.
        # Assumption: we treat any email ending with '@taken.com' as already registered.
        if ctx.validated_user_data.email.endswith("@taken.com"):
            raise ConflictException(
                f"Email already registered: {ctx.validated_user_data.email}"
            )

    # ------------------------------------------------------------------
    # Action: FilterDisposableEmailDomains
    # Stereotype: Filter
    # @meta desc: Reject registrations from known disposable / temporary email domains.
    # @meta exceptions: DisposableEmailDetected
    # @meta auditRequired: true   llmPriority: high
    # Output: ctx.email_allowed (bool)
    # ------------------------------------------------------------------
    async def filter_disposable_email_domains(self, ctx: RegistrationContext) -> None:
        """
        Checks the email domain against a maintained blocklist.
        Sets ctx.email_allowed = False and raises DisposableEmailDetected if blocked.
        auditRequired=true: audit entry written on detection.
        llmPriority=high: explicit, fail-fast logic; no silent pass-through.
        """
        assert ctx.validated_user_data is not None

        domain = ctx.validated_user_data.email.split("@", 1)[-1]
        if domain in _DISPOSABLE_DOMAINS:
            ctx.email_allowed = False
            AuditLogger.log_event(
                "FilterDisposableEmailDomains:BLOCKED", ctx, {"domain": domain}
            )
            raise DisposableEmailDetected(
                f"Registrations from disposable domain '{domain}' are not allowed."
            )

        ctx.email_allowed = True

    # ------------------------------------------------------------------
    # Action: CreateUserAccount
    # Stereotype: Repository
    # @meta desc: Persist new user account with hashed credentials.
    # @meta exceptions: TimeoutException
    # @meta sideEffects: true  idempotent: false  consistencyScope: local-transaction
    # @meta transactional: true  auditRequired: true  llmPriority: high
    # Outputs: ctx.user_id (UUID), ctx.email (String)
    # ------------------------------------------------------------------
    async def create_user_account(self, ctx: RegistrationContext) -> None:
        """
        Persists the new user inside a local transaction boundary.
        transactional=true: begin/commit wrapped around the DB write.
        auditRequired=true: audit entry written after commit.
        llmPriority=high: strict transactional semantics enforced.
        """
        assert ctx.validated_user_data is not None

        # --- BEGIN local-transaction ---
        # In production: async with db.transaction(): ...
        try:
            new_id = uuid.uuid4()

            # Simulate DB write (insert user record).
            # Assumption: write always succeeds unless a TimeoutException is raised.
            logger.info(
                "DB INSERT users (id=%s, email=%s)", new_id, ctx.validated_user_data.email
            )

            ctx.user_id = new_id
            ctx.email = ctx.validated_user_data.email
            # --- COMMIT local-transaction ---
        except Exception:
            # --- ROLLBACK local-transaction ---
            raise

        # auditRequired=true
        AuditLogger.log_event("CreateUserAccount:SUCCESS", ctx)

    # ------------------------------------------------------------------
    # Action: HandleRegistrationTimeout
    # Stereotype: ErrorHandler
    # @meta desc: Handle timeout – log failure and return error response.
    #             Called regardless of which action triggered timeout;
    #             userId may not be available.
    # @meta postCondition: registrationStatus_Failed
    # Output: ctx.error_response (ErrorResponse → response)
    # ------------------------------------------------------------------
    async def handle_registration_timeout(self, ctx: RegistrationContext) -> None:
        """
        Logs the timeout failure and shapes the error response.
        postCondition: registrationStatus_Failed — the registration record
        (if partially created) must be marked as failed/rolled-back.
        """
        logger.error(
            "Registration timeout for user_id=%s",
            ctx.user_id if ctx.user_id else "<not yet created>",
        )

        ctx.error_response = ErrorResponse(
            error_code="REGISTRATION_TIMEOUT",
            message="Registration could not be completed due to a timeout. Please try again.",
        )

        # postCondition: registrationStatus_Failed
        # Assumption: if user_id exists we update the record status; otherwise no record
        # to update (timeout occurred before CreateUserAccount completed).
        if ctx.user_id:
            logger.info(
                "DB UPDATE users SET status='FAILED' WHERE id=%s (postCondition)", ctx.user_id
            )

    # ------------------------------------------------------------------
    # Action: SendActivationEmail
    # Stereotype: ExternalCall
    # @meta desc: Send activation email with confirmation link.
    # @meta exceptions: TimeoutException
    # @meta retryPolicy: {maxAttempts:3, backoffMs:300, retryOn:["Timeout","502","503"],
    #                      onRetryExhausted:"raise"}
    # Output: ctx.email_sent_status (bool)
    # ------------------------------------------------------------------
    async def _send_activation_email_once(self, ctx: RegistrationContext) -> None:
        """Single attempt to call the email-sending external service."""
        assert ctx.user_id is not None
        assert ctx.email is not None

        # Simulation: in production POST to an email-service API.
        logger.info(
            "ExternalCall: send activation email to %s (user_id=%s)", ctx.email, ctx.user_id
        )
        # Simulate success.  Replace with real HTTP call + raise TimeoutException on failure.
        ctx.email_sent_status = True

    async def send_activation_email(self, ctx: RegistrationContext) -> None:
        """
        Sends activation email, applying the specified retry policy:
          maxAttempts=3, backoffMs=300, retryOn=[Timeout,502,503],
          onRetryExhausted=raise.
        """
        await with_retry(
            lambda: self._send_activation_email_once(ctx),
            max_attempts=3,
            backoff_ms=300,
            retry_on=(TimeoutException,),  # 502/503 map to TimeoutException in this context
            on_retry_exhausted="raise",
        )

    # ------------------------------------------------------------------
    # Action: FinalizeRegistration
    # Stereotype: Repository
    # @meta desc: Mark registration as complete and set user status active.
    # @meta postCondition: userStatus_Active
    # @meta sideEffects: true  idempotent: true  consistencyScope: local-transaction
    # @meta transactional: true
    # Output: ctx.request_id (UUID → response)
    # ------------------------------------------------------------------
    async def finalize_registration(self, ctx: RegistrationContext) -> None:
        """
        Marks user as active inside a local transaction.
        transactional=true: explicit begin/commit boundary.
        postCondition: userStatus_Active — verified after write.
        idempotent=true: safe to replay (UPDATE is idempotent by nature).
        """
        assert ctx.user_id is not None

        # --- BEGIN local-transaction ---
        try:
            logger.info(
                "DB UPDATE users SET status='ACTIVE' WHERE id=%s (transactional)", ctx.user_id
            )
            ctx.request_id = uuid.uuid4()
            # --- COMMIT local-transaction ---
        except Exception:
            # --- ROLLBACK local-transaction ---
            raise

        # postCondition: userStatus_Active
        # Assumption: we verify by checking the simulated result flag.
        # In production: re-query the DB to confirm the status field is 'ACTIVE'.
        assert ctx.request_id is not None, "postCondition violated: request_id not set"
        logger.info("postCondition satisfied: userStatus_Active for user_id=%s", ctx.user_id)


# ──────────────────────────────────────────────
# Controller: UserRegistrationController
# REST: POST /users/register
# Orchestrates the full flow as per the specification.
# ──────────────────────────────────────────────

class UserRegistrationController:
    """
    Implements the Decision(isTimeout) / FlowJoin(Registration) control flow.

    Decision(isTimeout) semantics (from BackToDecision annotations):
      - okfilterdisposableemaildomains → try CreateUserAccount
      - okcreateuseraccount            → try SendActivationEmail
      - oksendactivationemail          → FinalizeRegistration → FlowJoin
      - exception (any TimeoutException in these actions) → HandleRegistrationTimeout → FlowJoin

    The Decision node is implemented as a try/except block encompassing
    CreateUserAccount, SendActivationEmail, and FinalizeRegistration,
    with TimeoutException routing to HandleRegistrationTimeout.
    """

    def __init__(self, service: UserRegistrationService) -> None:
        self.service = service

    # ----------------------------------------------------------------
    # FlowJoinRegistration — end()
    # ----------------------------------------------------------------
    async def flow_join_registration(self, ctx: RegistrationContext) -> JSONResponse:
        """
        Merge node: both success and timeout error paths converge here.
        Shapes the final HTTP response.
        """
        if ctx.error_response is not None:
            # Timeout error path
            return JSONResponse(
                status_code=504,
                content={
                    "error_code": ctx.error_response.error_code,
                    "message": ctx.error_response.message,
                    "timestamp": ctx.error_response.timestamp,
                },
            )

        # Success path: 201 Created
        return JSONResponse(
            status_code=201,
            content={"request_id": str(ctx.request_id)},
        )

    # ----------------------------------------------------------------
    # Main endpoint handler
    # ----------------------------------------------------------------
    async def register(self, registration_dto: RegistrationDTO) -> JSONResponse:
        """
        POST /users/register
        responseSuccess: 201 Created
        responseError:   400 | 409 | 422 | 504
        """
        ctx = RegistrationContext()

        # ── Step 1: ValidateUserInput ──────────────────────────────
        try:
            await self.service.validate_user_input(registration_dto, ctx)
        except ValidationException as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})

        # ── Step 2: CheckEmailUniqueness ───────────────────────────
        try:
            await self.service.check_email_uniqueness(ctx)
        except TimeoutException as exc:
            return JSONResponse(status_code=504, content={"error": str(exc)})
        except ConflictException as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})

        # ── Step 3: FilterDisposableEmailDomains ───────────────────
        # auditRequired=true, llmPriority=high: handled explicitly.
        try:
            await self.service.filter_disposable_email_domains(ctx)
        except DisposableEmailDetected as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

        # ── Decision(isTimeout) ────────────────────────────────────
        #
        #   if okfilterdisposableemaildomains:
        #       CreateUserAccount()        BackToDecision(isTimeout)(okcreateuseraccount)
        #   if okcreateuseraccount:
        #       SendActivationEmail()      BackToDecision(isTimeout)(oksendactivationemail)
        #   if oksendactivationemail:
        #       FinalizeRegistration()     FlowJoinRegistration
        #   if exception (TimeoutException anywhere in the above):
        #       HandleRegistrationTimeout  FlowJoinRegistration
        #
        # BackToDecision means: if the action SUCCEEDS proceed to the next
        # branch; if it raises TimeoutException fall through to the
        # exception branch.  Implemented as a single try/except block.

        try:
            # if okfilterdisposableemaildomains → CreateUserAccount
            await self.service.create_user_account(ctx)

            # BackToDecision(isTimeout)(okcreateuseraccount)
            # if okcreateuseraccount → SendActivationEmail
            await self.service.send_activation_email(ctx)

            # BackToDecision(isTimeout)(oksendactivationemail)
            # if oksendactivationemail → FinalizeRegistration → FlowJoinRegistration
            await self.service.finalize_registration(ctx)

        except TimeoutException:
            # if exception → HandleRegistrationTimeout → FlowJoinRegistration
            await self.service.handle_registration_timeout(ctx)

        # ── FlowJoinRegistration → end() ───────────────────────────
        return await self.flow_join_registration(ctx)


# ──────────────────────────────────────────────
# FastAPI wiring
# ──────────────────────────────────────────────

app = FastAPI(title="User Registration Service")

_service = UserRegistrationService()
_controller = UserRegistrationController(_service)


@app.post("/users/register", status_code=201)
async def register_user(request: Request) -> JSONResponse:
    """
    POST /users/register
    Parses the JSON body into RegistrationDTO and delegates to the controller.
    """
    body = await request.json()
    try:
        dto = RegistrationDTO(
            email=body.get("email", ""),
            password=body.get("password", ""),
            full_name=body.get("full_name", ""),
        )
    except (KeyError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"error": f"Malformed request body: {exc}"})

    return await _controller.register(dto)


# ──────────────────────────────────────────────
# Entry point (dev server)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("user_registration:app", host="0.0.0.0", port=8000, reload=True)
