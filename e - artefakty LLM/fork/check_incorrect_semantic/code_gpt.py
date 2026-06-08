from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Path, Response, status
from pydantic import BaseModel, Field


# ============================================================
# Exceptions (@meta.exceptions + condition enforcement)
# ============================================================

class DomainException(Exception):
  pass


class Unauthorized(DomainException):
  pass


class Forbidden(DomainException):
  pass


class BadRequest(DomainException):
  pass


class Conflict(DomainException):
  pass


class NotFound(DomainException):
  pass


class JobNotFound(DomainException):
  pass


class AnalysisUnavailable(DomainException):
  pass


class ProfileServiceUnavailable(DomainException):
  pass


class StorageUnavailable(DomainException):
  pass


class EventPublishFailed(DomainException):
  pass


class PreConditionFailed(DomainException):
  pass


class PostConditionFailed(DomainException):
  pass


class TimeoutException(DomainException):
  pass


# ============================================================
# DTOs / Models
# ============================================================

class StatementRequest(BaseModel):
  periodStart: date
  periodEnd: date
  outputFormat: str = Field(default="PDF")


class ValidatedStatement(BaseModel):
  periodStart: date
  periodEnd: date
  outputFormat: str


class JobProgress(BaseModel):
  stalled: bool
  percent: int


class FailureAnalysis(BaseModel):
  retryable: bool
  rootCause: str


class RecoveryResult(BaseModel):
  recoveryJobId: UUID
  status: str


class ResumePoint(BaseModel):
  checkpointId: str


class Transaction(BaseModel):
  txId: UUID
  amount: float
  currency: str


class FeeSchedule(BaseModel):
  monthlyFee: float
  fxMarkupPercent: float


class CalculationResult(BaseModel):
  key: str
  value: float


class AccountProfile(BaseModel):
  accountId: UUID
  customerName: str
  locale: str
  timezone: str
  hasFormattingPreferences: bool = True
  preferredDateFormat: Optional[str] = None


class FormattingPreferences(BaseModel):
  dateFormat: str
  currencyFormat: str


class StatementTotals(BaseModel):
  gross: float
  fees: float
  net: float


class StatementAcceptedResponse(BaseModel):
  statementId: UUID
  status: str
  downloadUrl: Optional[str] = None


class StatementStatus(str, Enum):
  completed = "completed"
  running = "running"
  failed = "failed"
  not_found = "not_found"


@dataclass
class StatementContext:
  customerId: Optional[UUID] = None
  validatedStatement: Optional[ValidatedStatement] = None
  existingStatementId: Optional[UUID] = None
  accountId: Optional[UUID] = None
  statementStatus: Optional[StatementStatus] = None
  response: Optional[StatementAcceptedResponse] = None

  jobProgress: Optional[JobProgress] = None
  restartedJobId: Optional[UUID] = None

  failureAnalysis: Optional[FailureAnalysis] = None
  recoveryResult: Optional[RecoveryResult] = None
  resumePoint: Optional[ResumePoint] = None
  newStatementId: Optional[UUID] = None

  statementId: Optional[UUID] = None
  transactions: List[Transaction] = field(default_factory=list)
  feeSchedule: Optional[FeeSchedule] = None
  preliminaryCalculations: List[CalculationResult] = field(default_factory=list)
  accountProfile: Optional[AccountProfile] = None
  formattingPreferences: Optional[FormattingPreferences] = None
  statementTotals: Optional[StatementTotals] = None
  statementDocument: Optional[bytes] = None
  downloadUrl: Optional[str] = None
  jobStatus: Optional[str] = None
  eventPublished: Optional[bool] = None


# ============================================================
# Helpers
# ============================================================

def ensure_pre(condition: bool, message: str) -> None:
  if not condition:
    raise PreConditionFailed(message)


def ensure_post(condition: bool, message: str) -> None:
  if not condition:
    raise PostConditionFailed(message)


@contextmanager
def transaction_boundary(name: str):
  try:
    yield
  except Exception:
    raise


