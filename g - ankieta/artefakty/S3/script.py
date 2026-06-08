from __future__ import annotations

import asyncio
import base64
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, Field


# ============================================================
# Logging
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("statement_api")


# ============================================================
# Exceptions
# ============================================================

class AppException(Exception):
  status_code: int = 500
  error_code: str = "internal_error"

  def __init__(self, message: str):
    super().__init__(message)
    self.message = message


class BadRequest(AppException):
  status_code = 400
  error_code = "bad_request"


class Unauthorized(AppException):
  status_code = 401
  error_code = "unauthorized"


class Forbidden(AppException):
  status_code = 403
  error_code = "forbidden"


class NotFound(AppException):
  status_code = 404
  error_code = "not_found"


class Conflict(AppException):
  status_code = 409
  error_code = "conflict"


class UnprocessableEntity(AppException):
  status_code = 422
  error_code = "unprocessable_entity"


class JobNotFound(AppException):
  status_code = 404
  error_code = "job_not_found"


class AnalysisUnavailable(AppException):
  status_code = 409
  error_code = "analysis_unavailable"


class ProfileServiceUnavailable(AppException):
  status_code = 503
  error_code = "profile_service_unavailable"


class StorageUnavailable(AppException):
  status_code = 503
  error_code = "storage_unavailable"


class EventPublishFailed(AppException):
  status_code = 503
  error_code = "event_publish_failed"


class DataCorruption(AppException):
  status_code = 409
  error_code = "data_corruption"


class TimeoutException(AppException):
  status_code = 504
  error_code = "timeout"


class RetryExhausted(AppException):
  status_code = 503
  error_code = "retry_exhausted"


class PreConditionFailed(UnprocessableEntity):
  error_code = "precondition_failed"


class PostConditionFailed(UnprocessableEntity):
  error_code = "postcondition_failed"


# ============================================================
# Domain models / DTOs
# ============================================================

class StatementStatus(str, Enum):
  COMPLETED = "completed"
  RUNNING = "running"
  FAILED = "failed"
  NOT_FOUND = "not_found"


class JobProgress(BaseModel):
  stalled: bool
  progress_percent: int = 0


class FailureAnalysis(BaseModel):
  retryable_failure: bool
  root_cause: str


class RecoveryResult(BaseModel):
  recovery_started: bool
  recovery_job_id: str


class ResumePoint(BaseModel):
  checkpoint_id: str


class Transaction(BaseModel):
  transaction_id: str
  amount: float
  currency: str
  description: str


class CalculationResult(BaseModel):
  name: str
  value: float


class FeeSchedule(BaseModel):
  monthly_fee: float
  fx_markup_rate: float


class AccountProfile(BaseModel):
  account_id: str
  customer_name: str
  locale: str
  formatting_preference: Optional[str] = None


class FormattingPreferences(BaseModel):
  date_format: str
  currency_format: str
  locale: str


class StatementTotals(BaseModel):
  total_debits: float
  total_credits: float
  fees: float
  net_total: float


class ValidatedStatement(BaseModel):
  period_start: date
  period_end: date
  requested_format: str


class StatementRequest(BaseModel):
  periodStart: date
  periodEnd: date
  format: str = Field(default="PDF")


class StatementAcceptedResponse(BaseModel):
  statementId: str
  status: str
  downloadUrl: Optional[str] = None
  message: str


class ErrorResponse(BaseModel):
  error: str
  message: str


@dataclass
class StatementContext:
  customer_id: Optional[str] = None
  validated_statement: Optional[ValidatedStatement] = None
  existing_statement_id: Optional[str] = None
  account_id: Optional[str] = None
  statement_status: Optional[StatementStatus] = None
  job_progress: Optional[JobProgress] = None
  restarted_job_id: Optional[str] = None
  failure_analysis: Optional[FailureAnalysis] = None
  recovery_result: Optional[RecoveryResult] = None
  resume_point: Optional[ResumePoint] = None
  new_statement_id: Optional[str] = None
  statement_id: Optional[str] = None
  transactions: Optional[List[Transaction]] = None
  fee_schedule: Optional[FeeSchedule] = None
  preliminary_calculations: Optional[List[CalculationResult]] = None
  account_profile: Optional[AccountProfile] = None
  formatting_preferences: Optional[FormattingPreferences] = None
  statement_totals: Optional[StatementTotals] = None
  statement_document: Optional[bytes] = None
  download_url: Optional[str] = None
  job_status: Optional[str] = None
  response: Optional[StatementAcceptedResponse] = None
  events: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# In-memory infrastructure (single-file demo)
