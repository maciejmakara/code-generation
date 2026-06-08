from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field


# ============================================================
# Logging
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("user_registration")


# ============================================================
# Exception Classes (derived from @meta.exceptions)
# ============================================================

class RegistrationException(Exception):
  """Base class for registration-related errors."""


class ValidationException(RegistrationException):
  """Raised when request validation fails."""


class TimeoutException(RegistrationException):
  """Raised when a timeout occurs in repository/external operations."""


class ConflictException(RegistrationException):
  """Raised when the email is already registered."""


class DisposableEmailDetected(RegistrationException):
  """Raised when the email domain is disposable/temporary."""


# ============================================================
# DTOs / Models
# ============================================================

class RegistrationDTO(BaseModel):
  email: EmailStr
  password: str = Field(..., min_length=8)
  first_name: str = Field(..., min_length=1)
  last_name: str = Field(..., min_length=1)


class ValidatedUserData(BaseModel):
  email: EmailStr
  password_hash: str
  first_name: str
  last_name: str


class ErrorResponse(BaseModel):
  code: str
  message: str
  request_id: Optional[str] = None
  details: Optional[Dict[str, Any]] = None


class RegistrationSuccessResponse(BaseModel):
  requestId: str


@dataclass
class RegistrationContext:
  validatedUserData: Optional[ValidatedUserData] = None
  emailAllowed: Optional[bool] = None
  userId: Optional[str] = None
  email: Optional[str] = None
  emailSentStatus: Optional[bool] = None
  errorResponse: Optional[ErrorResponse] = None


# ============================================================
# Infrastructure Helpers
# ============================================================

class AuditService:
  def write(self, action_name: str, details: Dict[str, Any]) -> None:
    logger.info("AUDIT action=%s details=%s", action_name, details)


class InMemoryUserRepository:
  """
  Demo repository.
  In production replace with a real database implementation.
  """
  def __init__(self) -> None:
    self._users_by_email: Dict[str, Dict[str, Any]] = {}
    self._users_by_id: Dict[str, Dict[str, Any]] = {}

  def email_exists(self, email: str) -> bool:
    return email.lower() in self._users_by_email

  def create_user(self, validated_user_data: ValidatedUserData) -> Dict[str, Any]:
    user_id = str(uuid.uuid4())
    record = {
      "user_id": user_id,
      "email": validated_user_data.email.lower(),
      "password_hash": validated_user_data.password_hash,
      "first_name": validated_user_data.first_name,
      "last_name": validated_user_data.last_name,
      "status": "PENDING",
    }
    self._users_by_email[validated_user_data.email.lower()] = record
    self._users_by_id[user_id] = record
    return record

  def finalize_registration(self, user_id: str) -> str:
    if user_id not in self._users_by_id:
      raise ValidationException("Cannot finalize registration for unknown user.")
    self._users_by_id[user_id]["status"] = "ACTIVE"
    return str(uuid.uuid4())

  def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
    return self._users_by_id.get(user_id)


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


@dataclass
class RetryPolicy:
  max_attempts: int
  backoff_ms: int
  retry_on: List[str]
  on_retry_exhausted: str


async def execute_with_retry(
    func: Callable[[], Any],
    retry_policy: RetryPolicy,
    classify_error: Callable[[Exception], str],
) -> Any:
  attempt = 0
  while True:
    attempt += 1
    try:
      result = func()
      if asyncio.iscoroutine(result):
        result = await result
      return result
    except Exception as exc:
      classification = classify_error(exc)
      should_retry = classification in retry_policy.retry_on
      is_last_attempt = attempt >= retry_policy.max_attempts

      logger.warning(
          "Retryable operation failed: attempt=%s/%s classification=%s error=%s",
          attempt,
          retry_policy.max_attempts,
          classification,
          repr(exc),
      )

      if (not should_retry) or is_last_attempt:
        if retry_policy.on_retry_exhausted == "raise":
          raise
        return None

      await asyncio.sleep(retry_policy.backoff_ms / 1000.0)


# ============================================================
# Services
# ============================================================