class RetryExecutor:
  async def run(
      self,
      fn,
      *,
      max_attempts: int,
      backoff_ms: int,
      retry_on: List[str],
      on_retry_exhausted: str,
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
        if on_retry_exhausted == "throw_ProfileServiceUnavailable":
          raise ProfileServiceUnavailable("Profile service unavailable after retries.") from exc
        if on_retry_exhausted == "throw_StorageUnavailable":
          raise StorageUnavailable("Storage unavailable after retries.") from exc
        raise

  @staticmethod
  def _label(exc: Exception) -> str:
    s = str(exc)
    if s in {"Timeout", "502", "503", "StorageUnavailable"}:
      return s
    if isinstance(exc, TimeoutException):
      return "Timeout"
    if isinstance(exc, StorageUnavailable):
      return "StorageUnavailable"
    return exc.__class__.__name__


# ============================================================
# Infrastructure (in-memory demo)
# ============================================================

class StatementRepo:
  def __init__(self):
    self.statements: Dict[UUID, Dict[str, Any]] = {}
    self.idempotency: Dict[Tuple[UUID, str, date, date], UUID] = {}
    self.jobs: Dict[UUID, Dict[str, Any]] = {}

  def find_existing_statement(self, account_id: UUID, req: StatementRequest) -> Optional[UUID]:
    return self.idempotency.get((account_id, req.outputFormat, req.periodStart, req.periodEnd))

  def create_statement_job(self, account_id: UUID, validated: ValidatedStatement) -> UUID:
    statement_id = uuid4()
    self.statements[statement_id] = {
      "accountId": account_id,
      "status": "running",
      "validated": validated.model_dump(),
      "downloadUrl": None,
    }
    self.jobs[statement_id] = {"stalled": False, "percent": 0}
    return statement_id

  def update_statement_completed(self, statement_id: UUID, download_url: str):
    if statement_id not in self.statements:
      raise NotFound("Statement not found.")
    self.statements[statement_id]["status"] = "completed"
    self.statements[statement_id]["downloadUrl"] = download_url
    self.jobs[statement_id] = {"stalled": False, "percent": 100}

  def get_status(self, statement_id: UUID) -> str:
    if statement_id not in self.statements:
      raise NotFound("Statement not found.")
    return self.statements[statement_id]["status"]

  def get_job_progress(self, statement_id: UUID) -> JobProgress:
    if statement_id not in self.jobs:
      raise JobNotFound("Job not found.")
    data = self.jobs[statement_id]
    return JobProgress(stalled=data["stalled"], percent=data["percent"])

  def restart_job(self, statement_id: UUID) -> UUID:
    if statement_id not in self.jobs:
      raise JobNotFound("Job not found.")
    new_job_id = uuid4()
    self.jobs[statement_id] = {"stalled": False, "percent": 5}
    return new_job_id


class EventBus:
  def publish(self, name: str, payload: Dict[str, Any]) -> bool:
    if name == "StatementReady" and "statementId" in payload:
      return True
    raise EventPublishFailed("Failed to publish event.")


class ObjectStorage:
  async def store(self, statement_id: UUID, data: bytes) -> str:
    if not data:
      raise StorageUnavailable("Empty document.")
    await asyncio.sleep(0.01)
    return f"https://storage.example/statements/{statement_id}.pdf"


# ============================================================
# Services (one method per action)
# ============================================================

class SecurityService:
  def AuthenticateRequest(self, authToken: Optional[str]) -> UUID:
    ensure_pre(authToken is not None and authToken.startswith("Bearer "), "valid_jwt_format")
    token = authToken.removeprefix("Bearer ").strip()
    try:
      customer_id = UUID(token)
    except ValueError as exc:
      raise Unauthorized("Invalid JWT token.") from exc
    # Assumption: basic forbidden rule for demo.
    if customer_id.int % 17 == 0:
      raise Forbidden("Customer is forbidden.")
    ensure_post(customer_id is not None, "customer_authenticated")
    return customer_id


class ValidationService:
  def __init__(self, repo: StatementRepo):
    self.repo = repo

  def ValidateStatementRequest(
      self,
      accountId: UUID,
      statementRequest: StatementRequest,
      idempotencyKey: str,
      customerId: UUID,
  ) -> Tuple[ValidatedStatement, Optional[UUID], UUID]:
    ensure_pre(customerId is not None, "customer_authenticated")
    if statementRequest.periodStart > statementRequest.periodEnd:
      raise BadRequest("Invalid period range.")
    if not idempotencyKey.strip():
      raise BadRequest("Idempotency key is required.")

    validated = ValidatedStatement(
        periodStart=statementRequest.periodStart,
        periodEnd=statementRequest.periodEnd,
        outputFormat=statementRequest.outputFormat,
    )
    existing = self.repo.find_existing_statement(accountId, statementRequest)

    # Assumption: Conflict check is mapped to malformed duplicate idempotency scenario.
    if existing is None and idempotencyKey.lower() == "conflict":
      raise Conflict("Idempotency conflict detected.")

    ensure_post(True, "request_validated_and_idempotency_checked")
    return validated, existing, accountId


class StatementBusinessService:
  def __init__(self, repo: StatementRepo):
    self.repo = repo

  def EvaluateStatementStatus(self, accountId: UUID, validatedStatement: ValidatedStatement, existingStatementId: Optional[UUID]) -> StatementStatus:
    if existingStatementId is None:
      return StatementStatus.not_found
    status_value = self.repo.get_status(existingStatementId)
    if status_value == "completed":
      return StatementStatus.completed
    if status_value == "running":
      return StatementStatus.running
    if status_value == "failed":
      return StatementStatus.failed
    raise Conflict("Unknown statement status.")

  def AnalyzeFailureReason(self, existingStatementId: UUID) -> FailureAnalysis:
    ensure_pre(existingStatementId is not None, "statement_status_failed")
    # Assumption: deterministic retryable evaluation by UUID parity.
    retryable = existingStatementId.int % 2 == 0
    analysis = FailureAnalysis(retryable=retryable, rootCause="Upstream timeout")
    ensure_post(bool(analysis.rootCause), "failure_root_cause_identified")
    return analysis

  def RecoverFailedJob(self, existingStatementId: UUID) -> Tuple[RecoveryResult, ResumePoint]:
    ensure_pre(existingStatementId is not None, "retryable_failure_detected")
    result = RecoveryResult(recoveryJobId=uuid4(), status="RECOVERY_STARTED")
    resume = ResumePoint(checkpointId=f"chk-{existingStatementId.hex[:8]}")
    ensure_post(result.status == "RECOVERY_STARTED", "job_recovery_initiated")
    return result, resume

  def ComputeStatementTotals(
      self,
      transactions: List[Transaction],
      feeSchedule: FeeSchedule,
      preliminaryCalculations: List[CalculationResult],
  ) -> StatementTotals:
    gross = sum(t.amount for t in transactions) + sum(c.value for c in preliminaryCalculations)
    fees = feeSchedule.monthlyFee + (abs(gross) * feeSchedule.fxMarkupPercent / 100.0)
    net = gross - fees
    return StatementTotals(gross=gross, fees=fees, net=net)


class StatementRepositoryService:
  def __init__(self, repo: StatementRepo):
    self.repo = repo

  def CheckJobProgress(self, existingStatementId: UUID) -> JobProgress:
    ensure_pre(existingStatementId is not None, "statement_status_running")
    progress = self.repo.get_job_progress(existingStatementId)
    ensure_post(progress.percent >= 0, "job_progress_evaluated")
    return progress

  def RestartJob(self, existingStatementId: UUID) -> UUID:
    ensure_pre(existingStatementId is not None, "job_stalled")
    restarted_id = self.repo.restart_job(existingStatementId)
    ensure_post(restarted_id is not None, "job_restarted_and_monitored")
    return restarted_id

  def CreateNewStatementJob(self, accountId: UUID, validatedStatement: ValidatedStatement) -> UUID:
    return self.repo.create_statement_job(accountId, validatedStatement)

  def CreateStatementJob(self, accountId: UUID, validatedStatement: ValidatedStatement) -> UUID:
    return self.repo.create_statement_job(accountId, validatedStatement)

  def LoadTransactions(self, accountId: UUID, validatedStatement: ValidatedStatement) -> List[Transaction]:
    ensure_pre(accountId is not None, "job_created")
    txs = [
      Transaction(txId=uuid4(), amount=120.0, currency="USD"),
      Transaction(txId=uuid4(), amount=-20.0, currency="USD"),
      Transaction(txId=uuid4(), amount=80.0, currency="USD"),
    ]
    ensure_post(len(txs) > 0, "transaction_data_loaded")
    return txs


class CacheService:
  def LoadFeesAndRates(self, validatedStatement: ValidatedStatement, transactions: List[Transaction]) -> Tuple[FeeSchedule, List[CalculationResult]]:
    # Assumption: parallel branch starts immediately but may wait until transactions become available.
    fee_schedule = FeeSchedule(monthlyFee=5.0, fxMarkupPercent=1.2)
    prelim = [CalculationResult(key="tx_count_bonus", value=float(len(transactions)) * 0.1)]
    return fee_schedule, prelim


class ExternalProfileService:
  def __init__(self, retry: RetryExecutor):
    self.retry = retry

  async def FetchAccountProfile(self, accountId: UUID) -> AccountProfile:
    async def _call():
      await asyncio.sleep(0.01)
      return AccountProfile(
          accountId=accountId,
          customerName="Customer",
          locale="en-US",
          timezone="UTC",
          hasFormattingPreferences=True,
          preferredDateFormat="YYYY-MM-DD",
      )

    profile = await self.retry.run(
        _call,
        max_attempts=2,
        backoff_ms=400,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="throw_ProfileServiceUnavailable",
    )
    ensure_pre(accountId is not None, "job_created")
    ensure_post(profile is not None, "account_profile_retrieved")
    return profile


class MapperService:
  def BuildCompletedResponse(self, existingStatementId: UUID) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=existingStatementId, status="COMPLETED")

  def BuildRestartedJobResponse(self, restartedJobId: UUID) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=restartedJobId, status="RESTARTED")

  def BuildRunningResponse(self, existingStatementId: UUID) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=existingStatementId, status="RUNNING")

  def BuildRecoveryResponse(self, recoveryResult: RecoveryResult) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=recoveryResult.recoveryJobId, status=recoveryResult.status)

  def BuildNewResponse(self, newStatementId: UUID) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=newStatementId, status="CREATED")

  def LoadFormattingPreferences(self, accountProfile: AccountProfile) -> FormattingPreferences:
    return FormattingPreferences(
        dateFormat=accountProfile.preferredDateFormat or "YYYY-MM-DD",
        currencyFormat="USD-2DP",
    )

  def UseDefaultFormattingPreferences(self) -> FormattingPreferences:
    return FormattingPreferences(dateFormat="YYYY-MM-DD", currencyFormat="USD-2DP")

  def RenderStatementDocument(
      self,
      accountProfile: AccountProfile,
      formattingPreferences: FormattingPreferences,
      transactions: List[Transaction],
      statementTotals: StatementTotals,
  ) -> bytes:
    ensure_pre(accountProfile is not None and formattingPreferences is not None, "document_render_inputs")
    content = (
      f"Statement for {accountProfile.customerName}\n"
      f"DateFormat={formattingPreferences.dateFormat}\n"
      f"Transactions={len(transactions)}\n"
      f"Gross={statementTotals.gross:.2f} Fees={statementTotals.fees:.2f} Net={statementTotals.net:.2f}\n"
    )
    return content.encode("utf-8")

  def BuildStatementResponse(self, statementId: UUID, downloadUrl: str) -> StatementAcceptedResponse:
    return StatementAcceptedResponse(statementId=statementId, status="READY", downloadUrl=downloadUrl)