# ============================================================

class InMemoryDatabase:
  def __init__(self) -> None:
    self.statement_jobs: Dict[str, Dict[str, Any]] = {}
    self.idempotency_index: Dict[Tuple[str, str], str] = {}
    self.transactions: Dict[str, List[Transaction]] = {}
    self.job_progress: Dict[str, JobProgress] = {}

  def seed(self) -> None:
    account_id = "11111111-1111-1111-1111-111111111111"
    self.transactions[account_id] = [
      Transaction(transaction_id="t1", amount=-120.0, currency="USD", description="Card purchase"),
      Transaction(transaction_id="t2", amount=2500.0, currency="USD", description="Salary"),
      Transaction(transaction_id="t3", amount=-15.0, currency="USD", description="ATM fee"),
    ]


class InMemoryCache:
  def __init__(self) -> None:
    self.fee_schedule = FeeSchedule(monthly_fee=5.0, fx_markup_rate=0.02)


class EventBus:
  async def publish(self, event_name: str, payload: Dict[str, Any]) -> bool:
    logger.info("Published event %s with payload=%s", event_name, payload)
    return True


class ObjectStorageClient:
  async def store(self, statement_id: str, payload: bytes) -> str:
    # Deterministic demo implementation
    if not payload:
      raise StorageUnavailable("Empty statement document cannot be stored.")
    return f"https://object-storage.local/statements/{statement_id}.pdf"


class ProfileClient:
  async def fetch_profile(self, account_id: str) -> AccountProfile:
    # Deterministic demo behavior with a concrete fallback path available.
    return AccountProfile(
        account_id=account_id,
        customer_name="Jane Doe",
        locale="en_US",
        formatting_preference="STANDARD"
    )


# ============================================================
# Helpers
# ============================================================

class AuditLogger:
  @staticmethod
  def log(action_name: str, details: Dict[str, Any]) -> None:
    logger.info("AUDIT action=%s details=%s", action_name, details)


class ConditionChecker:
  JWT_REGEX = re.compile(r"^[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+$")

  @staticmethod
  def require(condition: bool, message: str, exc_type: type[AppException] = PreConditionFailed) -> None:
    if not condition:
      raise exc_type(message)

  @staticmethod
  def valid_jwt_format(token: Optional[str]) -> bool:
    return bool(token and ConditionChecker.JWT_REGEX.match(token))

  @staticmethod
  def customer_authenticated(ctx: StatementContext) -> bool:
    return bool(ctx.customer_id)

  @staticmethod
  def request_validated_and_idempotency_checked(ctx: StatementContext) -> bool:
    return ctx.validated_statement is not None and ctx.account_id is not None

  @staticmethod
  def statement_status_running(ctx: StatementContext) -> bool:
    return ctx.statement_status == StatementStatus.RUNNING

  @staticmethod
  def job_progress_evaluated(ctx: StatementContext) -> bool:
    return ctx.job_progress is not None

  @staticmethod
  def job_stalled(ctx: StatementContext) -> bool:
    return bool(ctx.job_progress and ctx.job_progress.stalled)

  @staticmethod
  def job_restarted_and_monitored(ctx: StatementContext) -> bool:
    return bool(ctx.restarted_job_id)

  @staticmethod
  def statement_status_failed(ctx: StatementContext) -> bool:
    return ctx.statement_status == StatementStatus.FAILED

  @staticmethod
  def failure_root_cause_identified(ctx: StatementContext) -> bool:
    return ctx.failure_analysis is not None

  @staticmethod
  def retryable_failure_detected(ctx: StatementContext) -> bool:
    return bool(ctx.failure_analysis and ctx.failure_analysis.retryable_failure)

  @staticmethod
  def job_recovery_initiated(ctx: StatementContext) -> bool:
    return ctx.recovery_result is not None and ctx.resume_point is not None

  @staticmethod
  def job_created(ctx: StatementContext) -> bool:
    return bool(ctx.statement_id)

  @staticmethod
  def transaction_data_loaded(ctx: StatementContext) -> bool:
    return ctx.transactions is not None

  @staticmethod
  def account_profile_retrieved(ctx: StatementContext) -> bool:
    return ctx.account_profile is not None

  @staticmethod
  def document_rendered(ctx: StatementContext) -> bool:
    return ctx.statement_document is not None

  @staticmethod
  def statement_stored_and_job_completed(ctx: StatementContext) -> bool:
    return bool(ctx.download_url and ctx.job_status == "completed")