class UtilityService:
  def __init__(self, audit_service: AuditService) -> None:
    self.audit_service = audit_service

  def audit_if_required(self, action_name: str, required: bool, details: Dict[str, Any]) -> None:
    if required:
      self.audit_service.write(action_name, details)

  def hash_password(self, raw_password: str) -> str:
    # Assumption: simple deterministic placeholder hash for self-contained example.
    # In production use bcrypt/argon2.
    return f"hashed::{raw_password}"


class RegistrationService:
  def __init__(
      self,
      repository: InMemoryUserRepository,
      utility_service: UtilityService,
      transaction_manager: TransactionManager,
  ) -> None:
    self.repository = repository
    self.utility_service = utility_service
    self.transaction_manager = transaction_manager

  # Action: ValidateUserInput()
  def ValidateUserInput(self, registrationDTO: RegistrationDTO) -> ValidatedUserData:
    """
    @meta:
    stereotype=Validation
    desc=Validate registration DTO structure and required fields.
    inputs=[registrationDTO(request)]
    outputs=[validatedUserData(context)]
    exceptions=[ValidationException]
    """
    if not registrationDTO.email:
      raise ValidationException("Email is required.")
    if not registrationDTO.password or len(registrationDTO.password) < 8:
      raise ValidationException("Password must be at least 8 characters long.")
    if not registrationDTO.first_name.strip():
      raise ValidationException("First name is required.")
    if not registrationDTO.last_name.strip():
      raise ValidationException("Last name is required.")

    validated = ValidatedUserData(
        email=registrationDTO.email,
        password_hash=self.utility_service.hash_password(registrationDTO.password),
        first_name=registrationDTO.first_name.strip(),
        last_name=registrationDTO.last_name.strip(),
    )
    return validated

  # Action: CheckEmailUniqueness()
  def CheckEmailUniqueness(self, validatedUserData: ValidatedUserData) -> None:
    """
    @meta:
    stereotype=Repository
    desc=Verify email address is not already registered.
    inputs=[validatedUserData(context)]
    exceptions=[TimeoutException, ConflictException]
    sideEffects=false
    idempotent=true
    consistencyScope=none
    """
    # Assumption: simulated timeout trigger for demo/testing.
    if validatedUserData.email.endswith("@timeout-check.example"):
      raise TimeoutException("Timeout while verifying email uniqueness.")

    if self.repository.email_exists(validatedUserData.email):
      raise ConflictException("Email address is already registered.")

  # Action: FilterDisposableEmailDomains()
  def FilterDisposableEmailDomains(self, validatedUserData: ValidatedUserData) -> bool:
    """
    @meta:
    stereotype=Filter
    desc=Reject registrations from known disposable or temporary email domains using a maintained blocklist.
    inputs=[validatedUserData(context)]
    outputs=[emailAllowed(context)]
    exceptions=[DisposableEmailDetected]
    auditRequired=true
    llmPriority=high
    """
    blocked_domains = {
      "mailinator.com",
      "10minutemail.com",
      "guerrillamail.com",
      "tempmail.com",
      "trashmail.com",
    }
    domain = validatedUserData.email.split("@")[-1].lower()

    self.utility_service.audit_if_required(
        action_name="FilterDisposableEmailDomains",
        required=True,
        details={"email": validatedUserData.email, "domain": domain},
    )

    if domain in blocked_domains:
      raise DisposableEmailDetected(f"Disposable email domain rejected: {domain}")

    return True

  # Action: CreateUserAccount()
  def CreateUserAccount(self, validatedUserData: ValidatedUserData) -> Dict[str, str]:
    """
    @meta:
    stereotype=Repository
    desc=Persist new user account with hashed credentials.
    inputs=[validatedUserData(context)]
    outputs=[userId(context), email(context)]
    exceptions=[TimeoutException]
    sideEffects=true
    idempotent=false
    consistencyScope=local-transaction
    transactional=true
    auditRequired=true
    llmPriority=high
    """
    self.utility_service.audit_if_required(
        action_name="CreateUserAccount",
        required=True,
        details={"email": validatedUserData.email},
    )

    # Assumption: simulated timeout trigger for demo/testing.
    if validatedUserData.email.endswith("@timeout-create.example"):
      raise TimeoutException("Timeout while creating user account.")

    with self.transaction_manager.transaction("CreateUserAccount"):
      record = self.repository.create_user(validatedUserData)

    return {
      "userId": record["user_id"],
      "email": record["email"],
    }

  # Action: SendActivationEmail()
  async def SendActivationEmail(self, userId: str, email: str) -> bool:
    """
    @meta:
    stereotype=ExternalCall
    desc=Send activation email with confirmation link.
    inputs=[userId(context), email(context)]
    outputs=[emailSentStatus(context)]
    exceptions=[TimeoutException]
    retryPolicy={maxAttempts:3, backoffMs:300, retryOn:[Timeout, 502, 503], onRetryExhausted:raise}
    """
    retry_policy = RetryPolicy(
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="raise",
    )

    def classify_error(exc: Exception) -> str:
      if isinstance(exc, TimeoutException):
        return "Timeout"
      message = str(exc)
      if "502" in message:
        return "502"
      if "503" in message:
        return "503"
      return exc.__class__.__name__

    async def do_send() -> bool:
      # Assumption: simulated timeout trigger for demo/testing.
      if email.endswith("@timeout-email.example"):
        raise TimeoutException("Timeout while sending activation email.")
      logger.info("Activation email sent to email=%s for userId=%s", email, userId)
      return True

    return await execute_with_retry(do_send, retry_policy, classify_error)

  # Action: FinalizeRegistration()
  def FinalizeRegistration(self, userId: str) -> str:
    """
    @meta:
    stereotype=Repository
    desc=Mark registration as complete and set user status active.
    inputs=[userId(context)]
    outputs=[requestId(response)]
    postCondition=userStatus_Active
    sideEffects=true
    idempotent=true
    consistencyScope=local-transaction
    transactional=true
    """
    with self.transaction_manager.transaction("FinalizeRegistration"):
      request_id = self.repository.finalize_registration(userId)

      user = self.repository.get_user(userId)
      if not user or user.get("status") != "ACTIVE":
        raise ValidationException("Post-condition failed: userStatus_Active")

    return request_id

  # Action: HandleRegistrationTimeout()
  def HandleRegistrationTimeout(self, userId: Optional[str]) -> ErrorResponse:
    """
    @meta:
    stereotype=ErrorHandler
    desc=Handle timeout – log failure and return error response.
         Called regardless of which action triggered timeout, userId may not be available.
    inputs=[userId(context)]
    outputs=[errorResponse(response)]
    postCondition=registrationStatus_Failed
    """
    logger.error("Registration timeout occurred. userId=%s", userId)
    error_response = ErrorResponse(
        code="REGISTRATION_TIMEOUT",
        message="Registration failed due to a timeout.",
        request_id=userId,
        details={"registrationStatus": "Failed"},
    )

    if error_response.details.get("registrationStatus") != "Failed":
      raise ValidationException("Post-condition failed: registrationStatus_Failed")

    return error_response