class StorageService:
  def __init__(self, storage: ObjectStorage, repo: StatementRepo, retry: RetryExecutor):
    self.storage = storage
    self.repo = repo
    self.retry = retry

  async def StoreStatement(self, statementId: UUID, statementDocument: bytes) -> Tuple[str, str]:
    ensure_pre(statementDocument is not None and len(statementDocument) > 0, "document_rendered")

    async def _store():
      url = await self.storage.store(statementId, statementDocument)
      self.repo.update_statement_completed(statementId, url)
      return url

    download_url = await self.retry.run(
        _store,
        max_attempts=3,
        backoff_ms=500,
        retry_on=["Timeout", "502", "503", "StorageUnavailable"],
        on_retry_exhausted="throw_StorageUnavailable",
    )
    job_status = "COMPLETED"
    ensure_post(bool(download_url) and job_status == "COMPLETED", "statement_stored_and_job_completed")
    return download_url, job_status


class PublisherService:
  def __init__(self, bus: EventBus):
    self.bus = bus

  def PublishStatementReady(self, statementId: UUID, downloadUrl: str) -> bool:
    ok = self.bus.publish("StatementReady", {"statementId": str(statementId), "downloadUrl": downloadUrl})
    if not ok:
      raise EventPublishFailed("Event publish failed.")
    return ok