async def retry_async(
    func,
    *args,
    max_attempts: int,
    backoff_ms: int,
    retry_on: List[str],
    on_retry_exhausted: str,
    **kwargs
):
  attempt = 0
  while True:
    attempt += 1
    try:
      return await func(*args, **kwargs)
    except Exception as exc:  # exact spec requires retry by labels provided
      exc_name = exc.__class__.__name__
      exc_text = str(exc)
      should_retry = exc_name in retry_on or exc_text in retry_on
      if not should_retry or attempt >= max_attempts:
        if on_retry_exhausted == "throw_ProfileServiceUnavailable":
          raise ProfileServiceUnavailable("Profile service unavailable after retries.") from exc
        if on_retry_exhausted == "throw_StorageUnavailable":
          raise StorageUnavailable("Storage unavailable after retries.") from exc
        raise RetryExhausted(f"Retry exhausted for {func.__name__}.") from exc
      await asyncio.sleep(backoff_ms / 1000.0)


@asynccontextmanager
async def transaction_boundary(name: str):
  logger.info("BEGIN TRANSACTION %s", name)
  try:
    yield
    logger.info("COMMIT TRANSACTION %s", name)
  except Exception:
    logger.info("ROLLBACK TRANSACTION %s", name)
    raise


# ============================================================
# Services
# ============================================================

class SecurityService:
  async def AuthenticateRequest(self, auth_token: str, ctx: StatementContext) -> None:
    # @meta preCondition: valid_jwt_format
    ConditionChecker.require(
        ConditionChecker.valid_jwt_format(auth_token),
        "JWT token format is invalid.",
        Unauthorized
    )

    # Assumption: in this self-contained demo, a syntactically valid JWT maps to a deterministic customer id.
    ctx.customer_id = str(uuid.uuid5(uuid.NAMESPACE_OID, auth_token))

    # @meta postCondition: customer_authenticated
    ConditionChecker.require(
        ConditionChecker.customer_authenticated(ctx),
        "Customer authentication failed.",
        Unauthorized
    )


class ValidationService:
  def __init__(self, db: InMemoryDatabase) -> None:
    self.db = db

  async def ValidateStatementRequest(
      self,
      account_id: str,
      statement_request: StatementRequest,
      idempotency_key: Optional[str],
      customer_id: str,
      ctx: StatementContext
  ) -> None:
    # @meta preCondition: customer_authenticated
    ConditionChecker.require(
        ConditionChecker.customer_authenticated(ctx),
        "Customer must be authenticated before request validation."
    )

    try:
      uuid.UUID(account_id)
    except Exception as exc:
      raise BadRequest("accountId must be a valid UUID.") from exc

    if not customer_id:
      raise Forbidden("Authenticated customer is required.")

    if statement_request.periodStart > statement_request.periodEnd:
      raise BadRequest("periodStart must not be after periodEnd.")

    if statement_request.format not in {"PDF"}:
      raise BadRequest("Unsupported statement format.")

    validated = ValidatedStatement(
        period_start=statement_request.periodStart,
        period_end=statement_request.periodEnd,
        requested_format=statement_request.format
    )

    ctx.validated_statement = validated
    ctx.account_id = account_id

    if idempotency_key:
      existing_statement_id = self.db.idempotency_index.get((account_id, idempotency_key))
      ctx.existing_statement_id = existing_statement_id

    # Assumption: account existence check is done here because the specification says validate parameters
    # and check idempotency, and lists NotFound as a possible exception.
    if account_id not in self.db.transactions:
      raise NotFound("Account not found.")

    # @meta postCondition: request_validated_and_idempotency_checked
    ConditionChecker.require(
        ConditionChecker.request_validated_and_idempotency_checked(ctx),
        "Request validation or idempotency check failed."
    )


class BusinessRuleService:
  def __init__(self, db: InMemoryDatabase) -> None:
    self.db = db

  async def EvaluateStatementStatus(self, account_id: str, validated_statement: ValidatedStatement, ctx: StatementContext) -> None:
    # Deterministic lookup based on existing statement/job state.
    if ctx.existing_statement_id and ctx.existing_statement_id in self.db.statement_jobs:
      status = self.db.statement_jobs[ctx.existing_statement_id]["status"]
      ctx.statement_status = StatementStatus(status)
      return

    for statement_id, job in self.db.statement_jobs.items():
      if (
          job["account_id"] == account_id
          and job["period_start"] == validated_statement.period_start.isoformat()
          and job["period_end"] == validated_statement.period_end.isoformat()
      ):
        ctx.existing_statement_id = statement_id
        ctx.statement_status = StatementStatus(job["status"])
        return

    ctx.statement_status = StatementStatus.NOT_FOUND

  async def AnalyzeFailureReason(self, existing_statement_id: str, ctx: StatementContext) -> None:
    # @meta preCondition: statement_status_failed
    ConditionChecker.require(
        ConditionChecker.statement_status_failed(ctx),
        "Statement status must be failed before failure analysis."
    )

    job = self.db.statement_jobs.get(existing_statement_id)
    if not job:
      raise AnalysisUnavailable("Failed job analysis unavailable because job does not exist.")

    reason = job.get("failure_reason", "unknown")
    ctx.failure_analysis = FailureAnalysis(
        retryable_failure=reason in {"timeout", "temporary_dependency_failure"},
        root_cause=reason
    )

    # @meta postCondition: failure_root_cause_identified
    ConditionChecker.require(
        ConditionChecker.failure_root_cause_identified(ctx),
        "Failure analysis was not produced."
    )

  async def RecoverFailedJob(self, existing_statement_id: str, ctx: StatementContext) -> None:
    # @meta preCondition: retryable_failure_detected
    ConditionChecker.require(
        ConditionChecker.retryable_failure_detected(ctx),
        "Failure must be retryable before recovery starts."
    )

    recovery_job_id = str(uuid.uuid4())
    ctx.recovery_result = RecoveryResult(recovery_started=True, recovery_job_id=recovery_job_id)
    ctx.resume_point = ResumePoint(checkpoint_id=f"checkpoint-{existing_statement_id}")

    job = self.db.statement_jobs.get(existing_statement_id)
    if job:
      job["status"] = "running"
      job["recovery_job_id"] = recovery_job_id

    # @meta postCondition: job_recovery_initiated
    ConditionChecker.require(
        ConditionChecker.job_recovery_initiated(ctx),
        "Job recovery initiation failed."
    )

  async def ComputeStatementTotals(
      self,
      transactions: List[Transaction],
      fee_schedule: FeeSchedule,
      preliminary_calculations: List[CalculationResult],
      ctx: StatementContext
  ) -> None:
    total_debits = sum(abs(t.amount) for t in transactions if t.amount < 0)
    total_credits = sum(t.amount for t in transactions if t.amount > 0)
    calculated_fees = fee_schedule.monthly_fee + sum(c.value for c in preliminary_calculations)
    net_total = total_credits - total_debits - calculated_fees
    ctx.statement_totals = StatementTotals(
        total_debits=round(total_debits, 2),
        total_credits=round(total_credits, 2),
        fees=round(calculated_fees, 2),
        net_total=round(net_total, 2),
    )


