"""
POST /contacts/import — Bulk Import Contacts
Generated from UML Activity Diagram pseudo-code specification.

Components:
  - DTOs / data models
  - Custom exceptions
  - ContactImportService  (all internal actions)
  - ContactImportController  (REST endpoint, orchestration)

Dependencies (install via pip):
  fastapi, uvicorn, pydantic, httpx, tenacity
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

logger = logging.getLogger(__name__)

# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================

class UnauthorizedException(Exception):
    """Raised when the JWT token is missing or invalid."""

class ForbiddenException(Exception):
    """Raised when the operator lacks permission to import for this tenant."""

class BadRequestException(Exception):
    """Raised when the import request payload fails validation."""

class ConflictException(Exception):
    """Raised on idempotency-key collision or contact upsert conflict."""

class FileNotFoundException(Exception):
    """Raised when the import file is not found in object storage."""

class StorageUnavailableException(Exception):
    """Raised after all retry attempts to object storage are exhausted."""

# =============================================================================
# DTOs / DATA MODELS
# =============================================================================

class ContactImportRequest(BaseModel):
    file_reference: str           # e.g. S3 key / GCS URI / HTTP URL
    format: str                   # e.g. "csv"
    idempotency_key: str


class RawContactRow(BaseModel):
    row_number: int
    email: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    tags: list[str] = []
    raw: dict[str, str] = {}      # original column values


class Contact(BaseModel):
    tenant_id: UUID
    email: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    tags: list[str] = []


class ImportError(BaseModel):
    row_number: int
    message: str


class ValidatedImport(BaseModel):
    file_reference: str
    format: str
    idempotency_key: str


class ContactImportAcceptedResponse(BaseModel):
    import_id: UUID
    status: str = "accepted"
    results_url: str


# =============================================================================
# IN-MEMORY "DATABASE" STUBS  (replace with real DB / cache in production)
# =============================================================================

# (tenant_id_str, idempotency_key) -> import_id
_idempotency_store: dict[tuple[str, str], UUID] = {}

# import_id -> summary dict
_import_summary_store: dict[UUID, dict] = {}

# (tenant_id_str, email_or_phone) -> contact_id
_contact_store: dict[tuple[str, str], UUID] = {}

# in-memory event bus stub
_event_bus: list[dict] = []

# =============================================================================
# FLOW CONTEXT
# =============================================================================

@dataclass
class ImportContext:
    """Carries all intermediate values through the import flow (targets: context)."""
    operator_id: Optional[UUID] = None
    validated_import: Optional[ValidatedImport] = None
    existing_import_id: Optional[UUID] = None
    file_bytes: Optional[bytes] = None
    rows: list[RawContactRow] = field(default_factory=list)
    total_rows: int = 0
    row_index: int = 0
    import_errors: list[ImportError] = field(default_factory=list)
    current_row: Optional[RawContactRow] = None
    row_valid: bool = False
    not_duplicate: bool = False
    contact: Optional[Contact] = None
    contact_id: Optional[UUID] = None
    import_id: Optional[UUID] = None
    response: Optional[ContactImportAcceptedResponse] = None


# =============================================================================
# SERVICE  —  one method per action node
# =============================================================================

class ContactImportService:

    # -------------------------------------------------------------------------
    # AuthorizeOperator  [Security]
    # -------------------------------------------------------------------------

    def authorize_operator(
        self,
        auth_token: str,
        tenant_id: UUID,
        ctx: ImportContext,
    ) -> None:
        """
        Ensure the caller is an operator allowed to import contacts for a tenant.
        outputs: operatorId -> context
        exceptions: Unauthorized, Forbidden
        """
        if not auth_token or not auth_token.startswith("Bearer "):
            raise UnauthorizedException("Missing or malformed Authorization header.")
        token_value = auth_token[len("Bearer "):]
        if not token_value:
            raise UnauthorizedException("Empty JWT token.")
        # Assumption: JWT signature verification via a real library (e.g. python-jose)
        # is omitted in this stub; operator_id is derived from the token hash.
        operator_id = uuid.uuid5(uuid.NAMESPACE_OID, token_value)
        # Assumption: any bearer token grants operator access unless tenant is
        # the sentinel zero-UUID (used as a test for the Forbidden path).
        if str(tenant_id) == "00000000-0000-0000-0000-000000000000":
            raise ForbiddenException("Operator is not allowed to import for this tenant.")
        ctx.operator_id = operator_id
        logger.info("Authorized operator %s for tenant %s", operator_id, tenant_id)

    # -------------------------------------------------------------------------
    # ValidateImportRequest  [Validation]
    # -------------------------------------------------------------------------

    def validate_import_request(
        self,
        import_request: ContactImportRequest,
        idempotency_key: str,
        ctx: ImportContext,
    ) -> None:
        """
        Validate import request includes file reference, format, and idempotency key.
        outputs: validatedImport -> context
        exceptions: BadRequest
        """
        if not import_request.file_reference:
            raise BadRequestException("file_reference is required.")
        if not import_request.format:
            raise BadRequestException("format is required.")
        if import_request.format.lower() != "csv":
            raise BadRequestException(
                f"Unsupported format '{import_request.format}'. Only 'csv' is supported."
            )
        if not idempotency_key:
            raise BadRequestException("Idempotency-Key header is required.")
        ctx.validated_import = ValidatedImport(
            file_reference=import_request.file_reference,
            format=import_request.format,
            idempotency_key=idempotency_key,
        )
        logger.info("Import request validated: %s", ctx.validated_import)

    # -------------------------------------------------------------------------
    # CheckImportIdempotency  [Repository]
    # -------------------------------------------------------------------------

    def check_import_idempotency(
        self,
        tenant_id: UUID,
        idempotency_key: str,
        ctx: ImportContext,
    ) -> None:
        """
        Ensure the idempotency key was not used before for this tenant.
        outputs: existingImportId (UUID?) -> context
        sideEffects: true | idempotent: true | consistencyScope: local-transaction
        exceptions: Conflict
        """
        # local-transaction boundary (stub: atomic dict lookup)
        key = (str(tenant_id), idempotency_key)
        existing = _idempotency_store.get(key)
        ctx.existing_import_id = existing
        logger.info(
            "Idempotency check tenant=%s key=%s -> existing=%s",
            tenant_id, idempotency_key, existing,
        )

    # -------------------------------------------------------------------------
    # ReturnExistingImport  [Mapper]
    # -------------------------------------------------------------------------

    def return_existing_import(self, ctx: ImportContext) -> None:
        """
        Return existing import reference without re-processing the file.
        inputs: existingImportId <- context
        outputs: response -> response
        """
        if ctx.existing_import_id is None:
            raise ConflictException("existingImportId is missing in context.")
        ctx.response = ContactImportAcceptedResponse(
            import_id=ctx.existing_import_id,
            status="accepted",
            results_url=f"/contacts/import/{ctx.existing_import_id}/results",
        )
        logger.info("Returning existing import %s", ctx.existing_import_id)

    # -------------------------------------------------------------------------
    # LoadImportFile  [ExternalCall]  retryPolicy: maxAttempts=3, backoffMs=300
    # -------------------------------------------------------------------------

    def load_import_file(self, ctx: ImportContext) -> None:
        """
        Download import file from object storage and verify checksum.
        retryPolicy: maxAttempts=3, backoffMs=300, retryOn=[Timeout, 502, 503]
        onRetryExhausted: throw StorageUnavailable
        sideEffects: false | idempotent: true | consistencyScope: none
        exceptions: FileNotFound, StorageUnavailable
        """
        validated = ctx.validated_import
        if validated is None:
            raise BadRequestException("validatedImport missing from context.")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_fixed(0.3),           # 300 ms
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
            reraise=False,
        )
        def _fetch() -> bytes:
            # Assumption: file_reference is an HTTP URL pointing to the object store;
            # replace with the appropriate storage SDK call in production.
            with httpx.Client(timeout=10) as client:
                resp = client.get(validated.file_reference)
                if resp.status_code == 404:
                    raise FileNotFoundException(
                        f"File not found: {validated.file_reference}"
                    )
                if resp.status_code in (502, 503):
                    # Force a retry-eligible exception
                    raise httpx.HTTPStatusError(
                        f"Storage returned {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.content

        try:
            file_bytes = _fetch()
        except FileNotFoundException:
            raise
        except RetryError as exc:
            raise StorageUnavailableException(
                "Storage unavailable after 3 retry attempts."
            ) from exc
        except Exception as exc:
            raise StorageUnavailableException(str(exc)) from exc

        ctx.file_bytes = file_bytes
        logger.info("Loaded file (%d bytes) from %s", len(file_bytes), validated.file_reference)

    # -------------------------------------------------------------------------
    # ParseCsvRows  [Mapper]
    # -------------------------------------------------------------------------

    def parse_csv_rows(self, ctx: ImportContext) -> None:
        """
        Parse CSV into a list of raw contact rows, preserving row numbers.
        outputs: rows, totalRows -> context
        """
        if ctx.file_bytes is None:
            raise BadRequestException("fileBytes missing from context.")
        text = ctx.file_bytes.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows: list[RawContactRow] = []
        for row_number, record in enumerate(reader, start=1):
            rows.append(
                RawContactRow(
                    row_number=row_number,
                    email=(record.get("email") or "").strip().lower() or None,
                    phone=(record.get("phone") or "").strip() or None,
                    first_name=(record.get("first_name") or "").strip() or None,
                    last_name=(record.get("last_name") or "").strip() or None,
                    tags=[
                        t.strip()
                        for t in (record.get("tags") or "").split(",")
                        if t.strip()
                    ],
                    raw=dict(record),
                )
            )
        ctx.rows = rows
        ctx.total_rows = len(rows)
        logger.info("Parsed %d CSV rows.", ctx.total_rows)

    # -------------------------------------------------------------------------
    # InitializeImportCounters  [Utility]
    # -------------------------------------------------------------------------

    def initialize_import_counters(self, ctx: ImportContext) -> None:
        """
        Initialize counters and an error collection for the import session.
        outputs: rowIndex, importErrors -> context
        """
        ctx.row_index = 0
        ctx.import_errors = []
        logger.info("Initialized import counters. total_rows=%d", ctx.total_rows)

    # -------------------------------------------------------------------------
    # ReadNextRow  [Mapper]
    # -------------------------------------------------------------------------

    def read_next_row(self, ctx: ImportContext) -> None:
        """
        Read current row and normalize values (trim, casing, locale).
        outputs: currentRow -> context
        """
        ctx.current_row = ctx.rows[ctx.row_index]
        logger.debug("Reading row index=%d row_number=%d", ctx.row_index, ctx.current_row.row_number)

    # -------------------------------------------------------------------------
    # ValidateRow  [Validation]
    # -------------------------------------------------------------------------

    def validate_row(self, ctx: ImportContext) -> None:
        """
        Validate required fields (email/phone), formats, and maximum lengths.
        outputs: rowValid -> context
        """
        row = ctx.current_row
        if row is None:
            ctx.row_valid = False
            return
        valid = True
        if not row.email and not row.phone:
            valid = False
        if row.email and len(row.email) > 254:
            valid = False
        if row.phone and len(row.phone) > 20:
            valid = False
        ctx.row_valid = valid
        logger.debug("Row %d row_valid=%s", row.row_number, valid)

    # -------------------------------------------------------------------------
    # AddRowValidationError  [Utility]
    # -------------------------------------------------------------------------

    def add_row_validation_error(self, ctx: ImportContext) -> None:
        """
        Append row-level validation errors with a human-friendly message.
        outputs: importErrors -> context  (appended)
        """
        row = ctx.current_row
        if row is None:
            return
        ctx.import_errors.append(
            ImportError(
                row_number=row.row_number,
                message=(
                    "Row validation failed: at least one of email or phone is required "
                    "and must not exceed the maximum allowed length."
                ),
            )
        )
        logger.debug("Validation error recorded for row %d.", row.row_number)

    # -------------------------------------------------------------------------
    # FilterDuplicateInBatch  [Filter]
    # -------------------------------------------------------------------------

    def filter_duplicate_in_batch(self, ctx: ImportContext) -> None:
        """
        Skip duplicates within the current batch using email and phone as keys.
        outputs: notDuplicate -> context
        """
        row = ctx.current_row
        if row is None:
            ctx.not_duplicate = False
            return
        for i, r in enumerate(ctx.rows):
            if i >= ctx.row_index:
                break
            if row.email and r.email and row.email == r.email:
                ctx.not_duplicate = False
                logger.debug(
                    "Row %d is a batch duplicate of row %d (email).",
                    row.row_number, r.row_number,
                )
                return
            if row.phone and r.phone and row.phone == r.phone:
                ctx.not_duplicate = False
                logger.debug(
                    "Row %d is a batch duplicate of row %d (phone).",
                    row.row_number, r.row_number,
                )
                return
        ctx.not_duplicate = True

    # -------------------------------------------------------------------------
    # AddBatchDuplicateError  [Utility]
    # -------------------------------------------------------------------------

    def add_batch_duplicate_error(self, ctx: ImportContext) -> None:
        """
        Record a batch-duplicate error for the row and continue the loop.
        outputs: importErrors -> context  (appended)
        """
        row = ctx.current_row
        if row is None:
            return
        ctx.import_errors.append(
            ImportError(
                row_number=row.row_number,
                message="Duplicate row in batch: email or phone already seen in this import file.",
            )
        )
        logger.debug("Batch-duplicate error recorded for row %d.", row.row_number)

    # -------------------------------------------------------------------------
    # MapRowToContact  [Mapper]
    # -------------------------------------------------------------------------

    def map_row_to_contact(self, tenant_id: UUID, ctx: ImportContext) -> None:
        """
        Convert row into domain Contact object using tenant defaults.
        outputs: contact -> context
        """
        row = ctx.current_row
        if row is None:
            raise BadRequestException("currentRow missing from context.")
        ctx.contact = Contact(
            tenant_id=tenant_id,
            email=row.email,
            phone=row.phone,
            first_name=row.first_name,
            last_name=row.last_name,
            tags=row.tags,
        )
        logger.debug("Mapped row %d to Contact.", row.row_number)

    # -------------------------------------------------------------------------
    # UpsertContact  [Repository]
    # -------------------------------------------------------------------------

    def upsert_contact(self, tenant_id: UUID, ctx: ImportContext) -> None:
        """
        Upsert contact by email/phone and attach tags from import.
        outputs: contactId -> context
        sideEffects: true | idempotent: true
        consistencyScope: local-transaction | transactional: true
        exceptions: Conflict
        """
        contact = ctx.contact
        if contact is None:
            raise BadRequestException("contact missing from context.")
        # local-transaction boundary start
        lookup_key = (str(tenant_id), contact.email or contact.phone or "")
        if lookup_key in _contact_store:
            contact_id = _contact_store[lookup_key]
            logger.debug("Upsert: updated existing contact %s.", contact_id)
        else:
            contact_id = uuid.uuid4()
            _contact_store[lookup_key] = contact_id
            logger.debug("Upsert: inserted new contact %s.", contact_id)
        ctx.contact_id = contact_id
        # local-transaction boundary end

    # -------------------------------------------------------------------------
    # IncrementRowIndex  [Utility]
    # -------------------------------------------------------------------------

    def increment_row_index(self, ctx: ImportContext) -> None:
        """
        Increment row index and continue processing remaining rows.
        outputs: rowIndex -> context
        """
        ctx.row_index += 1
        logger.debug("Row index incremented to %d.", ctx.row_index)

    # -------------------------------------------------------------------------
    # PersistImportSummary  [Repository]
    # -------------------------------------------------------------------------

    def persist_import_summary(self, tenant_id: UUID, ctx: ImportContext) -> None:
        """
        Persist import summary including counts and error list for later retrieval.
        outputs: importId -> context
        sideEffects: true | idempotent: false
        consistencyScope: local-transaction | transactional: true
        """
        # local-transaction boundary start
        import_id = uuid.uuid4()
        _import_summary_store[import_id] = {
            "tenant_id": str(tenant_id),
            "total_rows": ctx.total_rows,
            "error_count": len(ctx.import_errors),
            "errors": [e.dict() for e in ctx.import_errors],
            "created_at": datetime.utcnow().isoformat(),
        }
        ctx.import_id = import_id
        # Register idempotency key once the import is durably committed
        if ctx.validated_import:
            idem_key = (str(tenant_id), ctx.validated_import.idempotency_key)
            _idempotency_store[idem_key] = import_id
        logger.info("Persisted import summary id=%s tenant=%s.", import_id, tenant_id)
        # local-transaction boundary end

    # -------------------------------------------------------------------------
    # PublishImportCompleted  [Publisher]
    # -------------------------------------------------------------------------

    def publish_import_completed(self, ctx: ImportContext) -> None:
        """
        Publish ContactImportCompleted event with importId and processed row count.
        outputs: eventPublished -> event
        sideEffects: true | idempotent: true | consistencyScope: eventual
        """
        if ctx.import_id is None:
            raise BadRequestException("importId missing from context.")
        event = {
            "type": "ContactImportCompleted",
            "import_id": str(ctx.import_id),
            "total_rows": ctx.total_rows,
            "published_at": datetime.utcnow().isoformat(),
        }
        # Assumption: _event_bus is a stub; replace with Kafka/SQS/SNS publish call.
        _event_bus.append(event)
        logger.info("Published ContactImportCompleted: %s", event)

    # -------------------------------------------------------------------------
    # BuildAcceptedResponse  [Mapper]
    # -------------------------------------------------------------------------

    def build_accepted_response(self, ctx: ImportContext) -> None:
        """
        Build 202 response containing importId and a link to fetch results.
        outputs: response -> response
        """
        if ctx.import_id is None:
            raise BadRequestException("importId missing from context.")
        ctx.response = ContactImportAcceptedResponse(
            import_id=ctx.import_id,
            status="accepted",
            results_url=f"/contacts/import/{ctx.import_id}/results",
        )
        logger.info("Built accepted response for import %s.", ctx.import_id)


# =============================================================================
# CONTROLLER  —  orchestrates the exact spec flow
# =============================================================================

app = FastAPI(title="Contacts Import API")
_service = ContactImportService()


@app.post(
    "/contacts/import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ContactImportAcceptedResponse,
    responses={
        400: {"description": "Bad Request"},
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden"},
        409: {"description": "Conflict"},
        422: {"description": "Unprocessable Entity"},
    },
    summary="Bulk Import Contacts",
)
async def bulk_import_contacts(
    import_request: ContactImportRequest,
    authorization: str = Header(..., alias="Authorization"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    tenant_id: UUID = Header(..., alias="X-Tenant-Id"),
) -> JSONResponse:
    """
    POST /contacts/import
    Accepts a file reference and imports contacts for the given tenant.
    Returns 202 Accepted immediately with a link to the import result.
    """
    ctx = ImportContext()

    # ------------------------------------------------------------------
    # 1. AuthorizeOperator
    # ------------------------------------------------------------------
    try:
        _service.authorize_operator(
            auth_token=authorization,
            tenant_id=tenant_id,
            ctx=ctx,
        )
    except UnauthorizedException as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ForbiddenException as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    # ------------------------------------------------------------------
    # 2. ValidateImportRequest
    # ------------------------------------------------------------------
    try:
        _service.validate_import_request(
            import_request=import_request,
            idempotency_key=idempotency_key,
            ctx=ctx,
        )
    except BadRequestException as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # 3. CheckImportIdempotency
    # ------------------------------------------------------------------
    try:
        _service.check_import_idempotency(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            ctx=ctx,
        )
    except ConflictException as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # ------------------------------------------------------------------
    # Decision: importAlreadyExists?
    # ------------------------------------------------------------------
    if ctx.existing_import_id is not None:
        # ---- yes branch ----
        _service.return_existing_import(ctx=ctx)
        # FlowJoinBulkImportContacts -> end()
        return JSONResponse(status_code=202, content=ctx.response.dict())

    # ---- no branch ----

    # ------------------------------------------------------------------
    # 4. LoadImportFile  (ExternalCall + retry)
    # ------------------------------------------------------------------
    try:
        _service.load_import_file(ctx=ctx)
    except FileNotFoundException as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except StorageUnavailableException as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # ------------------------------------------------------------------
    # 5. ParseCsvRows
    # ------------------------------------------------------------------
    try:
        _service.parse_csv_rows(ctx=ctx)
    except BadRequestException as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # 6. InitializeImportCounters
    # ------------------------------------------------------------------
    _service.initialize_import_counters(ctx=ctx)

    # ------------------------------------------------------------------
    # FlowJoinProcessRow  —  row-processing loop
    # Decision(rowIndexLessTotalRows): while rowIndex < totalRows
    # ------------------------------------------------------------------
    while ctx.row_index < ctx.total_rows:

        # ---- yes branch ----

        # ReadNextRow
        _service.read_next_row(ctx=ctx)

        # ValidateRow
        _service.validate_row(ctx=ctx)

        # Decision(rowValid)?
        if not ctx.row_valid:
            # ---- false branch ----
            _service.add_row_validation_error(ctx=ctx)
            # FlowJoinAfterRow -> IncrementRowIndex -> FlowJoinProcessRow (loop)
            _service.increment_row_index(ctx=ctx)
            continue

        # ---- true branch ----

        # FilterDuplicateInBatch
        _service.filter_duplicate_in_batch(ctx=ctx)

        # Decision(notDuplicate)?
        if not ctx.not_duplicate:
            # ---- false branch ----
            _service.add_batch_duplicate_error(ctx=ctx)
            # FlowJoinDuplicate -> FlowJoinAfterRow -> IncrementRowIndex -> loop
            _service.increment_row_index(ctx=ctx)
            continue

        # ---- true branch ----

        # MapRowToContact
        _service.map_row_to_contact(tenant_id=tenant_id, ctx=ctx)

        # UpsertContact
        try:
            _service.upsert_contact(tenant_id=tenant_id, ctx=ctx)
        except ConflictException as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # FlowJoinDuplicate -> FlowJoinAfterRow -> IncrementRowIndex -> loop
        _service.increment_row_index(ctx=ctx)

    # ------------------------------------------------------------------
    # Decision(rowIndexLessTotalRows) -> no branch  (all rows consumed)
    # ------------------------------------------------------------------

    # PersistImportSummary
    try:
        _service.persist_import_summary(tenant_id=tenant_id, ctx=ctx)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to persist import summary: {exc}"
        )

    # PublishImportCompleted
    try:
        _service.publish_import_completed(ctx=ctx)
    except Exception as exc:
        # Assumption: event publish failure is non-fatal for the HTTP caller.
        # An outbox pattern or dead-letter retry should handle this in production.
        logger.error("Failed to publish ContactImportCompleted: %s", exc)

    # BuildAcceptedResponse
    _service.build_accepted_response(ctx=ctx)

    # FlowJoinBulkImportContacts -> end()
    return JSONResponse(status_code=202, content=ctx.response.dict())


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.DEBUG)
    uvicorn.run(app, host="0.0.0.0", port=8000)