# ============================================================
# REST Controller
# ============================================================

app = FastAPI(title="User Registration API")

repository = InMemoryUserRepository()
audit_service = AuditService()
utility_service = UtilityService(audit_service=audit_service)
transaction_manager = TransactionManager()
registration_service = RegistrationService(
    repository=repository,
    utility_service=utility_service,
    transaction_manager=transaction_manager,
)


@app.post(
    "/users/register",
    response_model=RegistrationSuccessResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
      400: {"model": ErrorResponse, "description": "Bad Request"},
      409: {"model": ErrorResponse, "description": "Conflict"},
      422: {"model": ErrorResponse, "description": "Unprocessable Entity"},
      504: {"model": ErrorResponse, "description": "Gateway Timeout"},
    },
)
async def main_Userregistration(
    request_body: RegistrationDTO,
    request: Request,
    x_request_id: Optional[str] = Header(default=None),
):
  """
  REST DEFINITION
  @meta {
    "endpoint": "POST /users/register",
    "responseSuccess": "201 Created",
    "responseError": "400|409|422|504"
  }

  Deterministic orchestration of the specification exactly as provided.
  """
  context = RegistrationContext()

  try:
    # Step 1: ValidateUserInput()
    context.validatedUserData = registration_service.ValidateUserInput(
        registrationDTO=request_body
    )

    # Step 2: CheckEmailUniqueness()
    # This action is before Decision(isTimeout), therefore timeout here is handled
    # by endpoint-level mapping, not by the later explicit Decision block.
    registration_service.CheckEmailUniqueness(
        validatedUserData=context.validatedUserData
    )

    # Step 3: FilterDisposableEmailDomains()
    context.emailAllowed = registration_service.FilterDisposableEmailDomains(
        validatedUserData=context.validatedUserData
    )

    # Step 4: Decision(isTimeout)
    #
    # Spec branches:
    #   if okfilterdisposableemaildomains:
    #       CreateUserAccount()
    #       BackToDecision(isTimeout)(okcreateuseraccount)
    #   if exception:
    #       HandleRegistrationTimeout()
    #       FlowJoinRegistration()
    #   if okcreateuseraccount:
    #       SendActivationEmail()
    #       BackToDecision(isTimeout)(oksendactivationemail)
    #   if oksendactivationemail:
    #       FinalizeRegistration()
    #       FlowJoinRegistration()
    #
    # Implemented as explicit deterministic re-entry loop over the same decision.
    decision_state = "okfilterdisposableemaildomains"

    while True:
      if decision_state == "okfilterdisposableemaildomains":
        try:
          create_result = registration_service.CreateUserAccount(
              validatedUserData=context.validatedUserData
          )
          context.userId = create_result["userId"]
          context.email = create_result["email"]
          # BackToDecision(isTimeout)(okcreateuseraccount)
          decision_state = "okcreateuseraccount"
          continue
        except TimeoutException:
          decision_state = "exception"
          continue

      if decision_state == "exception":
        context.errorResponse = registration_service.HandleRegistrationTimeout(
            userId=context.userId
        )
        # FlowJoinRegistration()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=context.errorResponse.model_dump(),
        )

      if decision_state == "okcreateuseraccount":
        try:
          context.emailSentStatus = await registration_service.SendActivationEmail(
              userId=context.userId,
              email=context.email,
          )
          # BackToDecision(isTimeout)(oksendactivationemail)
          decision_state = "oksendactivationemail"
          continue
        except TimeoutException:
          decision_state = "exception"
          continue

      if decision_state == "oksendactivationemail":
        request_id = registration_service.FinalizeRegistration(
            userId=context.userId
        )
        # FlowJoinRegistration()
        return RegistrationSuccessResponse(requestId=request_id)

      # Defensive guard; not part of spec logic, but ensures deterministic failure
      # if the internal state becomes invalid.
      raise RuntimeError(f"Unknown decision state: {decision_state}")

  except HTTPException:
    raise

  except ValidationException as exc:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=ErrorResponse(
            code="VALIDATION_ERROR",
            message=str(exc),
            request_id=x_request_id,
        ).model_dump(),
    ) from exc

  except DisposableEmailDetected as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorResponse(
            code="DISPOSABLE_EMAIL_REJECTED",
            message=str(exc),
            request_id=x_request_id,
        ).model_dump(),
    ) from exc

  except ConflictException as exc:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorResponse(
            code="EMAIL_ALREADY_EXISTS",
            message=str(exc),
            request_id=x_request_id,
        ).model_dump(),
    ) from exc

  except TimeoutException as exc:
    # Timeout outside explicit Decision(isTimeout) block.
    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail=ErrorResponse(
            code="TIMEOUT",
            message=str(exc),
            request_id=x_request_id,
        ).model_dump(),
    ) from exc

  except Exception as exc:
    logger.exception("Unexpected registration failure")
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorResponse(
            code="REGISTRATION_ERROR",
            message="Registration failed.",
            request_id=x_request_id,
            details={"error": str(exc)},
        ).model_dump(),
    ) from exc


# ============================================================
# FlowJoin function from spec
# ============================================================

def FlowJoinRegistration() -> None:
  """
  function FlowJoinRegistration():
    end()

  Included explicitly because the specification requires it.
  In code, the join is represented by deterministic return/termination points.
  """
  return


# ============================================================
# Optional local run entrypoint
# ============================================================

if __name__ == "__main__":
  import uvicorn

  uvicorn.run(app, host="127.0.0.1", port=8000)