class RepositoryService:
  def __init__(self, db: InMemoryDatabase) -> None:
    self.db = db

  async def CheckJobProgress(self, existing_statement_id: str, ctx: StatementContext) -> None:
    # @meta preCondition: statement_status_running
    ConditionChecker.require(
        ConditionChecker.statement_status_running(ctx),
        "Statement status must be running before checking job progress."
    )

    progress = self.db.job_progress.get(existing_statement_id)
    if not progress:
      raise JobNotFound("Running job progress not found.")

    ctx.job_progress = progress

    # @meta postCondition: job_progress_evaluated
    ConditionChecker.require(
        ConditionChecker.job_progress_evaluated(ctx),
        "Job progress was not evaluated."
    )

  async def RestartJob(self, existing_statement_id: str, ctx: StatementContext) -> None:
    # @meta preCondition: job_stalled
    ConditionChecker.require(
        ConditionChecker.job_stalled(ctx),
        "Job must be stalled before restart."
    )

    restarted_job_id = str(uuid.uuid4())
    ctx.restarted_job_id = restarted_job_id

    if existing_statement_id not in self.db.statement_jobs:
      raise JobNotFound("Cannot restart missing job.")

    self.db.statement_jobs[existing_statement_id]["status"] = "running"
    self.db.statement_jobs[existing_statement_id]["restarted_job_id"] = restarted_job_id
    self.db.job_progress[existing_statement_id] = JobProgress(stalled=False, progress_percent=1)

    # @meta postCondition: job_restarted_and_monitored
    ConditionChecker.require(
        ConditionChecker.job_restarted_and_monitored(ctx),
        "Job restart did not complete as expected."
    )

  async def CreateNewStatementJob(self, account_id: str, validated_statement: ValidatedStatement, ctx: StatementContext) -> None:
    new_statement_id = str(uuid.uuid4())
    ctx.new_statement_id = new_statement_id

    self.db.statement_jobs[new_statement_id] = {
      "status": "running",
      "account_id": account_id,
      "period_start": validated_statement.period_start.isoformat(),
      "period_end": validated_statement.period_end.isoformat(),
    }
    self.db.job_progress[new_statement_id] = JobProgress(stalled=False, progress_percent=0)

  async def CreateStatementJob(self, account_id: str, validated_statement: ValidatedStatement, ctx: StatementContext) -> None:
    statement_id = str(uuid.uuid4())
    ctx.statement_id = statement_id

    async with transaction_boundary("CreateStatementJob"):
      self.db.statement_jobs[statement_id] = {
        "status": "running",
        "account_id": account_id,
        "period_start": validated_statement.period_start.isoformat(),
        "period_end": validated_statement.period_end.isoformat(),
      }
      self.db.job_progress[statement_id] = JobProgress(stalled=False, progress_percent=0)

  async def LoadTransactions(self, account_id: str, validated_statement: ValidatedStatement, ctx: StatementContext) -> None:
    # @meta preCondition: job_created
    ConditionChecker.require(
        ConditionChecker.job_created(ctx),
        "Job must be created before loading transactions."
    )

    transactions = self.db.transactions.get(account_id)
    if transactions is None:
      raise NotFound("Transactions not found for account.")

    # Assumption: this demo has no corrupt storage layer; the branch remains explicit.
    ctx.transactions = transactions

    # @meta postCondition: transaction_data_loaded
    ConditionChecker.require(
        ConditionChecker.transaction_data_loaded(ctx),
        "Transaction data was not loaded."
    )


class CacheService:
  def __init__(self, cache: InMemoryCache) -> None:
    self.cache = cache

  async def LoadFeesAndRates(self, validated_statement: ValidatedStatement, ctx: StatementContext) -> None:
    ctx.fee_schedule = self.cache.fee_schedule
    months = max(1, (validated_statement.period_end.month - validated_statement.period_start.month) + 1)
    ctx.preliminary_calculations = [
      CalculationResult(name="monthly_fee_component", value=self.cache.fee_schedule.monthly_fee * months * 0.1)
    ]


class ExternalService:
  def __init__(self, profile_client: ProfileClient, storage_client: ObjectStorageClient, db: InMemoryDatabase) -> None:
    self.profile_client = profile_client
    self.storage_client = storage_client
    self.db = db

  async def FetchAccountProfile(self, account_id: str, ctx: StatementContext) -> None:
    # @meta preCondition: job_created
    ConditionChecker.require(
        ConditionChecker.job_created(ctx),
        "Job must be created before fetching account profile."
    )

    profile = await retry_async(
        self.profile_client.fetch_profile,
        account_id,
        max_attempts=2,
        backoff_ms=400,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="throw_ProfileServiceUnavailable",
    )
    ctx.account_profile = profile

    # @meta postCondition: account_profile_retrieved
    ConditionChecker.require(
        ConditionChecker.account_profile_retrieved(ctx),
        "Account profile retrieval failed."
    )

  async def StoreStatement(self, statement_id: str, statement_document: bytes, ctx: StatementContext) -> None:
    # @meta preCondition: document_rendered
    ConditionChecker.require(
        ConditionChecker.document_rendered(ctx),
        "Document must be rendered before storage."
    )

    download_url = await retry_async(
        self.storage_client.store,
        statement_id,
        statement_document,
        max_attempts=3,
        backoff_ms=500,
        retry_on=["Timeout", "502", "503", "StorageUnavailable"],
        on_retry_exhausted="throw_StorageUnavailable",
    )

    ctx.download_url = download_url
    ctx.job_status = "completed"

    async with transaction_boundary("StoreStatement"):
      if statement_id not in self.db.statement_jobs:
        raise StorageUnavailable("Cannot update status for missing statement job.")
      self.db.statement_jobs[statement_id]["status"] = "completed"
      self.db.statement_jobs[statement_id]["download_url"] = download_url

    # @meta postCondition: statement_stored_and_job_completed
    ConditionChecker.require(
        ConditionChecker.statement_stored_and_job_completed(ctx),
        "Statement storage or job completion update failed."
    )


class PublisherService:
  def __init__(self, event_bus: EventBus) -> None:
    self.event_bus = event_bus

  async def PublishStatementReady(self, statement_id: str, download_url: str, ctx: StatementContext) -> None:
    published = await self.event_bus.publish(
        "StatementReady",
        {"statementId": statement_id, "downloadUrl": download_url}
    )
    if not published:
      raise EventPublishFailed("StatementReady event could not be published.")
    ctx.events["eventPublished"] = True


class MapperService:
  async def BuildCompletedResponse(self, existing_statement_id: str, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=existing_statement_id,
        status="completed",
        message="Existing completed statement accepted."
    )

  async def BuildRestartedJobResponse(self, restarted_job_id: str, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=restarted_job_id,
        status="running",
        message="Stalled statement job restarted."
    )

  async def BuildRunningResponse(self, existing_statement_id: str, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=existing_statement_id,
        status="running",
        message="Statement generation is still running."
    )

  async def BuildRecoveryResponse(self, recovery_result: RecoveryResult, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=recovery_result.recovery_job_id,
        status="running",
        message="Failed statement job recovery started."
    )

  async def BuildNewResponse(self, new_statement_id: str, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=new_statement_id,
        status="running",
        message="New statement job created for non-retryable failure."
    )

  async def LoadFormattingPreferences(self, account_profile: AccountProfile, ctx: StatementContext) -> None:
    preference = account_profile.formatting_preference or "STANDARD"
    # Assumption: deterministic mapping from profile preference to formatting config.
    if preference == "STANDARD":
      ctx.formatting_preferences = FormattingPreferences(
          date_format="YYYY-MM-DD",
          currency_format="#,##0.00",
          locale=account_profile.locale,
      )
    else:
      ctx.formatting_preferences = FormattingPreferences(
          date_format="DD/MM/YYYY",
          currency_format="#,##0.00",
          locale=account_profile.locale,
      )

  async def UseDefaultFormattingPreferences(self, ctx: StatementContext) -> None:
    ctx.formatting_preferences = FormattingPreferences(
        date_format="YYYY-MM-DD",
        currency_format="#,##0.00",
        locale="en_US",
    )

  async def RenderStatementDocument(
      self,
      account_profile: Optional[AccountProfile],
      formatting_preferences: FormattingPreferences,
      transactions: List[Transaction],
      statement_totals: StatementTotals,
      ctx: StatementContext
  ) -> None:
    customer_name = account_profile.customer_name if account_profile else "Unknown Customer"
    rendered_text = (
      f"Statement for {customer_name}\n"
      f"Locale: {formatting_preferences.locale}\n"
      f"Transactions: {len(transactions)}\n"
      f"Debits: {statement_totals.total_debits}\n"
      f"Credits: {statement_totals.total_credits}\n"
      f"Fees: {statement_totals.fees}\n"
      f"Net Total: {statement_totals.net_total}\n"
    )
    ctx.statement_document = rendered_text.encode("utf-8")

  async def BuildStatementResponse(self, statement_id: str, download_url: str, ctx: StatementContext) -> None:
    ctx.response = StatementAcceptedResponse(
        statementId=statement_id,
        status="completed",
        downloadUrl=download_url,
        message="Statement generation accepted."
    )


# ============================================================
# FlowJoin helpers
# ============================================================

async def FlowJoinGenerateStatement() -> None:
  return


async def FlowJoinJobStalled() -> None:
  await FlowJoinGenerateStatement()


async def FlowJoinRetryableFailure() -> None:
  await FlowJoinGenerateStatement()


async def FlowJoinProfileResolved() -> None:
  return


# ============================================================
# Application assembly
# ============================================================

db = InMemoryDatabase()
db.seed()
cache = InMemoryCache()
event_bus = EventBus()
profile_client = ProfileClient()
storage_client = ObjectStorageClient()

security_service = SecurityService()
validation_service = ValidationService(db)
business_rule_service = BusinessRuleService(db)
repository_service = RepositoryService(db)
cache_service = CacheService(cache)
external_service = ExternalService(profile_client, storage_client, db)
publisher_service = PublisherService(event_bus)
mapper_service = MapperService()

