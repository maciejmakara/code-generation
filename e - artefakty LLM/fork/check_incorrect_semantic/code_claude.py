"""
generate_monthly_statement.py
------------------------------
Production-quality Python implementation of the GenerateMonthlyStatement
endpoint derived from the UML Activity Diagram specification.

Components generated:
  - DTOs / data models
  - Custom exception classes
  - Retry helper
  - Audit helper
  - StatementService  (all internal actions)
  - StatementController  (REST endpoint orchestrator)

Framework: FastAPI (async, fits asyncio.gather for Fork/Join concurrency)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from fastapi import FastAPI, Header, Path, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ===========================================================================
# DTOs / Request & Response Models
# ===========================================================================

class StatementRequest(BaseModel):
  period_start: str          # ISO date, e.g. "2024-01-01"
  period_end: str            # ISO date, e.g. "2024-01-31"
  format: str = "PDF"        # output format preference


class StatementAcceptedResponse(BaseModel):
  statement_id: Optional[str] = None
  download_url: Optional[str] = None
  status: str = "ACCEPTED"
  message: str = ""


# ---------------------------------------------------------------------------
# Internal domain types  (simplified dataclasses for demo)
# ---------------------------------------------------------------------------

@dataclass
class ValidatedStatement:
  account_id: UUID
  period_start: str
  period_end: str
  format: str
  idempotency_key: str


@dataclass
class JobProgress:
  job_id: UUID
  stalled: bool
  progress_pct: int


@dataclass
class FailureAnalysis:
  root_cause: str
  retryable: bool


@dataclass
class RecoveryResult:
  recovered_job_id: UUID
  strategy: str


@dataclass
class ResumePoint:
  checkpoint: str


@dataclass
class Transaction:
  tx_id: UUID
  amount: float
  currency: str
  description: str


@dataclass
class FeeSchedule:
  base_fee: float
  fx_markup_pct: float


@dataclass
class CalculationResult:
  label: str
  value: float


@dataclass
class AccountProfile:
  account_id: UUID
  full_name: str
  preferred_locale: str
  preferred_format: str


@dataclass
class FormattingPreferences:
  locale: str
  date_format: str
  currency_symbol: str


@dataclass
class StatementTotals:
  opening_balance: float
  closing_balance: float
  total_debits: float
  total_credits: float
  total_fees: float


# ---------------------------------------------------------------------------
# Flow context  – stores all inter-step values
# ---------------------------------------------------------------------------

@dataclass
class FlowContext:
  customer_id: Optional[UUID] = None
  account_id: Optional[UUID] = None
  validated_statement: Optional[ValidatedStatement] = None
  existing_statement_id: Optional[UUID] = None
  statement_status: Optional[str] = None   # "completed|running|failed|not_found"
  job_progress: Optional[JobProgress] = None
  restarted_job_id: Optional[UUID] = None
  failure_analysis: Optional[FailureAnalysis] = None
  recovery_result: Optional[RecoveryResult] = None
  resume_point: Optional[ResumePoint] = None
  new_statement_id: Optional[UUID] = None
  statement_id: Optional[UUID] = None
  transactions: list[Transaction] = field(default_factory=list)
  fee_schedule: Optional[FeeSchedule] = None
  preliminary_calculations: list[CalculationResult] = field(default_factory=list)
  account_profile: Optional[AccountProfile] = None
  formatting_preferences: Optional[FormattingPreferences] = None
  statement_totals: Optional[StatementTotals] = None
  statement_document: Optional[bytes] = None
  download_url: Optional[str] = None
  job_status: Optional[str] = None
  response: Optional[StatementAcceptedResponse] = None


# ===========================================================================
# Custom Exception Classes  (derived from @meta.exceptions across all actions)
# ===========================================================================

class UnauthorizedException(Exception):
  """Raised when JWT authentication fails."""


class ForbiddenException(Exception):
  """Raised when the caller lacks permission."""


class BadRequestException(Exception):
  """Raised when request parameters are invalid."""


class ConflictException(Exception):
  """Raised on idempotency key conflict or state conflict."""


class NotFoundException(Exception):
  """Raised when a required resource is not found."""


class JobNotFoundException(Exception):
  """Raised when the referenced job does not exist."""


class AnalysisUnavailableException(Exception):
  """Raised when failure analysis cannot be performed."""


class ProfileServiceUnavailableException(Exception):
  """Raised when the external profile service is unavailable after retries."""


class StorageUnavailableException(Exception):
  """Raised when object storage is unavailable after retries."""


class EventPublishFailedException(Exception):
  """Raised when publishing the StatementReady domain event fails."""


# ===========================================================================
# Retry Helper
# ===========================================================================

async def retry_async(
    coro_factory,
    *,
    max_attempts: int,
    backoff_ms: int,
    retry_on: list[str],
    on_exhausted_exc: Exception,
):
  """
  Execute an async coroutine factory with retry logic.

  :param coro_factory:  callable that returns a new coroutine each time
  :param max_attempts:  total attempts (not retries)
  :param backoff_ms:    fixed backoff between attempts in milliseconds
  :param retry_on:      list of exception type names / HTTP status strings to retry
  :param on_exhausted_exc: exception instance to raise when retries are exhausted
  """
  last_exc: Optional[Exception] = None
  for attempt in range(1, max_attempts + 1):
    try:
      return await coro_factory()
    except Exception as exc:
      exc_name = type(exc).__name__
      # Check whether this exception is retryable
      if any(label in exc_name or label in str(exc) for label in retry_on):
        last_exc = exc
        if attempt < max_attempts:
          logger.warning(
              "Retry %d/%d after %dms due to: %s",
              attempt, max_attempts, backoff_ms, exc,
          )
          await asyncio.sleep(backoff_ms / 1000)
      else:
        raise  # non-retryable – propagate immediately
  raise on_exhausted_exc from last_exc


# ===========================================================================
# Audit Helper
# ===========================================================================

def audit_log(action: str, context: FlowContext, **extra: Any) -> None:
  """Side-effect-free audit log entry (writes to structured logger)."""
  logger.info(
      "AUDIT action=%s customer_id=%s account_id=%s extra=%s",
      action, context.customer_id, context.account_id, extra,
  )


# ===========================================================================
# StatementService  – implements every internal action
# ===========================================================================

class StatementService:

  # ------------------------------------------------------------------
  # Security
  # ------------------------------------------------------------------

  async def authenticate_request(self, auth_token: str, ctx: FlowContext) -> None:
    """
    Authenticate customer and validate JWT token.
    stereotype: Security
    preCondition:  valid_jwt_format
    postCondition: customer_authenticated
    exceptions: Unauthorized, Forbidden
    """
    # preCondition guard
    if not auth_token or not auth_token.startswith("Bearer "):
      raise UnauthorizedException("valid_jwt_format precondition failed: missing or malformed token")

    # Assumption: real JWT validation library (e.g. PyJWT) would be used;
    # here we simulate extraction of customer_id from the token payload.
    try:
      # Simulated decode – replace with PyJWT decode in production
      token_body = auth_token.split(".")
      if len(token_body) < 2:
        raise UnauthorizedException("Invalid JWT structure")
      # Simulate customer_id extraction
      ctx.customer_id = uuid.uuid4()  # placeholder
    except UnauthorizedException:
      raise
    except Exception as exc:
      raise UnauthorizedException(f"Token validation error: {exc}") from exc

    # postCondition guard
    if ctx.customer_id is None:
      raise UnauthorizedException("customer_authenticated postcondition failed")

  # ------------------------------------------------------------------
  # Validation
  # ------------------------------------------------------------------

  async def validate_statement_request(
      self,
      account_id: UUID,
      statement_request: StatementRequest,
      idempotency_key: str,
      customer_id: UUID,
      ctx: FlowContext,
  ) -> None:
    """
    Validate statement request parameters and check idempotency.
    stereotype: Validation
    preCondition:  customer_authenticated
    postCondition: request_validated_and_idempotency_checked
    exceptions: BadRequest, Conflict, NotFound
    sideEffects: true  (idempotency key written to store)
    """
    # preCondition guard
    if customer_id is None:
      raise BadRequestException("customer_authenticated precondition failed")

    # Basic field validation
    if not statement_request.period_start or not statement_request.period_end:
      raise BadRequestException("period_start and period_end are required")
    if not idempotency_key:
      raise BadRequestException("Idempotency-Key header is required")

    # Assumption: idempotency store lookup; we simulate with None (no existing record).
    existing_id: Optional[UUID] = None  # replace with real store lookup

    ctx.validated_statement = ValidatedStatement(
        account_id=account_id,
        period_start=statement_request.period_start,
        period_end=statement_request.period_end,
        format=statement_request.format,
        idempotency_key=idempotency_key,
    )
    ctx.existing_statement_id = existing_id
    ctx.account_id = account_id

    # postCondition guard
    if ctx.validated_statement is None:
      raise BadRequestException("request_validated_and_idempotency_checked postcondition failed")

  # ------------------------------------------------------------------
  # BusinessRule
  # ------------------------------------------------------------------

  async def evaluate_statement_status(self, ctx: FlowContext) -> None:
    """
    Evaluate current statement status to determine processing path.
    stereotype: BusinessRule
    outputs: statementStatus → context
    exceptions: Conflict
    """
    # Assumption: If existing_statement_id is None there is no prior record → not_found.
    # Otherwise we query the job store (simulated here).
    if ctx.existing_statement_id is None:
      ctx.statement_status = "not_found"
    else:
      # Simulate status lookup; in production query the job repository.
      ctx.statement_status = "not_found"  # placeholder

    valid_statuses = {"completed", "running", "failed", "not_found"}
    if ctx.statement_status not in valid_statuses:
      raise ConflictException(f"Unknown statement status: {ctx.statement_status}")

  # ------------------------------------------------------------------
  # Mapper – completed path
  # ------------------------------------------------------------------

  async def build_completed_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response for existing completed statement.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.existing_statement_id),
        status="COMPLETED",
        message="Statement already generated.",
    )

  # ------------------------------------------------------------------
  # Repository – running path
  # ------------------------------------------------------------------

  async def check_job_progress(self, ctx: FlowContext) -> None:
    """
    Check if running job is stalled or needs to be restarted.
    stereotype: Repository
    preCondition:  statement_status_running
    postCondition: job_progress_evaluated
    exceptions: JobNotFound
    """
    # preCondition guard
    if ctx.statement_status != "running":
      raise BadRequestException("statement_status_running precondition failed")

    if ctx.existing_statement_id is None:
      raise JobNotFoundException("No existing statement ID available")

    # Simulation: assume job is not stalled; replace with real DB query.
    ctx.job_progress = JobProgress(
        job_id=ctx.existing_statement_id,
        stalled=False,
        progress_pct=42,
    )

    # postCondition guard
    if ctx.job_progress is None:
      raise JobNotFoundException("job_progress_evaluated postcondition failed")

  async def restart_job(self, ctx: FlowContext) -> None:
    """
    Restart stalled job with monitoring.
    stereotype: Repository
    preCondition:  job_stalled
    postCondition: job_restarted_and_monitored
    sideEffects: true
    """
    # preCondition guard
    if ctx.job_progress is None or not ctx.job_progress.stalled:
      raise BadRequestException("job_stalled precondition failed")

    # Simulation: mark old job cancelled, create new job.
    ctx.restarted_job_id = uuid.uuid4()

    # postCondition guard
    if ctx.restarted_job_id is None:
      raise ConflictException("job_restarted_and_monitored postcondition failed")

  async def build_restarted_job_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response for restarted job.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.restarted_job_id),
        status="RESTARTED",
        message="Stalled job has been restarted.",
    )

  async def build_running_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response for still-running job.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.existing_statement_id),
        status="RUNNING",
        message="Statement generation is in progress.",
    )

  # ------------------------------------------------------------------
  # BusinessRule – failed path
  # ------------------------------------------------------------------

  async def analyze_failure_reason(self, ctx: FlowContext) -> None:
    """
    Analyze why previous statement generation failed.
    stereotype: BusinessRule
    preCondition:  statement_status_failed
    postCondition: failure_root_cause_identified
    exceptions: AnalysisUnavailable
    """
    # preCondition guard
    if ctx.statement_status != "failed":
      raise BadRequestException("statement_status_failed precondition failed")

    # Simulation: inspect job record and derive root cause.
    ctx.failure_analysis = FailureAnalysis(
        root_cause="StorageTimeout",
        retryable=True,
    )

    # postCondition guard
    if ctx.failure_analysis is None:
      raise AnalysisUnavailableException("failure_root_cause_identified postcondition failed")

  async def recover_failed_job(self, ctx: FlowContext) -> None:
    """
    Recover failed job with checkpoint resume.
    stereotype: BusinessRule
    preCondition:  retryable_failure_detected
    postCondition: job_recovery_initiated
    sideEffects: true
    """
    # preCondition guard
    if ctx.failure_analysis is None or not ctx.failure_analysis.retryable:
      raise BadRequestException("retryable_failure_detected precondition failed")

    ctx.recovery_result = RecoveryResult(
        recovered_job_id=ctx.existing_statement_id or uuid.uuid4(),
        strategy="CheckpointResume",
    )
    ctx.resume_point = ResumePoint(checkpoint="AFTER_TRANSACTIONS_LOAD")

    # postCondition guard
    if ctx.recovery_result is None:
      raise ConflictException("job_recovery_initiated postcondition failed")

  async def build_recovery_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response for recovered job.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.recovery_result.recovered_job_id),
        status="RECOVERING",
        message=f"Job recovery initiated using strategy: {ctx.recovery_result.strategy}",
    )

  # ------------------------------------------------------------------
  # Repository – non-retryable failure path
  # ------------------------------------------------------------------

  async def create_new_statement_job(self, ctx: FlowContext) -> None:
    """
    Create new job for non-retryable failures.
    stereotype: Repository
    sideEffects: true
    """
    ctx.new_statement_id = uuid.uuid4()

  async def build_new_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response for newly created job.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.new_statement_id),
        status="ACCEPTED",
        message="A new statement generation job has been created.",
    )

  # ------------------------------------------------------------------
  # Repository – not_found path: create initial job
  # ------------------------------------------------------------------

  async def create_statement_job(self, ctx: FlowContext) -> None:
    """
    Create statement generation job.
    stereotype: Repository
    sideEffects: true
    """
    ctx.statement_id = uuid.uuid4()

  # ------------------------------------------------------------------
  # Fork/Join parallel branches
  # ------------------------------------------------------------------

  async def load_transactions(self, ctx: FlowContext) -> None:
    """
    Load transactions for the account and period.
    stereotype: Repository
    preCondition:  job_created
    postCondition: transaction_data_loaded
    """
    # preCondition guard
    if ctx.statement_id is None:
      raise NotFoundException("job_created precondition failed: statement_id is None")

    # Simulation: fetch from DB; return sample data.
    ctx.transactions = [
      Transaction(tx_id=uuid.uuid4(), amount=100.0, currency="USD", description="Purchase A"),
      Transaction(tx_id=uuid.uuid4(), amount=50.0, currency="USD", description="Purchase B"),
    ]

    # postCondition guard
    if ctx.transactions is None:
      raise NotFoundException("transaction_data_loaded postcondition failed")

  async def load_fees_and_rates(self, ctx: FlowContext) -> None:
    """
    Load fee schedule and FX rates from cache.
    stereotype: Cache

    NOTE: This branch depends on `transactions` which is loaded in the
    parallel LoadTransactions branch.  Per the spec the two run concurrently.
    The asyncio.gather in the controller ensures both complete before Join.
    In practice, if LoadFeesAndRates genuinely depends on transactions it
    should follow LoadTransactions.  We implement as specified (concurrent)
    and accept that the service method reads ctx.transactions which may still
    be None if LoadTransactions hasn't finished yet — this is an inherent
    ordering constraint the spec author accepted by placing them in a Fork.
    """
    ctx.fee_schedule = FeeSchedule(base_fee=2.50, fx_markup_pct=0.015)
    ctx.preliminary_calculations = [
      CalculationResult(label="BaseFee", value=ctx.fee_schedule.base_fee),
    ]

  async def fetch_account_profile(self, ctx: FlowContext) -> None:
    """
    Fetch account holder profile and formatting preferences.
    stereotype: ExternalCall
    preCondition:  job_created
    postCondition: account_profile_retrieved
    sideEffects: false
    retryPolicy: maxAttempts=2, backoffMs=400, retryOn=[Timeout, 502, 503]
    exceptions: ProfileServiceUnavailable
    """
    # preCondition guard
    if ctx.statement_id is None:
      raise NotFoundException("job_created precondition failed")

    async def _call():
      # Simulation: HTTP GET to profile service.
      ctx.account_profile = AccountProfile(
          account_id=ctx.account_id,
          full_name="Jane Doe",
          preferred_locale="en-US",
          preferred_format="PDF",
      )

    await retry_async(
        _call,
        max_attempts=2,
        backoff_ms=400,
        retry_on=["Timeout", "502", "503"],
        on_exhausted_exc=ProfileServiceUnavailableException("Profile service unavailable after retries"),
    )

    # postCondition guard
    if ctx.account_profile is None:
      raise ProfileServiceUnavailableException("account_profile_retrieved postcondition failed")

  # ------------------------------------------------------------------
  # Decision(profileAvailable) helpers inside FetchAccountProfile branch
  # ------------------------------------------------------------------

  async def load_formatting_preferences(self, ctx: FlowContext) -> None:
    """
    Extract formatting preferences from account profile.
    stereotype: Mapper
    """
    ctx.formatting_preferences = FormattingPreferences(
        locale=ctx.account_profile.preferred_locale,
        date_format="MM/DD/YYYY",
        currency_symbol="$",
    )

  async def use_default_formatting_preferences(self, ctx: FlowContext) -> None:
    """
    Use system default formatting preferences when profile is unavailable.
    stereotype: Mapper
    """
    ctx.formatting_preferences = FormattingPreferences(
        locale="en-US",
        date_format="YYYY-MM-DD",
        currency_symbol="$",
    )

  # ------------------------------------------------------------------
  # Post-Join sequential actions
  # ------------------------------------------------------------------

  async def compute_statement_totals(self, ctx: FlowContext) -> None:
    """
    Compute totals using preliminary calculations and transaction data.
    stereotype: BusinessRule
    """
    total_debits = sum(t.amount for t in ctx.transactions if t.amount < 0)
    total_credits = sum(t.amount for t in ctx.transactions if t.amount >= 0)
    base_fee = ctx.fee_schedule.base_fee if ctx.fee_schedule else 0.0
    ctx.statement_totals = StatementTotals(
        opening_balance=0.0,
        closing_balance=total_credits + total_debits - base_fee,
        total_debits=abs(total_debits),
        total_credits=total_credits,
        total_fees=base_fee,
    )

  async def render_statement_document(self, ctx: FlowContext) -> None:
    """
    Render document with formatting preferences and computed totals.
    stereotype: Mapper
    """
    # Simulation: build a minimal byte representation.
    summary = (
      f"Statement for {ctx.account_profile.full_name if ctx.account_profile else 'Unknown'}\n"
      f"Period: {ctx.validated_statement.period_start} – {ctx.validated_statement.period_end}\n"
      f"Closing Balance: {ctx.statement_totals.closing_balance:.2f}\n"
    )
    ctx.statement_document = summary.encode("utf-8")

  async def store_statement(self, ctx: FlowContext) -> None:
    """
    Store document to object storage and update job status.
    stereotype: ExternalCall
    preCondition:  document_rendered
    postCondition: statement_stored_and_job_completed
    sideEffects: true
    retryPolicy: maxAttempts=3, backoffMs=500, retryOn=[Timeout, 502, 503, StorageUnavailable]
    exceptions: StorageUnavailable
    """
    # preCondition guard
    if ctx.statement_document is None:
      raise StorageUnavailableException("document_rendered precondition failed")

    async def _upload():
      # Simulation: PUT to S3-compatible storage.
      ctx.download_url = f"https://storage.example.com/statements/{ctx.statement_id}.pdf"
      ctx.job_status = "COMPLETED"

    await retry_async(
        _upload,
        max_attempts=3,
        backoff_ms=500,
        retry_on=["Timeout", "502", "503", "StorageUnavailable"],
        on_exhausted_exc=StorageUnavailableException("Storage unavailable after retries"),
    )

    # postCondition guard
    if ctx.download_url is None or ctx.job_status != "COMPLETED":
      raise StorageUnavailableException("statement_stored_and_job_completed postcondition failed")

  async def publish_statement_ready(self, ctx: FlowContext) -> None:
    """
    Publish StatementReady domain event.
    stereotype: Publisher
    sideEffects: true
    exceptions: EventPublishFailed
    """
    # Simulation: publish to message bus (Kafka / SNS / etc.).
    logger.info(
        "EVENT StatementReady statementId=%s downloadUrl=%s",
        ctx.statement_id, ctx.download_url,
    )
    # If the publish fails propagate EventPublishFailedException.
    # In production, use the appropriate client SDK here.
    event_published = True  # simulation
    if not event_published:
      raise EventPublishFailedException("Failed to publish StatementReady event")

  async def build_statement_response(self, ctx: FlowContext) -> None:
    """
    Build 202 response with download URL.
    stereotype: Mapper
    """
    ctx.response = StatementAcceptedResponse(
        statement_id=str(ctx.statement_id),
        download_url=ctx.download_url,
        status="ACCEPTED",
        message="Statement generation completed.",
    )

  # ------------------------------------------------------------------
  # Fork/Join parallel orchestration helper
  # ------------------------------------------------------------------

  async def _parallel_fetch_account_profile_branch(self, ctx: FlowContext) -> None:
    """
    Encapsulates the FetchAccountProfile parallel branch including its
    inner Decision(profileAvailable) / FlowJoinProfileResolved merge.
    """
    try:
      await self.fetch_account_profile(ctx)
      profile_available = ctx.account_profile is not None
    except ProfileServiceUnavailableException:
      profile_available = False

    # Decision(profileAvailable)
    if profile_available:
      # if yes:
      await self.load_formatting_preferences(ctx)
      # FlowJoinProfileResolved → return
    else:
      # if no:
      await self.use_default_formatting_preferences(ctx)
      # FlowJoinProfileResolved → return


# ===========================================================================
# StatementController  – REST endpoint orchestrator
# ===========================================================================

app = FastAPI(title="Statement Service")


@app.post(
    "/accounts/{account_id}/statements",
    status_code=202,
    response_model=StatementAcceptedResponse,
    responses={
      400: {"description": "Bad Request"},
      401: {"description": "Unauthorized"},
      403: {"description": "Forbidden"},
      404: {"description": "Not Found"},
      409: {"description": "Conflict"},
      422: {"description": "Unprocessable Entity"},
    },
)
async def generate_monthly_statement(
    account_id: UUID = Path(..., description="Target account identifier"),
    statement_request: StatementRequest = ...,
    authorization: str = Header(..., alias="Authorization"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> JSONResponse:
  """
  POST /accounts/{accountId}/statements
  responseSuccess: 202 Accepted
  responseError:   400 | 401 | 403 | 404 | 409 | 422
  """
  svc = StatementService()
  ctx = FlowContext()

  try:
    # ---------------------------------------------------------------
    # Step 1: AuthenticateRequest
    # ---------------------------------------------------------------
    await svc.authenticate_request(auth_token=authorization, ctx=ctx)

    # ---------------------------------------------------------------
    # Step 2: ValidateStatementRequest
    # ---------------------------------------------------------------
    await svc.validate_statement_request(
        account_id=account_id,
        statement_request=statement_request,
        idempotency_key=idempotency_key,
        customer_id=ctx.customer_id,
        ctx=ctx,
    )

    # ---------------------------------------------------------------
    # Step 3: EvaluateStatementStatus
    # ---------------------------------------------------------------
    await svc.evaluate_statement_status(ctx=ctx)

    # ---------------------------------------------------------------
    # Step 4: Decision(statementStatus)
    # ---------------------------------------------------------------
    if ctx.statement_status == "completed":
      # ---- if completed: ----
      await svc.build_completed_response(ctx=ctx)
      # FlowJoinGenerateStatement → end()

    elif ctx.statement_status == "running":
      # ---- if running: ----
      await svc.check_job_progress(ctx=ctx)

      # Decision(jobStalled)
      if ctx.job_progress.stalled:
        # if yes:
        await svc.restart_job(ctx=ctx)
        await svc.build_restarted_job_response(ctx=ctx)
        # FlowJoinJobStalled → FlowJoinGenerateStatement → end()
      else:
        # if no:
        await svc.build_running_response(ctx=ctx)
        # FlowJoinJobStalled → FlowJoinGenerateStatement → end()

    elif ctx.statement_status == "failed":
      # ---- if failed: ----
      await svc.analyze_failure_reason(ctx=ctx)

      # Decision(retryableFailure)
      if ctx.failure_analysis.retryable:
        # if yes:
        await svc.recover_failed_job(ctx=ctx)
        await svc.build_recovery_response(ctx=ctx)
        # FlowJoinRetryableFailure → FlowJoinGenerateStatement → end()
      else:
        # if no:
        await svc.create_new_statement_job(ctx=ctx)
        await svc.build_new_response(ctx=ctx)
        # FlowJoinRetryableFailure → FlowJoinGenerateStatement → end()

    elif ctx.statement_status == "not_found":
      # ---- if not_found: ----
      await svc.create_statement_job(ctx=ctx)

      # -----------------------------------------------------------
      # Fork(UaauxTmGAqAKCQv1)  – real concurrency via asyncio.gather
      # Three parallel branches:
      #   parallel LoadTransactions
      #   parallel LoadFeesAndRates
      #   parallel FetchAccountProfile (contains inner Decision)
      # -----------------------------------------------------------
      await asyncio.gather(
          svc.load_transactions(ctx=ctx),
          svc.load_fees_and_rates(ctx=ctx),
          svc._parallel_fetch_account_profile_branch(ctx=ctx),
      )
      # Join(IyduxTmGAqAKCQ1h) – all branches completed; flow is deterministic again

      await svc.compute_statement_totals(ctx=ctx)
      await svc.render_statement_document(ctx=ctx)
      await svc.store_statement(ctx=ctx)
      await svc.publish_statement_ready(ctx=ctx)
      await svc.build_statement_response(ctx=ctx)
      # FlowJoinGenerateStatement → end()

    else:
      # Should never happen – evaluate_statement_status guards valid values
      raise ConflictException(f"Unhandled statement status: {ctx.statement_status}")

  # -----------------------------------------------------------------------
  # Exception → HTTP error mapping
  # -----------------------------------------------------------------------
  except UnauthorizedException as exc:
    raise HTTPException(status_code=401, detail=str(exc))
  except ForbiddenException as exc:
    raise HTTPException(status_code=403, detail=str(exc))
  except BadRequestException as exc:
    raise HTTPException(status_code=400, detail=str(exc))
  except ConflictException as exc:
    raise HTTPException(status_code=409, detail=str(exc))
  except NotFoundException as exc:
    raise HTTPException(status_code=404, detail=str(exc))
  except JobNotFoundException as exc:
    raise HTTPException(status_code=404, detail=str(exc))
  except AnalysisUnavailableException as exc:
    raise HTTPException(status_code=422, detail=str(exc))
  except ProfileServiceUnavailableException as exc:
    raise HTTPException(status_code=503, detail=str(exc))
  except StorageUnavailableException as exc:
    raise HTTPException(status_code=503, detail=str(exc))
  except EventPublishFailedException as exc:
    raise HTTPException(status_code=500, detail=str(exc))
  except Exception as exc:
    logger.exception("Unexpected error in generate_monthly_statement")
    raise HTTPException(status_code=500, detail="Internal server error") from exc

  # -----------------------------------------------------------------------
  # FlowJoinGenerateStatement → end() – return 202 Accepted
  # -----------------------------------------------------------------------
  return JSONResponse(
      status_code=202,
      content=ctx.response.dict() if ctx.response else {},
  )


# ===========================================================================
# Entry point (for local dev: uvicorn generate_monthly_statement:app --reload)
# ===========================================================================
if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)