# ============================================================
# FlowJoin functions
# ============================================================

def FlowJoinGenerateStatement() -> None:
  return


def FlowJoinJobStalled() -> None:
  FlowJoinGenerateStatement()


def FlowJoinRetryableFailure() -> None:
  FlowJoinGenerateStatement()


def FlowJoinProfileResolved() -> None:
  return


# ============================================================
# App + wiring
# ============================================================

app = FastAPI(title="Statement Generation API", version="1.0.0")

repo = StatementRepo()
retry_executor = RetryExecutor()
security_service = SecurityService()
validation_service = ValidationService(repo)
business_service = StatementBusinessService(repo)
repo_service = StatementRepositoryService(repo)
cache_service = CacheService()
profile_service = ExternalProfileService(retry_executor)
mapper_service = MapperService()
storage_service = StorageService(ObjectStorage(), repo, retry_executor)
publisher_service = PublisherService(EventBus())


# ============================================================
# Controller
# REST DEFINITION @meta:
# POST /accounts/{accountId}/statements
# success: 202 Accepted
# errors: 400|401|403|404|409|422
# ============================================================

@app.post("/accounts/{accountId}/statements", status_code=status.HTTP_202_ACCEPTED)
async def main_Generatemonthlystatement(
    statementRequest: StatementRequest,
    accountId: UUID = Path(...),
    authToken: Optional[str] = Header(default=None, alias="Authorization"),
    idempotencyKey: str = Header(default="", alias="Idempotency-Key"),
):
  ctx = StatementContext()

  try:
    # AuthenticateRequest()
    ctx.customerId = security_service.AuthenticateRequest(authToken=authToken)

    # ValidateStatementRequest()
    validated, existing_statement_id, out_account_id = validation_service.ValidateStatementRequest(
        accountId=accountId,
        statementRequest=statementRequest,
        idempotencyKey=idempotencyKey,
        customerId=ctx.customerId,
    )
    ctx.validatedStatement = validated
    ctx.existingStatementId = existing_statement_id
    ctx.accountId = out_account_id

    # EvaluateStatementStatus()
    ctx.statementStatus = business_service.EvaluateStatementStatus(
        accountId=ctx.accountId,
        validatedStatement=ctx.validatedStatement,
        existingStatementId=ctx.existingStatementId,
    )

    # Decision(statementStatus)
    if ctx.statementStatus == StatementStatus.completed:
      # if completed:
      ctx.response = mapper_service.BuildCompletedResponse(existingStatementId=ctx.existingStatementId)
      FlowJoinGenerateStatement()
      return ctx.response

    if ctx.statementStatus == StatementStatus.running:
      # if running:
      ctx.jobProgress = repo_service.CheckJobProgress(existingStatementId=ctx.existingStatementId)

      # Decision(jobStalled)
      jobStalled = ctx.jobProgress.stalled
      if jobStalled:
        # if yes:
        ctx.restartedJobId = repo_service.RestartJob(existingStatementId=ctx.existingStatementId)
        ctx.response = mapper_service.BuildRestartedJobResponse(restartedJobId=ctx.restartedJobId)
        FlowJoinJobStalled()
        return ctx.response
      else:
        # if no:
        ctx.response = mapper_service.BuildRunningResponse(existingStatementId=ctx.existingStatementId)
        FlowJoinJobStalled()
        return ctx.response

    if ctx.statementStatus == StatementStatus.failed:
      # if failed:
      ctx.failureAnalysis = business_service.AnalyzeFailureReason(existingStatementId=ctx.existingStatementId)

      # Decision(retryableFailure)
      retryableFailure = ctx.failureAnalysis.retryable
      if retryableFailure:
        # if yes:
        recovery_result, resume_point = business_service.RecoverFailedJob(existingStatementId=ctx.existingStatementId)
        ctx.recoveryResult = recovery_result
        ctx.resumePoint = resume_point
        ctx.response = mapper_service.BuildRecoveryResponse(recoveryResult=ctx.recoveryResult)
        FlowJoinRetryableFailure()
        return ctx.response
      else:
        # if no:
        ctx.newStatementId = repo_service.CreateNewStatementJob(
            accountId=ctx.accountId, validatedStatement=ctx.validatedStatement
        )
        ctx.response = mapper_service.BuildNewResponse(newStatementId=ctx.newStatementId)
        FlowJoinRetryableFailure()
        return ctx.response

    if ctx.statementStatus == StatementStatus.not_found:
      # if not_found:
      ctx.statementId = repo_service.CreateStatementJob(
          accountId=ctx.accountId, validatedStatement=ctx.validatedStatement
      )

      # Fork(UaauxTmGAqAKCQv1) with real concurrency
      tx_ready_event = asyncio.Event()

      async def branch_LoadTransactions():
        ctx.transactions = repo_service.LoadTransactions(
            accountId=ctx.accountId, validatedStatement=ctx.validatedStatement
        )
        tx_ready_event.set()

      async def branch_LoadFeesAndRates():
        # Assumption: branch waits for transactions while remaining concurrently scheduled.
        await tx_ready_event.wait()
        fee_schedule, prelim = cache_service.LoadFeesAndRates(
            validatedStatement=ctx.validatedStatement,
            transactions=ctx.transactions,
        )
        ctx.feeSchedule = fee_schedule
        ctx.preliminaryCalculations = prelim

      async def branch_FetchAccountProfile():
        ctx.accountProfile = await profile_service.FetchAccountProfile(accountId=ctx.accountId)

        # Decision(profileAvailable)
        profileAvailable = bool(ctx.accountProfile and ctx.accountProfile.hasFormattingPreferences)
        if profileAvailable:
          # if yes:
          ctx.formattingPreferences = mapper_service.LoadFormattingPreferences(accountProfile=ctx.accountProfile)
          FlowJoinProfileResolved()
        else:
          # if no:
          ctx.formattingPreferences = mapper_service.UseDefaultFormattingPreferences()
          FlowJoinProfileResolved()

      # Join(IyduxTmGAqAKCQ1h)
      await asyncio.gather(
          branch_LoadTransactions(),
          branch_LoadFeesAndRates(),
          branch_FetchAccountProfile(),
      )

      # ComputeStatementTotals()
      ctx.statementTotals = business_service.ComputeStatementTotals(
          transactions=ctx.transactions,
          feeSchedule=ctx.feeSchedule,
          preliminaryCalculations=ctx.preliminaryCalculations,
      )

      # RenderStatementDocument()
      ctx.statementDocument = mapper_service.RenderStatementDocument(
          accountProfile=ctx.accountProfile,
          formattingPreferences=ctx.formattingPreferences,
          transactions=ctx.transactions,
          statementTotals=ctx.statementTotals,
      )

      # StoreStatement()
      ctx.downloadUrl, ctx.jobStatus = await storage_service.StoreStatement(
          statementId=ctx.statementId,
          statementDocument=ctx.statementDocument,
      )

      # PublishStatementReady()
      ctx.eventPublished = publisher_service.PublishStatementReady(
          statementId=ctx.statementId, downloadUrl=ctx.downloadUrl
      )

      # BuildStatementResponse()
      ctx.response = mapper_service.BuildStatementResponse(
          statementId=ctx.statementId, downloadUrl=ctx.downloadUrl
      )

      FlowJoinGenerateStatement()
      return ctx.response

    raise Conflict("Unsupported statement status branch.")

  except (BadRequest, PreConditionFailed) as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  except Unauthorized as exc:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
  except Forbidden as exc:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
  except (NotFound, JobNotFound) as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
  except Conflict as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
  except (
      AnalysisUnavailable,
      ProfileServiceUnavailable,
      StorageUnavailable,
      EventPublishFailed,
      PostConditionFailed,
  ) as exc:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
