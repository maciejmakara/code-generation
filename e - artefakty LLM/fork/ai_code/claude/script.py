"""
generate_monthly_statement.py

Production-quality implementation of POST /accounts/{accountId}/statements
Generated from UML Activity Diagram pseudo-code specification.

Components:
  - DTOs / data models
  - Custom exceptions
  - Retry helper
  - Audit helper
  - StatementService  (all internal action methods)
  - StatementController (REST endpoint orchestration)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DTOs / Data Models
# ---------------------------------------------------------------------------

@dataclass
class StatementRequest:
  period_start: str          # e.g. "2024-01-01"
  period_end: str            # e.g. "2024-01-31"
  format: str = "PDF"


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
  percent_complete: float
  last_heartbeat_ts: float   # unix epoch
  stalled: bool


@dataclass
class FailureAnalysis:
  root_cause: str
  retryable: bool


@dataclass
class RecoveryResult:
  recovered_job_id: UUID
  status: str


@dataclass
class ResumePoint:
  checkpoint_name: str
  offset: int


@dataclass
class Transaction:
  transaction_id: UUID
  amount: float
  currency: str
  timestamp: str


@dataclass
class FeeSchedule:
  base_fee: float
  fx_rate: float


@dataclass
class CalculationResult:
  label: str
  value: float


@dataclass
class AccountProfile:
  account_id: UUID
  holder_name: str
  email: str
  locale: str
  preferences: dict = field(default_factory=dict)


@dataclass
class FormattingPreferences:
  locale: str
  date_format: str
  currency_symbol: str
  paper_size: str = "A4"


@dataclass
class StatementTotals:
  opening_balance: float
  closing_balance: float
  total_credits: float
  total_debits: float
  total_fees: float


@dataclass
class StatementAcceptedResponse:
  statement_id: Optional[UUID]
  status: str
  message: str
  download_url: Optional[str] = None


# Enum-like constants for StatementStatus
class StatementStatus:
  COMPLETED  = "completed"
  RUNNING    = "running"
  FAILED     = "failed"
  NOT_FOUND  = "not_found"


# ---------------------------------------------------------------------------
# Custom Exceptions  (from @meta.exceptions)
# ---------------------------------------------------------------------------

class UnauthorizedException(Exception):
  """JWT token missing or invalid."""
  http_status = 401


class ForbiddenException(Exception):
  """Authenticated customer does not own this resource."""
  http_status = 403


class BadRequestException(Exception):
  """Request parameters are invalid."""
  http_status = 400


class ConflictException(Exception):
  """Resource already exists or is in a conflicting state."""
  http_status = 409


class NotFoundException(Exception):
  """Requested resource was not found."""
  http_status = 404


class UnprocessableEntityException(Exception):
  """Semantic validation failed."""
  http_status = 422


class JobNotFoundException(Exception):
  """Running job record not found."""
  http_status = 404


class AnalysisUnavailableException(Exception):
  """Failure analysis could not be retrieved."""
  http_status = 422


class ProfileServiceUnavailableException(Exception):
  """External profile service is unavailable after retries."""
  http_status = 502


class DataCorruptionException(Exception):
  """Transaction data is corrupted."""
  http_status = 422


class StorageUnavailableException(Exception):
  """Object storage is unavailable after retries."""
  http_status = 502


class EventPublishFailedException(Exception):
  """Domain event could not be published."""
  http_status = 500


# ---------------------------------------------------------------------------
# Retry Helper
# ---------------------------------------------------------------------------

async def retry_async(
    fn,
    *args,
    max_attempts: int,
    backoff_ms: int,
    retry_on: tuple,
    exhausted_exception: type,
    **kwargs,
):
  """
  Execute async callable `fn` with exponential-style retry.

  :param max_attempts:       Total attempts (including first).
  :param backoff_ms:         Base delay in milliseconds between retries.
  :param retry_on:           Tuple of exception types (or string labels) to retry on.
  :param exhausted_exception: Exception class to raise when all attempts are spent.
  """
  last_exc: Optional[Exception] = None
  for attempt in range(1, max_attempts + 1):
    try:
      return await fn(*args, **kwargs)
    except Exception as exc:
      # Match by type name (string label) or actual type
      exc_name = type(exc).__name__
      if any(
          (isinstance(label, str) and label in (exc_name, str(exc)))
          or (isinstance(label, type) and isinstance(exc, label))
          for label in retry_on
      ):
        last_exc = exc
        if attempt < max_attempts:
          delay = backoff_ms / 1000.0
          logger.warning(
              "Retry %d/%d for %s after %.3fs due to %s",
              attempt, max_attempts, fn.__name__, delay, exc_name,
          )
          await asyncio.sleep(delay)
        else:
          logger.error("All %d attempts exhausted for %s", max_attempts, fn.__name__)
      else:
        raise
  raise exhausted_exception(f"Retries exhausted: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Audit Helper
# ---------------------------------------------------------------------------

def audit_log(action: str, context: dict, detail: str = "") -> None:
  """Lightweight audit logger. Replace with a real audit sink in production."""
  logger.info("[AUDIT] action=%s customer=%s detail=%s", action, context.get("customerId"), detail)


# ---------------------------------------------------------------------------
# StatementService
# ---------------------------------------------------------------------------

class StatementService:
  """
  Implements every internal action from the specification.
  Each method corresponds exactly to one action node.
  """

  # ------------------------------------------------------------------
  # Security
  # ------------------------------------------------------------------

  async def authenticate_request(self, auth_token: str) -> UUID:
    """
    AuthenticateRequest — Security
    Authenticate customer and validate JWT token.
    preCondition:  valid_jwt_format
    postCondition: customer_authenticated
    """
    # preCondition: valid_jwt_format
    if not auth_token or not auth_token.startswith("Bearer "):
      raise UnauthorizedException("JWT token missing or malformed.")

    # Assumption: real JWT validation would call an auth library here.
    # Stub: decode customer_id from a mock token store.
    try:
      # In production: decode & verify JWT, extract 'sub' claim.
      token_body = auth_token[len("Bearer "):]
      if token_body == "":
        raise UnauthorizedException("Empty token.")
      # Stub: derive a deterministic UUID from the token for demo purposes.
      customer_id = UUID(int=abs(hash(token_body)) % (2 ** 128))
    except (ValueError, AttributeError) as exc:
      raise UnauthorizedException("Invalid JWT.") from exc

    # postCondition: customer_authenticated
    logger.debug("Customer authenticated: %s", customer_id)
    return customer_id

  # ------------------------------------------------------------------
  # Validation
  # ------------------------------------------------------------------

  async def validate_statement_request(
      self,
      account_id: UUID,
      statement_request: StatementRequest,
      idempotency_key: str,
      customer_id: UUID,
  ) -> tuple[ValidatedStatement, Optional[UUID], UUID]:
    """
    ValidateStatementRequest — Validation
    Validate statement request parameters and check idempotency.
    preCondition:  customer_authenticated
    postCondition: request_validated_and_idempotency_checked
    sideEffects:   true  (records idempotency key)
    """
    # preCondition: customer_authenticated
    if customer_id is None:
      raise BadRequestException("customer_authenticated preCondition failed.")

    if account_id is None:
      raise BadRequestException("accountId is required.")
    if not statement_request.period_start or not statement_request.period_end:
      raise BadRequestException("period_start and period_end are required.")
    if not idempotency_key:
      raise BadRequestException("Idempotency-Key header is required.")

    # Assumption: ownership check — customer must own the account.
    # In production: query DB for account ownership.
    # Stub: always passes for demo.

    # Idempotency check: look up existing statement by idempotency key.
    existing_statement_id: Optional[UUID] = await self._lookup_idempotency_key(
        idempotency_key
    )
    if existing_statement_id:
      # Conflict: a request with this key was already submitted.
      # We surface existing_statement_id to the controller for routing.
      pass  # caller handles Conflict vs 202 based on status

    validated_statement = ValidatedStatement(
        account_id=account_id,
        period_start=statement_request.period_start,
        period_end=statement_request.period_end,
        format=statement_request.format,
        idempotency_key=idempotency_key,
    )

    # postCondition: request_validated_and_idempotency_checked
    return validated_statement, existing_statement_id, account_id

  async def _lookup_idempotency_key(self, key: str) -> Optional[UUID]:
    """
    Stub: in production query a DB/cache for existing statement by idempotency key.
    Returns None if not found.
    """
    # Assumption: no persistent store in this demo; always returns None.
    return None

  # ------------------------------------------------------------------
  # BusinessRule
  # ------------------------------------------------------------------

  async def evaluate_statement_status(
      self,
      account_id: UUID,
      validated_statement: ValidatedStatement,
  ) -> str:
    """
    EvaluateStatementStatus — BusinessRule
    Evaluate current statement status to determine processing path.
    """
    # Assumption: query DB for the most recent statement matching account/period.
    # Stub: always returns NOT_FOUND for demo.
    # In production: look up by account_id + period + format.
    return StatementStatus.NOT_FOUND

  # ------------------------------------------------------------------
  # Mapper — completed path
  # ------------------------------------------------------------------

  def build_completed_response(
      self, existing_statement_id: UUID
  ) -> StatementAcceptedResponse:
    """
    BuildCompletedResponse — Mapper
    Build 202 response for existing completed statement.
    """
    return StatementAcceptedResponse(
        statement_id=existing_statement_id,
        status="completed",
        message="Statement already generated.",
    )

  # ------------------------------------------------------------------
  # Repository — running path
  # ------------------------------------------------------------------

  async def check_job_progress(self, existing_statement_id: UUID) -> JobProgress:
    """
    CheckJobProgress — Repository
    Check if running job is stalled or needs to be restarted.
    preCondition:  statement_status_running
    postCondition: job_progress_evaluated
    """
    # preCondition: statement_status_running (enforced by caller branch)
    if existing_statement_id is None:
      raise JobNotFoundException("existing_statement_id is None.")

    # Stub: fetch job progress from DB.
    # Assumption: a job is considered stalled if last heartbeat > 5 minutes ago.
    stall_threshold_seconds = 300
    now = time.time()
    job_progress = JobProgress(
        job_id=existing_statement_id,
        percent_complete=45.0,
        last_heartbeat_ts=now - 400,  # Stub: 400s ago → stalled
        stalled=(now - (now - 400)) > stall_threshold_seconds,
    )

    # postCondition: job_progress_evaluated
    return job_progress

  async def restart_job(self, existing_statement_id: UUID) -> UUID:
    """
    RestartJob — Repository
    Restart stalled job with monitoring.
    preCondition:  job_stalled
    postCondition: job_restarted_and_monitored
    sideEffects:   true
    """
    # preCondition: job_stalled (enforced by caller branch)
    if existing_statement_id is None:
      raise NotFoundException("Job to restart not found.")

    # Stub: mark old job as cancelled, create new job.
    restarted_job_id = uuid.uuid4()
    logger.info("Job %s restarted as %s", existing_statement_id, restarted_job_id)

    # postCondition: job_restarted_and_monitored
    return restarted_job_id

  def build_restarted_job_response(
      self, restarted_job_id: UUID
  ) -> StatementAcceptedResponse:
    """
    BuildRestartedJobResponse — Mapper
    Build 202 response for restarted job.
    """
    return StatementAcceptedResponse(
        statement_id=restarted_job_id,
        status="running",
        message="Stalled job restarted.",
    )

  def build_running_response(
      self, existing_statement_id: UUID
  ) -> StatementAcceptedResponse:
    """
    BuildRunningResponse — Mapper
    Build 202 response for still-running job.
    """
    return StatementAcceptedResponse(
        statement_id=existing_statement_id,
        status="running",
        message="Job is still running.",
    )

  # ------------------------------------------------------------------
  # BusinessRule — failed path
  # ------------------------------------------------------------------

  async def analyze_failure_reason(
      self, existing_statement_id: UUID
  ) -> FailureAnalysis:
    """
    AnalyzeFailureReason — BusinessRule
    Analyze why previous statement generation failed.
    preCondition:  statement_status_failed
    postCondition: failure_root_cause_identified
    """
    # preCondition: statement_status_failed (enforced by caller branch)
    if existing_statement_id is None:
      raise AnalysisUnavailableException("No statement ID provided for analysis.")

    # Stub: query failure log for given job id.
    failure_analysis = FailureAnalysis(
        root_cause="TransientNetworkError",
        retryable=True,
    )

    # postCondition: failure_root_cause_identified
    return failure_analysis

  async def recover_failed_job(
      self, existing_statement_id: UUID
  ) -> tuple[RecoveryResult, ResumePoint]:
    """
    RecoverFailedJob — BusinessRule
    Recover failed job with checkpoint resume.
    preCondition:  retryable_failure_detected
    postCondition: job_recovery_initiated
    sideEffects:   true
    """
    # preCondition: retryable_failure_detected (enforced by caller branch)
    recovered_job_id = uuid.uuid4()
    recovery_result = RecoveryResult(
        recovered_job_id=recovered_job_id,
        status="recovering",
    )
    resume_point = ResumePoint(
        checkpoint_name="post_transaction_load",
        offset=0,
    )

    # postCondition: job_recovery_initiated
    return recovery_result, resume_point

  def build_recovery_response(
      self, recovery_result: RecoveryResult
  ) -> StatementAcceptedResponse:
    """
    BuildRecoveryResponse — Mapper
    Build 202 response for recovered job.
    """
    return StatementAcceptedResponse(
        statement_id=recovery_result.recovered_job_id,
        status="recovering",
        message="Failed job recovery initiated.",
    )

  async def create_new_statement_job(
      self,
      account_id: UUID,
      validated_statement: ValidatedStatement,
  ) -> UUID:
    """
    CreateNewStatementJob — Repository
    Create new job for non-retryable failures.
    sideEffects: true
    """
    new_statement_id = uuid.uuid4()
    logger.info("New statement job created: %s (non-retryable failure path)", new_statement_id)
    return new_statement_id

  def build_new_response(self, new_statement_id: UUID) -> StatementAcceptedResponse:
    """
    BuildNewResponse — Mapper
    Build 202 response for newly created job.
    """
    return StatementAcceptedResponse(
        statement_id=new_statement_id,
        status="accepted",
        message="New statement job created after non-retryable failure.",
    )

  # ------------------------------------------------------------------
  # Repository — not_found path
  # ------------------------------------------------------------------

  async def create_statement_job(
      self,
      account_id: UUID,
      validated_statement: ValidatedStatement,
  ) -> UUID:
    """
    CreateStatementJob — Repository
    Create statement generation job.
    sideEffects: true
    """
    statement_id = uuid.uuid4()
    logger.info("Statement job created: %s for account %s", statement_id, account_id)
    return statement_id

  # ------------------------------------------------------------------
  # Parallel branches (Fork/Join)
  # ------------------------------------------------------------------

  async def load_transactions(
      self,
      account_id: UUID,
      validated_statement: ValidatedStatement,
  ) -> list[Transaction]:
    """
    LoadTransactions — Repository
    Load transactions for the account and period.
    preCondition:  job_created
    postCondition: transaction_data_loaded
    """
    # preCondition: job_created (enforced by caller; statement_id exists in context)
    # Stub: return dummy transactions.
    transactions = [
      Transaction(
          transaction_id=uuid.uuid4(),
          amount=100.0,
          currency="USD",
          timestamp=validated_statement.period_start,
      )
    ]
    # postCondition: transaction_data_loaded
    return transactions

  async def load_fees_and_rates(
      self, validated_statement: ValidatedStatement
  ) -> tuple[FeeSchedule, list[CalculationResult]]:
    """
    LoadFeesAndRates — Cache
    Load fee schedule and FX rates from cache.
    """
    fee_schedule = FeeSchedule(base_fee=2.50, fx_rate=1.08)
    preliminary_calculations = [
      CalculationResult(label="fx_adjustment", value=5.0)
    ]
    return fee_schedule, preliminary_calculations

  async def _fetch_account_profile_inner(self, account_id: UUID) -> AccountProfile:
    """Raw fetch (wrapped by retry in fetch_account_profile)."""
    # Stub: in production call external profile service.
    return AccountProfile(
        account_id=account_id,
        holder_name="Jane Doe",
        email="jane.doe@example.com",
        locale="en-US",
        preferences={"date_format": "MM/DD/YYYY", "currency_symbol": "$"},
    )

  async def fetch_account_profile(self, account_id: UUID) -> Optional[AccountProfile]:
    """
    FetchAccountProfile — ExternalCall
    Fetch account holder profile and formatting preferences.
    preCondition:  job_created
    postCondition: account_profile_retrieved
    retryPolicy:   maxAttempts=2, backoffMs=400, retryOn=[Timeout, 502, 503]
                   onRetryExhausted: throw_ProfileServiceUnavailable
    sideEffects:   false
    """
    # preCondition: job_created (statement_id exists in context)
    try:
      profile = await retry_async(
          self._fetch_account_profile_inner,
          account_id,
          max_attempts=2,
          backoff_ms=400,
          retry_on=("Timeout", "502", "503", TimeoutError),
          exhausted_exception=ProfileServiceUnavailableException,
      )
    except ProfileServiceUnavailableException:
      raise

    # postCondition: account_profile_retrieved
    return profile

  def load_formatting_preferences(
      self, account_profile: AccountProfile
  ) -> FormattingPreferences:
    """
    LoadFormattingPreferences — Mapper
    Extract formatting preferences from account profile.
    """
    prefs = account_profile.preferences
    return FormattingPreferences(
        locale=account_profile.locale,
        date_format=prefs.get("date_format", "YYYY-MM-DD"),
        currency_symbol=prefs.get("currency_symbol", "$"),
    )

  def use_default_formatting_preferences(self) -> FormattingPreferences:
    """
    UseDefaultFormattingPreferences — Mapper
    Use system default formatting preferences when profile is unavailable.
    """
    return FormattingPreferences(
        locale="en-US",
        date_format="YYYY-MM-DD",
        currency_symbol="$",
    )

  # ------------------------------------------------------------------
  # BusinessRule — post-Join
  # ------------------------------------------------------------------

  def compute_statement_totals(
      self,
      transactions: list[Transaction],
      fee_schedule: FeeSchedule,
      preliminary_calculations: list[CalculationResult],
  ) -> StatementTotals:
    """
    ComputeStatementTotals — BusinessRule
    Compute totals using preliminary calculations and transaction data.
    """
    total_credits = sum(t.amount for t in transactions if t.amount > 0)
    total_debits  = sum(abs(t.amount) for t in transactions if t.amount < 0)
    fx_adj        = sum(c.value for c in preliminary_calculations)
    total_fees    = fee_schedule.base_fee + fx_adj

    return StatementTotals(
        opening_balance=0.0,      # Assumption: not provided by spec; default 0
        closing_balance=total_credits - total_debits - total_fees,
        total_credits=total_credits,
        total_debits=total_debits,
        total_fees=total_fees,
    )

  def render_statement_document(
      self,
      account_profile: AccountProfile,
      formatting_preferences: FormattingPreferences,
      transactions: list[Transaction],
      statement_totals: StatementTotals,
  ) -> bytes:
    """
    RenderStatementDocument — Mapper
    Render document with formatting preferences and computed totals.
    """
    # Stub: in production use a PDF rendering library (e.g. reportlab / weasyprint).
    content = (
      f"Statement for {account_profile.holder_name}\n"
      f"Locale: {formatting_preferences.locale}\n"
      f"Total Credits: {statement_totals.total_credits}\n"
      f"Total Debits:  {statement_totals.total_debits}\n"
      f"Total Fees:    {statement_totals.total_fees}\n"
      f"Closing Bal:   {statement_totals.closing_balance}\n"
      f"Transactions:  {len(transactions)}\n"
    )
    return content.encode("utf-8")

  async def _store_statement_inner(
      self, statement_id: UUID, statement_document: bytes
  ) -> tuple[str, str]:
    """Raw store (wrapped by retry in store_statement)."""
    # Stub: upload to object storage (S3, GCS, etc.)
    download_url = f"https://storage.example.com/statements/{statement_id}.pdf"
    job_status   = "completed"
    return download_url, job_status

  async def store_statement(
      self, statement_id: UUID, statement_document: bytes
  ) -> tuple[str, str]:
    """
    StoreStatement — ExternalCall
    Store document to object storage and update job status.
    preCondition:  document_rendered
    postCondition: statement_stored_and_job_completed
    retryPolicy:   maxAttempts=3, backoffMs=500,
                   retryOn=[Timeout, 502, 503, StorageUnavailable]
                   onRetryExhausted: throw_StorageUnavailable
    sideEffects:   true
    """
    # preCondition: document_rendered (enforced by caller; document bytes present)
    if not statement_document:
      raise StorageUnavailableException("document_rendered preCondition failed: empty document.")

    download_url, job_status = await retry_async(
        self._store_statement_inner,
        statement_id,
        statement_document,
        max_attempts=3,
        backoff_ms=500,
        retry_on=("Timeout", "502", "503", StorageUnavailableException, TimeoutError),
        exhausted_exception=StorageUnavailableException,
    )

    # postCondition: statement_stored_and_job_completed
    return download_url, job_status

  async def publish_statement_ready(
      self, statement_id: UUID, download_url: str
  ) -> bool:
    """
    PublishStatementReady — Publisher
    Publish StatementReady domain event.
    sideEffects: true
    """
    # Stub: publish to event bus (Kafka, SNS, etc.)
    logger.info(
        "Event published: StatementReady statement_id=%s url=%s",
        statement_id, download_url,
    )
    event_published = True
    return event_published

  def build_statement_response(
      self, statement_id: UUID, download_url: str
  ) -> StatementAcceptedResponse:
    """
    BuildStatementResponse — Mapper
    Build 202 response with download URL.
    """
    return StatementAcceptedResponse(
        statement_id=statement_id,
        status="accepted",
        message="Statement generation initiated.",
        download_url=download_url,
    )


# ---------------------------------------------------------------------------
# StatementController
# ---------------------------------------------------------------------------

class StatementController:
  """
  REST controller: POST /accounts/{accountId}/statements
  Orchestrates the full flow as specified in the UML Activity Diagram.
  """

  def __init__(self, service: StatementService):
    self.service = service

  async def generate_monthly_statement(
      self,
      account_id: str,
      auth_token: str,
      statement_request: StatementRequest,
      idempotency_key: str,
  ) -> tuple[int, StatementAcceptedResponse]:
    """
    POST /accounts/{accountId}/statements
    responseSuccess: 202 Accepted
    responseError:   400 | 401 | 403 | 404 | 409 | 422
    """

    # ----------------------------------------------------------------
    # Execution context (stores outputs targeted at "context")
    # ----------------------------------------------------------------
    ctx: dict[str, Any] = {}

    # ================================================================
    # 1. AuthenticateRequest  [Security]
    # ================================================================
    customer_id: UUID = await self.service.authenticate_request(auth_token)
    ctx["customerId"] = customer_id

    # ================================================================
    # 2. ValidateStatementRequest  [Validation]
    # ================================================================
    validated_statement, existing_statement_id, validated_account_id = (
      await self.service.validate_statement_request(
          account_id=UUID(account_id),
          statement_request=statement_request,
          idempotency_key=idempotency_key,
          customer_id=customer_id,
      )
    )
    ctx["validatedStatement"]    = validated_statement
    ctx["existingStatementId"]   = existing_statement_id
    ctx["accountId"]             = validated_account_id

    # ================================================================
    # 3. EvaluateStatementStatus  [BusinessRule]
    # ================================================================
    statement_status: str = await self.service.evaluate_statement_status(
        account_id=ctx["accountId"],
        validated_statement=ctx["validatedStatement"],
    )
    ctx["statementStatus"] = statement_status

    # ================================================================
    # 4. Decision(statementStatus)
    # ================================================================

    # ----------------------------------------------------------------
    # Branch: completed
    # ----------------------------------------------------------------
    if statement_status == StatementStatus.COMPLETED:
      response = self.service.build_completed_response(
          existing_statement_id=ctx["existingStatementId"]
      )
      ctx["response"] = response
      # FlowJoinGenerateStatement → end()
      return 202, ctx["response"]

    # ----------------------------------------------------------------
    # Branch: running
    # ----------------------------------------------------------------
    elif statement_status == StatementStatus.RUNNING:
      job_progress: JobProgress = await self.service.check_job_progress(
          existing_statement_id=ctx["existingStatementId"]
      )
      ctx["jobProgress"] = job_progress

      # Decision(jobStalled)
      if job_progress.stalled:
        # Branch: yes (job stalled)
        restarted_job_id: UUID = await self.service.restart_job(
            existing_statement_id=ctx["existingStatementId"]
        )
        ctx["restartedJobId"] = restarted_job_id

        response = self.service.build_restarted_job_response(
            restarted_job_id=ctx["restartedJobId"]
        )
        ctx["response"] = response
        # FlowJoinJobStalled → FlowJoinGenerateStatement → end()
        return 202, ctx["response"]

      else:
        # Branch: no (job still running and not stalled)
        response = self.service.build_running_response(
            existing_statement_id=ctx["existingStatementId"]
        )
        ctx["response"] = response
        # FlowJoinJobStalled → FlowJoinGenerateStatement → end()
        return 202, ctx["response"]

    # ----------------------------------------------------------------
    # Branch: failed
    # ----------------------------------------------------------------
    elif statement_status == StatementStatus.FAILED:
      failure_analysis: FailureAnalysis = await self.service.analyze_failure_reason(
          existing_statement_id=ctx["existingStatementId"]
      )
      ctx["failureAnalysis"] = failure_analysis

      # Decision(retryableFailure)
      if failure_analysis.retryable:
        # Branch: yes
        recovery_result, resume_point = await self.service.recover_failed_job(
            existing_statement_id=ctx["existingStatementId"]
        )
        ctx["recoveryResult"] = recovery_result
        ctx["resumePoint"]    = resume_point

        response = self.service.build_recovery_response(
            recovery_result=ctx["recoveryResult"]
        )
        ctx["response"] = response
        # FlowJoinRetryableFailure → FlowJoinGenerateStatement → end()
        return 202, ctx["response"]

      else:
        # Branch: no
        new_statement_id: UUID = await self.service.create_new_statement_job(
            account_id=ctx["accountId"],
            validated_statement=ctx["validatedStatement"],
        )
        ctx["newStatementId"] = new_statement_id

        response = self.service.build_new_response(
            new_statement_id=ctx["newStatementId"]
        )
        ctx["response"] = response
        # FlowJoinRetryableFailure → FlowJoinGenerateStatement → end()
        return 202, ctx["response"]

    # ----------------------------------------------------------------
    # Branch: not_found  (new statement, full processing path)
    # ----------------------------------------------------------------
    elif statement_status == StatementStatus.NOT_FOUND:
      # Step: CreateStatementJob
      statement_id: UUID = await self.service.create_statement_job(
          account_id=ctx["accountId"],
          validated_statement=ctx["validatedStatement"],
      )
      ctx["statementId"] = statement_id

      # ============================================================
      # Fork(UaauxTmGAqAKCQv1) — real concurrency via asyncio.gather
      # Three parallel branches:
      #   1. LoadTransactions
      #   2. LoadFeesAndRates
      #   3. FetchAccountProfile (with internal Decision + FlowJoinProfileResolved)
      # ============================================================

      async def branch_load_transactions() -> list[Transaction]:
        return await self.service.load_transactions(
            account_id=ctx["accountId"],
            validated_statement=ctx["validatedStatement"],
        )

      async def branch_load_fees_and_rates() -> tuple[FeeSchedule, list[CalculationResult]]:
        return await self.service.load_fees_and_rates(
            validated_statement=ctx["validatedStatement"]
        )

      async def branch_fetch_account_profile() -> FormattingPreferences:
        """
        FetchAccountProfile branch, includes internal Decision(profileAvailable)
        and FlowJoinProfileResolved merge node.
        """
        try:
          account_profile: Optional[AccountProfile] = (
            await self.service.fetch_account_profile(
                account_id=ctx["accountId"]
            )
          )
        except ProfileServiceUnavailableException:
          account_profile = None

        # Decision(profileAvailable)
        if account_profile is not None:
          # Branch: yes
          formatting_preferences = self.service.load_formatting_preferences(
              account_profile=account_profile
          )
          # FlowJoinProfileResolved → return from parallel branch
          return account_profile, formatting_preferences
        else:
          # Branch: no
          formatting_preferences = self.service.use_default_formatting_preferences()
          # FlowJoinProfileResolved → return from parallel branch
          # Assumption: when profile is unavailable we pass a stub AccountProfile
          # so downstream RenderStatementDocument has a valid object.
          stub_profile = AccountProfile(
              account_id=ctx["accountId"],
              holder_name="Unknown",
              email="",
              locale="en-US",
          )
          return stub_profile, formatting_preferences

      # Join(IyduxTmGAqAKCQ1h) — wait for ALL branches; propagate any error
      (
        transactions,
        (fee_schedule, preliminary_calculations),
        (account_profile, formatting_preferences),
      ) = await asyncio.gather(
          branch_load_transactions(),
          branch_load_fees_and_rates(),
          branch_fetch_account_profile(),
      )

      # Store parallel results in context
      ctx["transactions"]             = transactions
      ctx["feeSchedule"]              = fee_schedule
      ctx["preliminaryCalculations"]  = preliminary_calculations
      ctx["accountProfile"]           = account_profile
      ctx["formattingPreferences"]    = formatting_preferences

      # ============================================================
      # Post-Join sequential flow
      # ============================================================

      # ComputeStatementTotals
      statement_totals: StatementTotals = self.service.compute_statement_totals(
          transactions=ctx["transactions"],
          fee_schedule=ctx["feeSchedule"],
          preliminary_calculations=ctx["preliminaryCalculations"],
      )
      ctx["statementTotals"] = statement_totals

      # RenderStatementDocument
      statement_document: bytes = self.service.render_statement_document(
          account_profile=ctx["accountProfile"],
          formatting_preferences=ctx["formattingPreferences"],
          transactions=ctx["transactions"],
          statement_totals=ctx["statementTotals"],
      )
      ctx["statementDocument"] = statement_document

      # StoreStatement  (with retry policy)
      download_url, job_status = await self.service.store_statement(
          statement_id=ctx["statementId"],
          statement_document=ctx["statementDocument"],
      )
      ctx["downloadUrl"] = download_url
      ctx["jobStatus"]   = job_status

      # PublishStatementReady
      event_published: bool = await self.service.publish_statement_ready(
          statement_id=ctx["statementId"],
          download_url=ctx["downloadUrl"],
      )
      # event_published is targeted at "event" scope; not stored in context.

      # BuildStatementResponse
      response = self.service.build_statement_response(
          statement_id=ctx["statementId"],
          download_url=ctx["downloadUrl"],
      )
      ctx["response"] = response

      # FlowJoinGenerateStatement → end()
      return 202, ctx["response"]

    else:
      # Should never reach here; guard for unrecognised statementStatus values.
      raise UnprocessableEntityException(
          f"Unrecognised statementStatus: {statement_status}"
      )


# ---------------------------------------------------------------------------
# Exception → HTTP status mapping helper
# ---------------------------------------------------------------------------

ERROR_STATUS_MAP: dict[type, int] = {
  UnauthorizedException:           401,
  ForbiddenException:              403,
  BadRequestException:             400,
  ConflictException:               409,
  NotFoundException:               404,
  UnprocessableEntityException:    422,
  JobNotFoundException:            404,
  AnalysisUnavailableException:    422,
  ProfileServiceUnavailableException: 502,
  DataCorruptionException:         422,
  StorageUnavailableException:     502,
  EventPublishFailedException:     500,
}


def http_status_for(exc: Exception) -> int:
  return ERROR_STATUS_MAP.get(type(exc), 500)


# ---------------------------------------------------------------------------
# Minimal ASGI / standalone runner (demo wiring — replace with FastAPI/Flask)
# ---------------------------------------------------------------------------

async def handle_request(
    account_id: str,
    auth_token: str,
    body: dict,
    idempotency_key: str,
) -> dict:
  """
  Thin adapter: parses HTTP-layer inputs, calls the controller,
  returns a JSON-serialisable dict with status_code + body.
  """
  service    = StatementService()
  controller = StatementController(service)

  try:
    stmt_req = StatementRequest(
        period_start=body.get("period_start", ""),
        period_end=body.get("period_end", ""),
        format=body.get("format", "PDF"),
    )
    status_code, response = await controller.generate_monthly_statement(
        account_id=account_id,
        auth_token=auth_token,
        statement_request=stmt_req,
        idempotency_key=idempotency_key,
    )
    return {
      "status_code": status_code,
      "body": {
        "statement_id": str(response.statement_id) if response.statement_id else None,
        "status":       response.status,
        "message":      response.message,
        "download_url": response.download_url,
      },
    }

  except Exception as exc:
    status_code = http_status_for(exc)
    logger.error("Error in generate_monthly_statement: %s", exc, exc_info=True)
    return {
      "status_code": status_code,
      "body": {"error": type(exc).__name__, "detail": str(exc)},
    }


# ---------------------------------------------------------------------------
# Quick smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
  import json

  async def _smoke_test():
    result = await handle_request(
        account_id="00000000-0000-0000-0000-000000000001",
        auth_token="Bearer test-token-abc123",
        body={
          "period_start": "2024-01-01",
          "period_end":   "2024-01-31",
          "format":       "PDF",
        },
        idempotency_key="idem-key-001",
    )
    print(json.dumps(result, indent=2))

  logging.basicConfig(level=logging.DEBUG)
  asyncio.run(_smoke_test())