app = FastAPI(title="Statement API", version="1.0.0")


# ============================================================
# Exception mapping
# ============================================================

@app.exception_handler(AppException)
async def app_exception_handler(_, exc: AppException):
  raise HTTPException(
      status_code=exc.status_code,
      detail=ErrorResponse(error=exc.error_code, message=exc.message).model_dump()
  )


# ============================================================
# Controller / REST endpoint
# REST DEFINITION @meta:
# {"endpoint": "POST /accounts/{accountId}/statements",
#  "responseSuccess": "202 Accepted",
#  "responseError": "400|401|403|404|409|422"}
# ============================================================

@app.post(
    "/accounts/{accountId}/statements",
    response_model=StatementAcceptedResponse,
    status_code=202,
    responses={
      400: {"model": ErrorResponse},
      401: {"model": ErrorResponse},
      403: {"model": ErrorResponse},
      404: {"model": ErrorResponse},
      409: {"model": ErrorResponse},
      422: {"model": ErrorResponse},
    },
)
async def main_Generatemonthlystatement(
    accountId: str = Path(..., alias="accountId"),
    statementRequest: StatementRequest = Body(..., alias="statementRequest"),
    authorization: str = Header(..., alias="Authorization"),
    idempotencyKey: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> StatementAcceptedResponse:
  ctx = StatementContext()

  try:
    # 1) AuthenticateRequest()
    await security_service.AuthenticateRequest(
        auth_token=authorization.replace("Bearer ", ""),
        ctx=ctx
    )

    # 2) ValidateStatementRequest()
    await validation_service.ValidateStatementRequest(
        account_id=accountId,
        statement_request=statementRequest,
        idempotency_key=idempotencyKey,
        customer_id=ctx.customer_id,
        ctx=ctx
    )

    # 3) EvaluateStatementStatus()
    await business_rule_service.EvaluateStatementStatus(
        account_id=ctx.account_id,
        validated_statement=ctx.validated_statement,
        ctx=ctx
    )

    # 4) Decision(statementStatus)
    if ctx.statement_status == StatementStatus.COMPLETED:
      # if completed:
      #   BuildCompletedResponse()
      await mapper_service.BuildCompletedResponse(
          existing_statement_id=ctx.existing_statement_id,
          ctx=ctx
      )
      #   FlowJoinGenerateStatement()
      await FlowJoinGenerateStatement()

    elif ctx.statement_status == StatementStatus.RUNNING:
      # if running:
      #   CheckJobProgress()
      await repository_service.CheckJobProgress(
          existing_statement_id=ctx.existing_statement_id,
          ctx=ctx
      )

      #   Decision(jobStalled)
      jobStalled = bool(ctx.job_progress and ctx.job_progress.stalled)

      if jobStalled:
        # if yes:
        #   RestartJob()
        await repository_service.RestartJob(
            existing_statement_id=ctx.existing_statement_id,
            ctx=ctx
        )
        #   BuildRestartedJobResponse()
        await mapper_service.BuildRestartedJobResponse(
            restarted_job_id=ctx.restarted_job_id,
            ctx=ctx
        )
        #   FlowJoinJobStalled()
        await FlowJoinJobStalled()
      else:
        # if no:
        #   BuildRunningResponse()
        await mapper_service.BuildRunningResponse(
            existing_statement_id=ctx.existing_statement_id,
            ctx=ctx
        )
        #   FlowJoinJobStalled()
        await FlowJoinJobStalled()

    elif ctx.statement_status == StatementStatus.FAILED:
      # if failed:
      #   AnalyzeFailureReason()
      await business_rule_service.AnalyzeFailureReason(
          existing_statement_id=ctx.existing_statement_id,
          ctx=ctx
      )

      #   Decision(retryableFailure)
      retryableFailure = bool(ctx.failure_analysis and ctx.failure_analysis.retryable_failure)

      if retryableFailure:
        # if yes:
        #   RecoverFailedJob()
        await business_rule_service.RecoverFailedJob(
            existing_statement_id=ctx.existing_statement_id,
            ctx=ctx
        )
        #   BuildRecoveryResponse()
        await mapper_service.BuildRecoveryResponse(
            recovery_result=ctx.recovery_result,
            ctx=ctx
        )
        #   FlowJoinRetryableFailure()
        await FlowJoinRetryableFailure()
      else:
        # if no:
        #   CreateNewStatementJob()
        await repository_service.CreateNewStatementJob(
            account_id=ctx.account_id,
            validated_statement=ctx.validated_statement,
            ctx=ctx
        )
        #   BuildNewResponse()
        await mapper_service.BuildNewResponse(
            new_statement_id=ctx.new_statement_id,
            ctx=ctx
        )
        #   FlowJoinRetryableFailure()
        await FlowJoinRetryableFailure()

    elif ctx.statement_status == StatementStatus.NOT_FOUND:
      # if not_found:
      #   CreateStatementJob()
      await repository_service.CreateStatementJob(
          account_id=ctx.account_id,
          validated_statement=ctx.validated_statement,
          ctx=ctx
      )

      #   Fork(UaauxTmGAqAKCQv1)
      async def parallel_LoadTransactions() -> None:
        # parallel LoadTransactions:
        #   LoadTransactions()
        await repository_service.LoadTransactions(
            account_id=ctx.account_id,
            validated_statement=ctx.validated_statement,
            ctx=ctx
        )

      async def parallel_LoadFeesAndRates() -> None:
        # parallel LoadFeesAndRates:
        #   LoadFeesAndRates()
        await cache_service.LoadFeesAndRates(
            validated_statement=ctx.validated_statement,
            ctx=ctx
        )

      async def parallel_FetchAccountProfile() -> None:
        # parallel FetchAccountProfile:
        #   FetchAccountProfile()
        await external_service.FetchAccountProfile(
            account_id=ctx.account_id,
            ctx=ctx
        )

        #   Decision(profileAvailable)
        profileAvailable = ctx.account_profile is not None

        if profileAvailable:
          # if yes:
          #   LoadFormattingPreferences()
          await mapper_service.LoadFormattingPreferences(
              account_profile=ctx.account_profile,
              ctx=ctx
          )
          #   FlowJoinProfileResolved()
          await FlowJoinProfileResolved()
        else:
          # if no:
          #   UseDefaultFormattingPreferences()
          await mapper_service.UseDefaultFormattingPreferences(ctx=ctx)
          #   FlowJoinProfileResolved()
          await FlowJoinProfileResolved()

      #   Join(IyduxTmGAqAKCQ1h)
      await asyncio.gather(
          parallel_LoadTransactions(),
          parallel_LoadFeesAndRates(),
          parallel_FetchAccountProfile(),
      )

      #   ComputeStatementTotals()
      await business_rule_service.ComputeStatementTotals(
          transactions=ctx.transactions,
          fee_schedule=ctx.fee_schedule,
          preliminary_calculations=ctx.preliminary_calculations,
          ctx=ctx
      )

      #   RenderStatementDocument()
      await mapper_service.RenderStatementDocument(
          account_profile=ctx.account_profile,
          formatting_preferences=ctx.formatting_preferences,
          transactions=ctx.transactions,
          statement_totals=ctx.statement_totals,
          ctx=ctx
      )

      #   StoreStatement()
      await external_service.StoreStatement(
          statement_id=ctx.statement_id,
          statement_document=ctx.statement_document,
          ctx=ctx
      )

      #   PublishStatementReady()
      await publisher_service.PublishStatementReady(
          statement_id=ctx.statement_id,
          download_url=ctx.download_url,
          ctx=ctx
      )

      #   BuildStatementResponse()
      await mapper_service.BuildStatementResponse(
          statement_id=ctx.statement_id,
          download_url=ctx.download_url,
          ctx=ctx
      )

      #   FlowJoinGenerateStatement()
      await FlowJoinGenerateStatement()

    else:
      raise Conflict("Unhandled statement status.")

    if ctx.response is None:
      raise PostConditionFailed("Response was not built.")

    return ctx.response

  except AppException:
    raise
  except HTTPException:
    raise
  except Exception as exc:
    logger.exception("Unhandled exception during statement generation flow.")
    raise HTTPException(
        status_code=500,
        detail=ErrorResponse(
            error="internal_error",
            message=f"Unhandled error: {str(exc)}"
        ).model_dump()
    ) from exc


# ============================================================
# Optional local runner note
# ============================================================
# Run with:
#   uvicorn this_file_name:app --reload
#
# Example request body:
# {
#   "statementRequest": {
#     "periodStart": "2026-02-01",
#     "periodEnd": "2026-02-28",
#     "format": "PDF"
#   }
# }
#
# Required headers:
#   Authorization: Bearer xxx.yyy.zzz
#   Idempotency-Key: any-string
