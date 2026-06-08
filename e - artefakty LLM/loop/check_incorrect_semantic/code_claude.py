"""
Bulk Import Contacts — Production-quality Python implementation
Generated from UML Activity Diagram pseudo-code specification.

Components:
  - DTOs / data models (dataclasses)
  - Custom exception classes
  - ImportService  (all internal actions)
  - ContactImportController  (REST endpoint orchestration)
  - FastAPI application wiring
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Custom Exception Classes  (@meta.exceptions)
# ──────────────────────────────────────────────────────────────────────────────


class UnauthorizedException(Exception):
  """Raised when the JWT token is missing or invalid."""


class ForbiddenException(Exception):
  """Raised when the operator is not allowed to import for this tenant."""


class BadRequestException(Exception):
  """Raised when the import request fails structural validation."""


class ConflictException(Exception):
  """Raised when an idempotency key collision is detected, or contact upsert conflicts."""


class FileNotFoundException(Exception):
  """Raised when the import file cannot be located in object storage."""


class StorageUnavailableException(Exception):
  """Raised when object storage is unreachable after retries are exhausted."""


# ──────────────────────────────────────────────────────────────────────────────
# DTOs / Data Models
# ──────────────────────────────────────────────────────────────────────────────


class ContactImportRequest(BaseModel):
  file_reference: str          # object-storage key / URL
  format: str                  # e.g. "csv"
  idempotency_key: str


@dataclass
class ValidatedImport:
  file_reference: str
  format: str
  idempotency_key: str
  tenant_id: UUID


@dataclass
class RawContactRow:
  row_number: int
  data: dict[str, str]


@dataclass
class Contact:
  tenant_id: UUID
  email: Optional[str]
  phone: Optional[str]
  first_name: Optional[str]
  last_name: Optional[str]
  tags: list[str] = field(default_factory=list)


@dataclass
class ImportError:
  row_number: int
  message: str


@dataclass
class ContactImportAcceptedResponse:
  import_id: UUID
  results_url: str


# ──────────────────────────────────────────────────────────────────────────────
# Flow Context  (carries all intermediate values between actions)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ImportFlowContext:
  # populated by AuthorizeOperator
  operator_id: Optional[UUID] = None
  # populated by ValidateImportRequest
  validated_import: Optional[ValidatedImport] = None
  # populated by CheckImportIdempotency
  existing_import_id: Optional[UUID] = None
  # populated by LoadImportFile
  file_bytes: Optional[bytes] = None
  # populated by ParseCsvRows
  rows: list[RawContactRow] = field(default_factory=list)
  total_rows: int = 0
  # populated by InitializeImportCounters
  row_index: int = 0
  import_errors: list[ImportError] = field(default_factory=list)
  # populated per-row
  current_row: Optional[RawContactRow] = None
  row_valid: bool = False
  not_duplicate: bool = False
  contact: Optional[Contact] = None
  contact_id: Optional[UUID] = None
  # populated at end
  import_id: Optional[UUID] = None
  # final response (target=response)
  response: Optional[ContactImportAcceptedResponse] = None


# ──────────────────────────────────────────────────────────────────────────────
# Retry Helper
# ──────────────────────────────────────────────────────────────────────────────


async def retry_async(
    coro_factory,
    max_attempts: int,
    backoff_ms: int,
    retry_on: tuple[type[Exception], ...],
    on_retry_exhausted: type[Exception],
):
  """Execute an async callable with retry logic per retryPolicy @meta."""
  last_exc: Exception = RuntimeError("retry block never ran")
  for attempt in range(1, max_attempts + 1):
    try:
      return await coro_factory()
    except retry_on as exc:
      last_exc = exc
      if attempt < max_attempts:
        await asyncio.sleep(backoff_ms / 1000)
      else:
        raise on_retry_exhausted(str(exc)) from exc
  raise on_retry_exhausted(str(last_exc))  # unreachable but satisfies type checkers


# ──────────────────────────────────────────────────────────────────────────────
# Import Service  (all internal actions mapped to methods)
# ──────────────────────────────────────────────────────────────────────────────


class ImportService:
  """
  Implements every action from the Bulk Import Contacts specification.
  Each method corresponds 1-to-1 to an action block in the spec.
  """

  # ── in-memory fakes (stand-ins for real infrastructure) ──────────────────
  _used_idempotency_keys: dict[tuple[UUID, str], UUID] = {}  # (tenant, key) -> importId
  _imports: dict[UUID, dict] = {}
  _contacts: dict[UUID, dict] = {}  # contactId -> contact record

  # ── Security ─────────────────────────────────────────────────────────────

  async def authorize_operator(
      self,
      auth_token: str,
      tenant_id: UUID,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Security
    Ensure the caller is an operator allowed to import contacts for a tenant.
    inputs:  authToken (JWT, request), tenantId (UUID, request)
    outputs: operatorId (UUID, context)
    exceptions: Unauthorized, Forbidden
    """
    # Assumption: real impl would verify JWT signature and extract claims.
    # Here we simulate a valid token check.
    if not auth_token or not auth_token.startswith("Bearer "):
      raise UnauthorizedException("Missing or malformed Authorization header.")

    # Assumption: decode & validate JWT; extract operator_id from sub claim.
    # For demo purposes we derive a deterministic operator UUID from the token.
    raw = auth_token.removeprefix("Bearer ").strip()
    if not raw:
      raise UnauthorizedException("Empty bearer token.")

    # Simulate role check — a real system would check tenant membership.
    if raw == "FORBIDDEN":
      raise ForbiddenException("Operator is not allowed to import for this tenant.")

    ctx.operator_id = uuid.uuid5(tenant_id, raw)

  # ── Validation ────────────────────────────────────────────────────────────

  async def validate_import_request(
      self,
      import_request: ContactImportRequest,
      idempotency_key: str,
      tenant_id: UUID,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Validation
    Validate import request includes file reference, format, and idempotency key.
    inputs:  importRequest (ContactImportRequest, request), idempotencyKey (String, request)
    outputs: validatedImport (ValidatedImport, context)
    exceptions: BadRequest
    """
    errors: list[str] = []
    if not import_request.file_reference:
      errors.append("file_reference is required.")
    if not import_request.format:
      errors.append("format is required.")
    if import_request.format not in {"csv"}:
      errors.append(f"Unsupported format '{import_request.format}'. Supported: csv.")
    if not idempotency_key:
      errors.append("idempotency_key is required.")
    if errors:
      raise BadRequestException("; ".join(errors))

    ctx.validated_import = ValidatedImport(
        file_reference=import_request.file_reference,
        format=import_request.format,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
    )

  # ── Repository ────────────────────────────────────────────────────────────

  async def check_import_idempotency(
      self,
      tenant_id: UUID,
      idempotency_key: str,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Repository
    Ensure the idempotency key was not used before for this tenant.
    inputs:  tenantId (UUID, request), idempotencyKey (String, request)
    outputs: existingImportId (UUID?, context)
    exceptions: Conflict
    consistencyScope: local-transaction
    sideEffects: true, idempotent: true
    """
    # Assumption: in production this would be a DB read inside a transaction.
    key = (tenant_id, idempotency_key)
    ctx.existing_import_id = self._used_idempotency_keys.get(key)

  # ── Mapper ────────────────────────────────────────────────────────────────

  async def return_existing_import(
      self,
      existing_import_id: UUID,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Mapper
    Return existing import reference without processing the file again.
    inputs:  existingImportId (UUID, context)
    outputs: response (ContactImportAcceptedResponse, response)
    """
    ctx.response = ContactImportAcceptedResponse(
        import_id=existing_import_id,
        results_url=f"/contacts/imports/{existing_import_id}/results",
    )

  # ── ExternalCall ──────────────────────────────────────────────────────────

  async def load_import_file(
      self,
      validated_import: ValidatedImport,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: ExternalCall
    Download import file from object storage and verify checksum.
    inputs:  validatedImport (ValidatedImport, context)
    outputs: fileBytes (byte[], context)
    retryPolicy: maxAttempts=3, backoffMs=300, retryOn=[Timeout, 502, 503]
      onRetryExhausted: throw_StorageUnavailable
    exceptions: FileNotFound, StorageUnavailable
    sideEffects: false, idempotent: true, consistencyScope: none
    """

    async def _fetch():
      # Assumption: validated_import.file_reference is a full URL or storage key.
      # Real implementation would call object-storage SDK/API.
      async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(validated_import.file_reference)
        if resp.status_code == 404:
          raise FileNotFoundException(
              f"File not found: {validated_import.file_reference}"
          )
        if resp.status_code in {502, 503}:
          raise StorageUnavailableException(
              f"Storage returned {resp.status_code}"
          )
        resp.raise_for_status()
        return resp.content

    ctx.file_bytes = await retry_async(
        coro_factory=_fetch,
        max_attempts=3,
        backoff_ms=300,
        retry_on=(httpx.TimeoutException, StorageUnavailableException),
        on_retry_exhausted=StorageUnavailableException,
    )

  # ── Mapper ────────────────────────────────────────────────────────────────

  async def parse_csv_rows(
      self,
      file_bytes: bytes,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Mapper
    Parse CSV into a list of raw contact rows, preserving row numbers for error reporting.
    inputs:  fileBytes (byte[], context)
    outputs: rows (RawContactRow[], context), totalRows (int, context)
    """
    text = file_bytes.decode("utf-8-sig")  # handle optional BOM
    reader = csv.DictReader(io.StringIO(text))
    rows: list[RawContactRow] = []
    for line_num, record in enumerate(reader, start=2):  # row 1 = header
      rows.append(RawContactRow(row_number=line_num, data=dict(record)))
    ctx.rows = rows
    ctx.total_rows = len(rows)

  # ── Utility ───────────────────────────────────────────────────────────────

  async def initialize_import_counters(
      self,
      total_rows: int,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Utility
    Initialize counters and an error collection for the import session.
    inputs:  totalRows (int, context)
    outputs: rowIndex (int, context), importErrors (ImportError[], context)
    """
    ctx.row_index = 0
    ctx.import_errors = []

  # ── Mapper ────────────────────────────────────────────────────────────────

  async def read_next_row(
      self,
      rows: list[RawContactRow],
      row_index: int,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Mapper
    Read current row and normalize values (trim, casing, locale).
    inputs:  rows (RawContactRow[], context), rowIndex (int, context)
    outputs: currentRow (RawContactRow, context)
    """
    raw = rows[row_index]
    normalized = {k.strip().lower(): v.strip() for k, v in raw.data.items()}
    ctx.current_row = RawContactRow(row_number=raw.row_number, data=normalized)

  # ── Validation ────────────────────────────────────────────────────────────

  async def validate_row(
      self,
      current_row: RawContactRow,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Validation
    Validate required fields (email/phone), formats, and maximum lengths.
    inputs:  currentRow (RawContactRow, context)
    outputs: rowValid (bool, context)
    """
    d = current_row.data
    email = d.get("email", "")
    phone = d.get("phone", "")

    # At least one of email or phone must be present
    if not email and not phone:
      ctx.row_valid = False
      return

    # Basic email format check
    if email and ("@" not in email or len(email) > 254):
      ctx.row_valid = False
      return

    # Maximum field lengths
    for field_name, max_len in [
      ("first_name", 100),
      ("last_name", 100),
      ("email", 254),
      ("phone", 30),
    ]:
      if len(d.get(field_name, "")) > max_len:
        ctx.row_valid = False
        return

    ctx.row_valid = True

  # ── Utility ───────────────────────────────────────────────────────────────

  async def add_row_validation_error(
      self,
      row_index: int,
      current_row: RawContactRow,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Utility
    Append row-level validation errors with a human-friendly message.
    inputs:  rowIndex (int, context), currentRow (RawContactRow, context)
    outputs: importErrors (ImportError[], context)
    """
    msg = (
      f"Row {current_row.row_number}: required field(s) missing or "
      f"invalid (email/phone must be present and well-formed)."
    )
    ctx.import_errors.append(ImportError(row_number=current_row.row_number, message=msg))

  # ── Filter ────────────────────────────────────────────────────────────────

  async def filter_duplicate_in_batch(
      self,
      current_row: RawContactRow,
      rows: list[RawContactRow],
      row_index: int,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Filter
    Skip duplicates within the current batch using email and phone as keys.
    inputs:  currentRow (RawContactRow, context), rows (RawContactRow[], context)
    outputs: notDuplicate (bool, context)
    """
    email = current_row.data.get("email", "")
    phone = current_row.data.get("phone", "")

    for i, row in enumerate(rows):
      if i >= row_index:  # only look at previous rows
        break
      prev_email = row.data.get("email", "")
      prev_phone = row.data.get("phone", "")
      # Duplicate if same non-empty email OR same non-empty phone
      if (email and email == prev_email) or (phone and phone == prev_phone):
        ctx.not_duplicate = False
        return

    ctx.not_duplicate = True

  # ── Utility ───────────────────────────────────────────────────────────────

  async def add_batch_duplicate_error(
      self,
      row_index: int,
      current_row: RawContactRow,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Utility
    Record a batch-duplicate error for the row and continue the loop.
    inputs:  rowIndex (int, context), currentRow (RawContactRow, context)
    outputs: importErrors (ImportError[], context)
    """
    msg = (
      f"Row {current_row.row_number}: duplicate within batch "
      f"(email or phone already seen at an earlier row)."
    )
    ctx.import_errors.append(ImportError(row_number=current_row.row_number, message=msg))

  # ── Mapper ────────────────────────────────────────────────────────────────

  async def map_row_to_contact(
      self,
      tenant_id: UUID,
      current_row: RawContactRow,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Mapper
    Convert row into domain Contact object using tenant defaults.
    inputs:  tenantId (UUID, request), currentRow (RawContactRow, context)
    outputs: contact (Contact, context)
    """
    d = current_row.data
    tags_raw = d.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    ctx.contact = Contact(
        tenant_id=tenant_id,
        email=d.get("email") or None,
        phone=d.get("phone") or None,
        first_name=d.get("first_name") or None,
        last_name=d.get("last_name") or None,
        tags=tags,
    )

  # ── Repository ────────────────────────────────────────────────────────────

  async def upsert_contact(
      self,
      tenant_id: UUID,
      contact: Contact,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Repository
    Upsert contact by email/phone and attach tags from import.
    inputs:  tenantId (UUID, request), contact (Contact, context)
    outputs: contactId (UUID, context)
    exceptions: Conflict
    sideEffects: true, idempotent: true
    """
    # Assumption: real implementation uses DB upsert (ON CONFLICT DO UPDATE).
    # Derive a stable ID from tenant + email/phone so repeated calls are idempotent.
    key_material = f"{tenant_id}:{contact.email or ''}:{contact.phone or ''}"
    contact_id = uuid.uuid5(tenant_id, key_material)

    self._contacts[contact_id] = {
      "tenant_id": str(tenant_id),
      "email": contact.email,
      "phone": contact.phone,
      "first_name": contact.first_name,
      "last_name": contact.last_name,
      "tags": contact.tags,
      "updated_at": datetime.utcnow().isoformat(),
    }
    ctx.contact_id = contact_id

  # ── Repository (transactional) ────────────────────────────────────────────

  async def persist_import_summary(
      self,
      tenant_id: UUID,
      import_errors: list[ImportError],
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Repository
    Persist import summary including counts and error list for later retrieval.
    inputs:  tenantId (UUID, request), importErrors (ImportError[], context)
    outputs: importId (UUID, context)
    sideEffects: true, idempotent: false
    consistencyScope: local-transaction, transactional: true
    """
    # Assumption: wrapping in a DB transaction; simulated here with a simple dict write.
    import_id = uuid.uuid4()
    self._imports[import_id] = {
      "tenant_id": str(tenant_id),
      "import_id": str(import_id),
      "total_errors": len(import_errors),
      "errors": [
        {"row": e.row_number, "message": e.message} for e in import_errors
      ],
      "created_at": datetime.utcnow().isoformat(),
    }
    # Also register idempotency key to prevent duplicate runs
    if ctx.validated_import:
      key = (tenant_id, ctx.validated_import.idempotency_key)
      self._used_idempotency_keys[key] = import_id

    ctx.import_id = import_id

  # ── Publisher ─────────────────────────────────────────────────────────────

  async def publish_import_completed(
      self,
      import_id: UUID,
      total_rows: int,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Publisher
    Publish ContactImportCompleted with importId and number of processed rows.
    inputs:  importId (UUID, context), totalRows (int, context)
    outputs: eventPublished (bool, event)
    sideEffects: true, idempotent: true, consistencyScope: eventual
    """
    # Assumption: in production this publishes to a message broker (e.g. Kafka / SNS).
    event = {
      "event_type": "ContactImportCompleted",
      "import_id": str(import_id),
      "total_rows": total_rows,
      "timestamp": datetime.utcnow().isoformat(),
    }
    logger.info("Publishing event: %s", event)
    # event_published stored to event target (not in context / response)

  # ── Mapper ────────────────────────────────────────────────────────────────

  async def build_accepted_response(
      self,
      import_id: UUID,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Mapper
    Build 202 response containing importId and a link to fetch results.
    inputs:  importId (UUID, context)
    outputs: response (ContactImportAcceptedResponse, response)
    """
    ctx.response = ContactImportAcceptedResponse(
        import_id=import_id,
        results_url=f"/contacts/imports/{import_id}/results",
    )

  # ── Utility ───────────────────────────────────────────────────────────────

  async def increment_row_index(
      self,
      row_index: int,
      ctx: ImportFlowContext,
  ) -> None:
    """
    Stereotype: Utility
    Increment row index and continue processing remaining rows.
    inputs:  rowIndex (int, context)
    outputs: rowIndex (int, context)
    """
    ctx.row_index = row_index + 1


# ──────────────────────────────────────────────────────────────────────────────
# Controller  (REST orchestration — exact step-by-step per the specification)
# ──────────────────────────────────────────────────────────────────────────────


class ContactImportController:
  def __init__(self, service: ImportService):
    self._svc = service

  async def bulk_import_contacts(
      self,
      tenant_id: UUID,
      import_request: ContactImportRequest,
      auth_token: str,
      idempotency_key: str,
  ) -> ContactImportAcceptedResponse:
    """
    POST /contacts/import
    Orchestrates main_Bulkimportcontacts() per the spec — deterministic, step-by-step.
    responseSuccess: 202 Accepted
    responseError:   400 | 401 | 403 | 409 | 422
    """
    ctx = ImportFlowContext()
    svc = self._svc

    # ── Step 1: AuthorizeOperator ─────────────────────────────────────────
    await svc.authorize_operator(auth_token, tenant_id, ctx)

    # ── Step 2: ValidateImportRequest ─────────────────────────────────────
    await svc.validate_import_request(import_request, idempotency_key, tenant_id, ctx)

    # ── Step 3: CheckImportIdempotency ────────────────────────────────────
    await svc.check_import_idempotency(tenant_id, idempotency_key, ctx)

    # ── Decision: importAlreadyExists ─────────────────────────────────────
    if ctx.existing_import_id is not None:
      # if yes branch
      # ── ReturnExistingImport ──────────────────────────────────────────
      await svc.return_existing_import(ctx.existing_import_id, ctx)
      # FlowJoinBulkImportContacts → end()
      return ctx.response  # type: ignore[return-value]

    # if no branch ─────────────────────────────────────────────────────────

    # ── LoadImportFile (with retry) ───────────────────────────────────────
    await svc.load_import_file(ctx.validated_import, ctx)  # type: ignore[arg-type]

    # ── ParseCsvRows ──────────────────────────────────────────────────────
    await svc.parse_csv_rows(ctx.file_bytes, ctx)  # type: ignore[arg-type]

    # ── InitializeImportCounters ──────────────────────────────────────────
    await svc.initialize_import_counters(ctx.total_rows, ctx)

    # ── FlowJoinProcessRow (loop entry) ───────────────────────────────────
    # Decision: rowIndexLessTotalRows  →  implements the per-row loop
    while ctx.row_index < ctx.total_rows:  # Decision: rowIndexLessTotalRows — yes branch

      # ── ReadNextRow ───────────────────────────────────────────────────
      await svc.read_next_row(ctx.rows, ctx.row_index, ctx)

      # ── ValidateRow ───────────────────────────────────────────────────
      await svc.validate_row(ctx.current_row, ctx)  # type: ignore[arg-type]

      # ── Decision: rowValid ────────────────────────────────────────────
      if not ctx.row_valid:
        # if false branch
        # ── AddRowValidationError ─────────────────────────────────────
        await svc.add_row_validation_error(ctx.row_index, ctx.current_row, ctx)  # type: ignore[arg-type]
        # FlowJoinProcessRow → FlowJoinAfterRow → IncrementRowIndex → loop
        await svc.increment_row_index(ctx.row_index, ctx)
        continue

      # if true branch ──────────────────────────────────────────────────

      # ── FilterDuplicateInBatch ────────────────────────────────────────
      await svc.filter_duplicate_in_batch(
          ctx.current_row, ctx.rows, ctx.row_index, ctx  # type: ignore[arg-type]
      )

      # ── Decision: notDuplicate ────────────────────────────────────────
      if not ctx.not_duplicate:
        # if false branch
        # ── AddBatchDuplicateError ────────────────────────────────────
        await svc.add_batch_duplicate_error(ctx.row_index, ctx.current_row, ctx)  # type: ignore[arg-type]
        # FlowJoinDuplicate → FlowJoinAfterRow → IncrementRowIndex → loop
        await svc.increment_row_index(ctx.row_index, ctx)
        continue

      # if true branch ──────────────────────────────────────────────────

      # ── MapRowToContact ───────────────────────────────────────────────
      await svc.map_row_to_contact(tenant_id, ctx.current_row, ctx)  # type: ignore[arg-type]

      # ── UpsertContact ─────────────────────────────────────────────────
      await svc.upsert_contact(tenant_id, ctx.contact, ctx)  # type: ignore[arg-type]

      # FlowJoinDuplicate → FlowJoinAfterRow → IncrementRowIndex → loop
      await svc.increment_row_index(ctx.row_index, ctx)

    # Decision: rowIndexLessTotalRows — no branch (all rows processed) ─────

    # ── PersistImportSummary (transactional) ──────────────────────────────
    await svc.persist_import_summary(tenant_id, ctx.import_errors, ctx)

    # ── PublishImportCompleted ────────────────────────────────────────────
    await svc.publish_import_completed(ctx.import_id, ctx.total_rows, ctx)  # type: ignore[arg-type]

    # ── BuildAcceptedResponse ─────────────────────────────────────────────
    await svc.build_accepted_response(ctx.import_id, ctx)  # type: ignore[arg-type]

    # FlowJoinBulkImportContacts → end()
    return ctx.response  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI Application Wiring
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Contact Import API")

_import_service = ImportService()
_controller = ContactImportController(_import_service)


def _exc_to_status(exc: Exception) -> int:
  mapping: dict[type[Exception], int] = {
    UnauthorizedException: status.HTTP_401_UNAUTHORIZED,
    ForbiddenException: status.HTTP_403_FORBIDDEN,
    BadRequestException: status.HTTP_400_BAD_REQUEST,
    ConflictException: status.HTTP_409_CONFLICT,
    FileNotFoundException: status.HTTP_422_UNPROCESSABLE_ENTITY,
    StorageUnavailableException: status.HTTP_503_SERVICE_UNAVAILABLE,
  }
  return mapping.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.post("/contacts/import", status_code=202)
async def bulk_import_contacts_endpoint(
    request: Request,
    import_request: ContactImportRequest,
    tenant_id: UUID = Header(..., alias="X-Tenant-Id"),
    authorization: str = Header(..., alias="Authorization"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> JSONResponse:
  """
  POST /contacts/import
  responseSuccess: 202 Accepted
  responseError:   400 | 401 | 403 | 409 | 422
  """
  try:
    result = await _controller.bulk_import_contacts(
        tenant_id=tenant_id,
        import_request=import_request,
        auth_token=authorization,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
          "import_id": str(result.import_id),
          "results_url": result.results_url,
        },
    )
  except (
      UnauthorizedException,
      ForbiddenException,
      BadRequestException,
      ConflictException,
      FileNotFoundException,
      StorageUnavailableException,
  ) as exc:
    http_status = _exc_to_status(exc)
    return JSONResponse(
        status_code=http_status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )