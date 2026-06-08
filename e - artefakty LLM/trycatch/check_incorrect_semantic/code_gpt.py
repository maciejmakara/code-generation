from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field


# =========================
# Exceptions (@meta.exceptions + flow/runtime)
# =========================

class DomainException(Exception):
  """Base class for domain exceptions."""


class ValidationException(DomainException):
  pass


class TimeoutException(DomainException):
  pass


class ConflictException(DomainException):
  pass


class DisposableEmailDetected(DomainException):
  pass


class PostConditionException(DomainException):
  pass


class InternalFlowException(DomainException):
  pass


# =========================
# DTOs / models
# =========================

class RegistrationDTO(BaseModel):
  email: EmailStr
  password: str = Field(min_length=8, max_length=256)
  first_name: str = Field(min_length=1, max_length=100)
  last_name: str = Field(min_length=1, max_length=100)


class ValidatedUserData(BaseModel):
  email: EmailStr
  password_hash: str
  first_name: str
  last_name: str


class SuccessResponse(BaseModel):
  requestId: uuid.UUID
  message: str = "Created"


class ErrorResponse(BaseModel):
  code: str
  message: str
  requestId: Optional[uuid.UUID] = None


@dataclass
class RegistrationContext:
  # @meta outputs/targets -> context
  validatedUserData: Optional[ValidatedUserData] = None
  emailAllowed: Optional[bool] = None
  userId: Optional[uuid.UUID] = None
  email: Optional[str] = None
  emailSentStatus: Optional[bool] = None

  # control-flow flags (Decision / BackToDecision)
  isTimeout: bool = False
  exception: bool = False
  okfilterdisposableemaildomains: bool = False
  okcreateuseraccount: bool = False
  oksendactivationemail: bool = False

  # post-condition state
  registrationStatus: Optional[str] = None
  userStatus: Optional[str] = None

  request_id: uuid.UUID = field(default_factory=uuid.uuid4)
  last_exception: Optional[Exception] = None


# =========================
# Helpers (retry/audit/transaction)
# =========================

logger = logging.getLogger("user-registration")
logging.basicConfig(level=logging.INFO)


class AuditService:
  def log(self, action: str, payload: Dict[str, Any]) -> None:
    logger.info("AUDIT action=%s payload=%s", action, payload)


class RetryPolicy(BaseModel):
  maxAttempts: int
  backoffMs: int
  retryOn: List[str]
  onRetryExhausted: str


class RetryExecutor:
  async def run(self, func, retry_policy: RetryPolicy):
    attempt = 0
    while True:
      attempt += 1
      try:
        return await func()
      except Exception as exc:  # noqa: BLE001
        label = self._label_for_exception(exc)
        should_retry = label in retry_policy.retryOn and attempt < retry_policy.maxAttempts
        if should_retry:
          await asyncio.sleep(retry_policy.backoffMs / 1000.0)
          continue
        if retry_policy.onRetryExhausted == "raise":
          raise
        return None

  @staticmethod
  def _label_for_exception(exc: Exception) -> str:
    if isinstance(exc, TimeoutException):
      return "Timeout"
    if isinstance(exc, ExternalServiceHTTPException):
      return str(exc.status_code)
    return exc.__class__.__name__


@contextmanager
def transaction_boundary(name: str):
  logger.info("BEGIN TRANSACTION: %s", name)
  try:
    yield
    logger.info("COMMIT TRANSACTION: %s", name)
  except Exception:  # noqa: BLE001
    logger.info("ROLLBACK TRANSACTION: %s", name)
    raise


# =========================
# Repository / external integrations (in-memory demo)
# =========================

class InMemoryUserStore:
  def __init__(self):
    self._users_by_email: Dict[str, Dict[str, Any]] = {}
    self._users_by_id: Dict[uuid.UUID, Dict[str, Any]] = {}

  def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
    return self._users_by_email.get(email.lower())

  def insert_user(self, user_id: uuid.UUID, user_record: Dict[str, Any]) -> None:
    self._users_by_email[user_record["email"].lower()] = user_record
    self._users_by_id[user_id] = user_record

  def activate_user(self, user_id: uuid.UUID) -> None:
    if user_id not in self._users_by_id:
      raise ValidationException("User does not exist.")
    self._users_by_id[user_id]["status"] = "ACTIVE"


class ExternalServiceHTTPException(Exception):
  def __init__(self, status_code: int, message: str):
    self.status_code = status_code
    super().__init__(message)


# =========================
# Services (one method per action)
# =========================

class RegistrationValidationService:
  # ValidateUserInput()
  def ValidateUserInput(self, registrationDTO: RegistrationDTO) -> ValidatedUserData:
    # @meta desc: Validate registration DTO structure and required fields.
    if not registrationDTO.email or not registrationDTO.password:
      raise ValidationException("Missing required fields.")
    password_hash = hashlib.sha256(registrationDTO.password.encode("utf-8")).hexdigest()
    return ValidatedUserData(
        email=registrationDTO.email,
        password_hash=password_hash,
        first_name=registrationDTO.first_name.strip(),
        last_name=registrationDTO.last_name.strip(),
    )


class RegistrationRepositoryService:
  def __init__(self, store: InMemoryUserStore, audit: AuditService):
    self.store = store
    self.audit = audit

  # CheckEmailUniqueness()
  def CheckEmailUniqueness(self, validatedUserData: ValidatedUserData) -> None:
    # @meta consistencyScope: none, idempotent: true, sideEffects: false
    # Assumption: timeout simulation hook via special domain; in real code use DB timeout exceptions.
    if validatedUserData.email.endswith("@simulate-timeout.local"):
      raise TimeoutException("Repository timeout while checking uniqueness.")
    existing = self.store.get_by_email(validatedUserData.email)
    if existing:
      raise ConflictException("Email is already registered.")

  # CreateUserAccount()
  def CreateUserAccount(self, validatedUserData: ValidatedUserData) -> Tuple[uuid.UUID, str]:
    # @meta transactional: true, consistencyScope: local-transaction
    if validatedUserData.email.endswith("@simulate-timeout.local"):
      raise TimeoutException("Repository timeout while creating user.")
    with transaction_boundary("CreateUserAccount"):
      user_id = uuid.uuid4()
      self.store.insert_user(
          user_id,
          {
            "id": user_id,
            "email": str(validatedUserData.email),
            "password_hash": validatedUserData.password_hash,
            "first_name": validatedUserData.first_name,
            "last_name": validatedUserData.last_name,
            "status": "PENDING_ACTIVATION",
            "created_at": time.time(),
          },
      )
      self.audit.log("CreateUserAccount", {"userId": str(user_id), "email": str(validatedUserData.email)})
      return user_id, str(validatedUserData.email)

  # FinalizeRegistration()
  def FinalizeRegistration(self, userId: uuid.UUID) -> uuid.UUID:
    # @meta transactional: true, idempotent: true
    with transaction_boundary("FinalizeRegistration"):
      self.store.activate_user(userId)
      request_id = uuid.uuid4()
      return request_id


class RegistrationFilterService:
  def __init__(self, audit: AuditService):
    self.audit = audit
    self.blocked_domains = {
      "mailinator.com",
      "10minutemail.com",
      "guerrillamail.com",
      "tempmail.com",
    }

  # FilterDisposableEmailDomains()
  def FilterDisposableEmailDomains(self, validatedUserData: ValidatedUserData) -> bool:
    domain = str(validatedUserData.email).split("@")[-1].lower()
    if domain in self.blocked_domains:
      self.audit.log("FilterDisposableEmailDomains", {"email": str(validatedUserData.email), "blocked": True})
      raise DisposableEmailDetected("Disposable email domains are not allowed.")
    self.audit.log("FilterDisposableEmailDomains", {"email": str(validatedUserData.email), "blocked": False})
    return True


class RegistrationNotificationService:
  def __init__(self, retry_executor: RetryExecutor):
    self.retry_executor = retry_executor

  # SendActivationEmail()
  async def SendActivationEmail(self, userId: uuid.UUID, email: str) -> bool:
    retry_policy = RetryPolicy(
        maxAttempts=3,
        backoffMs=300,
        retryOn=["Timeout", "502", "503"],
        onRetryExhausted="raise",
    )

    async def _send_once() -> bool:
      # Assumption: simulation hooks by domain to emulate integration instability.
      if email.endswith("@simulate-timeout.local"):
        raise TimeoutException("Email provider timed out.")
      if email.endswith("@simulate-502.local"):
        raise ExternalServiceHTTPException(502, "Bad Gateway")
      if email.endswith("@simulate-503.local"):
        raise ExternalServiceHTTPException(503, "Service Unavailable")
      await asyncio.sleep(0.01)
      return True

    return await self.retry_executor.run(_send_once, retry_policy)


class RegistrationErrorHandlerService:
  # HandleRegistrationTimeout()
  def HandleRegistrationTimeout(self, userId: Optional[uuid.UUID]) -> ErrorResponse:
    logger.error("Registration timeout handled. userId=%s", str(userId) if userId else None)
    # postCondition: registrationStatus_Failed is validated by controller context update.
    return ErrorResponse(
        code="TIMEOUT",
        message="Registration request timed out. Please retry later.",
        requestId=userId,
    )


# =========================
# FlowJoin
# =========================

def FlowJoinRegistration() -> None:
  # function FlowJoinRegistration(): end()
  return None


# =========================
# Controller / REST endpoint
# REST DEFINITION @meta:
# endpoint: POST /users/register
# responseSuccess: 201 Created
# responseError: 400|409|422|504
# =========================

app = FastAPI(title="User Registration API", version="1.0.0")

audit_service = AuditService()
store = InMemoryUserStore()
validation_service = RegistrationValidationService()
repository_service = RegistrationRepositoryService(store, audit_service)
filter_service = RegistrationFilterService(audit_service)
notification_service = RegistrationNotificationService(RetryExecutor())
error_handler_service = RegistrationErrorHandlerService()


@app.post("/users/register", status_code=status.HTTP_201_CREATED)
async def main_Userregistration(
    request: Request,
    registrationDTO: RegistrationDTO,
    x_request_id: Optional[str] = Header(default=None),
):
  context = RegistrationContext()

  try:
    # 1) ValidateUserInput()
    context.validatedUserData = validation_service.ValidateUserInput(registrationDTO)

    # 2) CheckEmailUniqueness()
    try:
      repository_service.CheckEmailUniqueness(context.validatedUserData)
    except TimeoutException as exc:
      context.isTimeout = True
      context.exception = True
      context.last_exception = exc

    # 3) FilterDisposableEmailDomains()
    if not context.exception:
      context.emailAllowed = filter_service.FilterDisposableEmailDomains(context.validatedUserData)
      context.okfilterdisposableemaildomains = bool(context.emailAllowed)

    # 4) Decision(isTimeout) with explicit BackToDecision re-entry
    while True:
      # if okfilterdisposableemaildomains:
      if context.okfilterdisposableemaildomains and not context.okcreateuseraccount and not context.exception:
        try:
          # CreateUserAccount()
          user_id, email = repository_service.CreateUserAccount(context.validatedUserData)
          context.userId = user_id
          context.email = email
          context.okcreateuseraccount = True
          # BackToDecision(isTimeout)(okcreateuseraccount)
          continue
        except TimeoutException as exc:
          context.isTimeout = True
          context.exception = True
          context.last_exception = exc

      # if exception:
      if context.exception:
        errorResponse = error_handler_service.HandleRegistrationTimeout(context.userId)
        context.registrationStatus = "FAILED"
        if context.registrationStatus != "FAILED":
          raise PostConditionException("postCondition violated: registrationStatus_Failed")
        FlowJoinRegistration()
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content=errorResponse.model_dump(mode="json"),
        )

      # if okcreateuseraccount:
      if context.okcreateuseraccount and not context.oksendactivationemail and not context.exception:
        try:
          # SendActivationEmail()
          context.emailSentStatus = await notification_service.SendActivationEmail(context.userId, context.email)
          context.oksendactivationemail = bool(context.emailSentStatus)
          # BackToDecision(isTimeout)(oksendactivationemail)
          continue
        except TimeoutException as exc:
          context.isTimeout = True
          context.exception = True
          context.last_exception = exc

      # if oksendactivationemail:
      if context.oksendactivationemail:
        # FinalizeRegistration()
        requestId = repository_service.FinalizeRegistration(context.userId)
        context.userStatus = "ACTIVE"
        if context.userStatus != "ACTIVE":
          raise PostConditionException("postCondition violated: userStatus_Active")
        FlowJoinRegistration()
        success = SuccessResponse(requestId=requestId)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=success.model_dump(mode="json"),
        )

      # Defensive deterministic failure for impossible/unmodeled path.
      raise InternalFlowException("No matching branch in Decision(isTimeout).")

  except ValidationException as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  except ConflictException as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
  except DisposableEmailDetected as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
  except PostConditionException as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
  except TimeoutException as exc:
    # Fallback timeout mapping if encountered outside modeled decision flow.
    raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
  except HTTPException:
    raise
  except Exception as exc:  # noqa: BLE001
    logger.exception("Unhandled error in registration flow")
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unhandled registration error.") from exc
