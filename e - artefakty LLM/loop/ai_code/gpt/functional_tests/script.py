from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Path, Body, status
from pydantic import BaseModel, Field


# ============================================================
# Logging
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bulk_import_contacts")


# ============================================================
# REST META
# REST DEFINITION   # @meta {"endpoint": "POST /contacts/import", "responseSuccess": "202 Accepted", "responseError": "400|401|403|409|422"}
# ============================================================


# ============================================================
# Exceptions generated from @meta.exceptions
# ============================================================

class ApplicationError(Exception):
  status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
  error_code: str = "ApplicationError"

  def __init__(self, message: str):
    super().__init__(message)
    self.message = message


class BadRequest(ApplicationError):
  status_code = status.HTTP_400_BAD_REQUEST
  error_code = "BadRequest"


class Unauthorized(ApplicationError):
  status_code = status.HTTP_401_UNAUTHORIZED
  error_code = "Unauthorized"


class Forbidden(ApplicationError):
  status_code = status.HTTP_403_FORBIDDEN
  error_code = "Forbidden"


class Conflict(ApplicationError):
  status_code = status.HTTP_409_CONFLICT
  error_code = "Conflict"


class FileNotFound(ApplicationError):
  status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
  error_code = "FileNotFound"


class StorageUnavailable(ApplicationError):
  status_code = status.HTTP_503_SERVICE_UNAVAILABLE
  error_code = "StorageUnavailable"


# Retry-on labels from spec
class RetryableTimeoutError(Exception):
  pass


class Retryable502Error(Exception):
  pass


class Retryable503Error(Exception):
  pass


# ============================================================
# DTOs / Models
# ============================================================

class ImportFormat(str, Enum):
  CSV = "csv"


class ContactImportRequest(BaseModel):
  file_ref: str = Field(..., description="Object storage file reference")
  format: ImportFormat = Field(..., description="Import format")
  checksum_sha256: str = Field(..., description="Expected SHA256 checksum")


class ContactImportAcceptedResponse(BaseModel):
  import_id: UUID
  results_url: str


class ValidatedImport(BaseModel):
  file_ref: str
  format: ImportFormat
  checksum_sha256: str


class RawContactRow(BaseModel):
  row_number: int
  email: Optional[str] = None
  phone: Optional[str] = None
  first_name: Optional[str] = None
  last_name: Optional[str] = None
  locale: Optional[str] = None
  tags: List[str] = Field(default_factory=list)


class Contact(BaseModel):
  tenant_id: UUID
  email: Optional[str] = None
  phone: Optional[str] = None
  first_name: Optional[str] = None
  last_name: Optional[str] = None
  locale: Optional[str] = None
  tags: List[str] = Field(default_factory=list)


class ImportErrorItem(BaseModel):
  row_index: int
  message: str


@dataclass
class BulkImportContext:
  operator_id: Optional[UUID] = None
  validated_import: Optional[ValidatedImport] = None
  existing_import_id: Optional[UUID] = None
  file_bytes: Optional[bytes] = None
  rows: List[RawContactRow] = field(default_factory=list)
  total_rows: int = 0
  row_index: int = 0
  import_errors: List[ImportErrorItem] = field(default_factory=list)
  current_row: Optional[RawContactRow] = None
  row_valid: Optional[bool] = None
  not_duplicate: Optional[bool] = None
  contact: Optional[Contact] = None
  contact_id: Optional[UUID] = None
  import_id: Optional[UUID] = None
  response: Optional[ContactImportAcceptedResponse] = None


# ============================================================
# Request DTO used by controller for parsed request values
# ============================================================

@dataclass
class BulkImportRequestEnvelope:
  tenant_id: UUID
  auth_token: str
  idempotency_key: str
  import_request: ContactImportRequest


# ============================================================
# Infrastructure helpers
# ============================================================

class AuditLogger:
  def log(self, action: str, details: Dict[str, Any]) -> None:
    logger.info("AUDIT %s %s", action, details)


class RetryPolicyExecutor:
  async def execute(
      self,
      func,
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
        result = func()
        if asyncio.iscoroutine(result):
          result = await result
        return result
      except Exception as exc:
        should_retry = self._matches_retry(exc, retry_on)
        if not should_retry or attempt >= max_attempts:
          if on_retry_exhausted == "throw_StorageUnavailable":
            raise StorageUnavailable("Storage access retry policy exhausted") from exc
          raise
        await asyncio.sleep(backoff_ms / 1000.0)

  @staticmethod
  def _matches_retry(exc: Exception, retry_on: List[str]) -> bool:
    exc_name = exc.__class__.__name__
    return exc_name in {"RetryableTimeoutError", "Retryable502Error", "Retryable503Error"} and (
        "Timeout" in retry_on or "502" in retry_on or "503" in retry_on
    )


class InMemoryTransactionManager:
  @contextmanager
  def transaction(self, name: str):
    logger.info("BEGIN TRANSACTION: %s", name)
    try:
      yield
      logger.info("COMMIT TRANSACTION: %s", name)
    except Exception:
      logger.info("ROLLBACK TRANSACTION: %s", name)
      raise


# ============================================================
# In-memory stores for self-contained demo
# ============================================================

class InMemoryDatabase:
  def __init__(self):
    self.idempotency_store: Dict[tuple[UUID, str], UUID] = {}
    self.contacts_by_tenant_key: Dict[tuple[UUID, str], UUID] = {}
    self.contact_records: Dict[UUID, Contact] = {}
    self.import_summaries: Dict[UUID, Dict[str, Any]] = {}

  @staticmethod
  def _contact_key(email: Optional[str], phone: Optional[str]) -> str:
    return f"{(email or '').lower()}|{phone or ''}"


class InMemoryObjectStorage:
  def __init__(self):
    self.objects: Dict[str, bytes] = {}

  def put_object(self, file_ref: str, content: bytes) -> None:
    self.objects[file_ref] = content

  def get_object(self, file_ref: str) -> bytes:
    if file_ref not in self.objects:
      raise FileNotFound(f"Import file '{file_ref}' not found")
    return self.objects[file_ref]


class InMemoryEventBus:
  def __init__(self):
    self.events: List[Dict[str, Any]] = []

  def publish(self, event_name: str, payload: Dict[str, Any]) -> bool:
    self.events.append({"event_name": event_name, "payload": payload, "published_at": time.time()})
    logger.info("Published event %s payload=%s", event_name, payload)
    return True


# ============================================================
# Services
# ============================================================

class SecurityService:
  def __init__(self, audit_logger: AuditLogger):
    self.audit_logger = audit_logger

  def AuthorizeOperator(self, auth_token: str, tenant_id: UUID) -> UUID:
    """
    @meta {"stereotype": "Security", "desc": "Ensure the caller is an operator allowed to import contacts for a tenant.", "inputs": [{"name": "authToken", "type": "JWT", "source": "request"}, {"name": "tenantId", "type": "UUID", "source": "request"}], "outputs": [{"name": "operatorId", "type": "UUID", "target": "context"}], "exceptions": ["Unauthorized", "Forbidden"]}
    """
    if not auth_token or not auth_token.startswith("Bearer "):
      raise Unauthorized("Missing or invalid authorization token")
    token_value = auth_token.removeprefix("Bearer ").strip()
    if not token_value:
      raise Unauthorized("Empty bearer token")
    # Assumption: token format "operator:<uuid>" for self-contained demo.
    if not token_value.startswith("operator:"):
      raise Forbidden("Caller is not an operator allowed to import contacts")
    operator_raw = token_value.split("operator:", 1)[1].strip()
    try:
      operator_id = UUID(operator_raw)
    except ValueError as exc:
      raise Unauthorized("Malformed operator token") from exc

    self.audit_logger.log("AuthorizeOperator", {"tenantId": str(tenant_id), "operatorId": str(operator_id)})
    return operator_id


class ValidationService:
  def ValidateImportRequest(
      self, import_request: ContactImportRequest, idempotency_key: str
  ) -> ValidatedImport:
    """
    @meta {"stereotype": "Validation", "desc": "Validate import request includes file reference, format, and idempotency key.", "inputs": [{"name": "importRequest", "type": "ContactImportRequest", "source": "request"}, {"name": "idempotencyKey", "type": "String", "source": "request"}], "outputs": [{"name": "validatedImport", "type": "ValidatedImport", "target": "context"}], "exceptions": ["BadRequest"]}
    """
    if import_request is None:
      raise BadRequest("Import request body is required")
    if not import_request.file_ref:
      raise BadRequest("file_ref is required")
    if import_request.format != ImportFormat.CSV:
      raise BadRequest("Only csv format is supported")
    if not import_request.checksum_sha256:
      raise BadRequest("checksum_sha256 is required")
    if not idempotency_key or not idempotency_key.strip():
      raise BadRequest("Idempotency key is required")
    return ValidatedImport(
        file_ref=import_request.file_ref,
        format=import_request.format,
        checksum_sha256=import_request.checksum_sha256,
    )

  def ValidateRow(self, current_row: RawContactRow) -> bool:
    """
    @meta {"stereotype": "Validation", "desc": "Validate required fields (email/phone), formats, and maximum lengths for the row.", "inputs": [{"name": "currentRow", "type": "RawContactRow", "source": "context"}], "outputs": [{"name": "rowValid", "type": "bool", "target": "context"}]}
    """
    if current_row is None:
      return False
    if not (current_row.email or current_row.phone):
      return False
    if current_row.email and "@" not in current_row.email:
      return False
    if current_row.first_name and len(current_row.first_name) > 100:
      return False
    if current_row.last_name and len(current_row.last_name) > 100:
      return False
    if current_row.phone and len(current_row.phone) > 30:
      return False
    return True


class RepositoryService:
  def __init__(self, db: InMemoryDatabase, tx_manager: InMemoryTransactionManager):
    self.db = db
    self.tx_manager = tx_manager

  def CheckImportIdempotency(self, tenant_id: UUID, idempotency_key: str) -> Optional[UUID]:
    """
    @meta {"stereotype": "Repository", "desc": "Ensure the idempotency key was not used before for this tenant to avoid duplicate imports.", "inputs": [{"name": "tenantId", "type": "UUID", "source": "request"}, {"name": "idempotencyKey", "type": "String", "source": "request"}], "outputs": [{"name": "existingImportId", "type": "UUID?", "target": "context"}], "exceptions": ["Conflict"], "sideEffects": true, "idempotent": true, "consistencyScope": "local-transaction"}
    """
    if not tenant_id:
      raise Conflict("tenantId is required for idempotency check")
    if not idempotency_key:
      raise Conflict("idempotencyKey is required for idempotency check")
    return self.db.idempotency_store.get((tenant_id, idempotency_key))

  def store_idempotency_record(self, tenant_id: UUID, idempotency_key: str, import_id: UUID) -> None:
    with self.tx_manager.transaction("store_idempotency_record"):
      self.db.idempotency_store[(tenant_id, idempotency_key)] = import_id

  def UpsertContact(self, tenant_id: UUID, contact: Contact) -> UUID:
    """
    @meta {"stereotype": "Repository", "desc": "Upsert contact by email/phone and attach tags from import.", "inputs": [{"name": "tenantId", "type": "UUID", "source": "request"}, {"name": "contact", "type": "Contact", "source": "context"}], "outputs": [{"name": "contactId", "type": "UUID", "target": "context"}], "exceptions": ["Conflict"], "sideEffects": true, "idempotent": true, "consistencyScope": "local-transaction", "transactional": true}
    """
    if contact is None:
      raise Conflict("Contact cannot be null for upsert")
    with self.tx_manager.transaction("UpsertContact"):
      key = self.db._contact_key(contact.email, contact.phone)
      existing_id = self.db.contacts_by_tenant_key.get((tenant_id, key))
      if existing_id:
        existing = self.db.contact_records[existing_id]
        existing.first_name = contact.first_name
        existing.last_name = contact.last_name
        existing.locale = contact.locale
        existing.tags = list(sorted(set(existing.tags + contact.tags)))
        self.db.contact_records[existing_id] = existing
        return existing_id

      contact_id = uuid4()
      self.db.contacts_by_tenant_key[(tenant_id, key)] = contact_id
      self.db.contact_records[contact_id] = contact
      return contact_id

  def PersistImportSummary(self, tenant_id: UUID, import_errors: List[ImportErrorItem]) -> UUID:
    """
    @meta {"stereotype": "Repository", "desc": "Persist import summary including counts and error list for later retrieval.", "inputs": [{"name": "tenantId", "type": "UUID", "source": "request"}, {"name": "importErrors", "type": "ImportError[]", "source": "context"}], "outputs": [{"name": "importId", "type": "UUID", "target": "context"}], "sideEffects": true, "idempotent": false, "consistencyScope": "local-transaction", "transactional": true}
    """
    with self.tx_manager.transaction("PersistImportSummary"):
      import_id = uuid4()
      self.db.import_summaries[import_id] = {
        "tenantId": tenant_id,
        "errors": [item.model_dump() for item in import_errors],
        "createdAt": time.time(),
      }
      return import_id


class ExternalCallService:
  def __init__(self, storage: InMemoryObjectStorage, retry_executor: RetryPolicyExecutor):
    self.storage = storage
    self.retry_executor = retry_executor

  async def LoadImportFile(self, validated_import: ValidatedImport) -> bytes:
    """
    @meta {"stereotype": "ExternalCall", "desc": "Download import file from object storage and verify checksum.", "inputs": [{"name": "validatedImport", "type": "ValidatedImport", "source": "context"}], "outputs": [{"name": "fileBytes", "type": "byte[]", "target": "context"}], "retryPolicy": {"maxAttempts": 3, "backoffMs": 300, "retryOn": ["Timeout", "502", "503"], "onRetryExhausted": "throw_StorageUnavailable"}, "exceptions": ["FileNotFound", "StorageUnavailable"], "sideEffects": false, "idempotent": true, "consistencyScope": "none"}
    """
    if validated_import is None:
      raise StorageUnavailable("validatedImport is required")

    async def _download_and_verify() -> bytes:
      data = self.storage.get_object(validated_import.file_ref)
      checksum = hashlib.sha256(data).hexdigest()
      if checksum != validated_import.checksum_sha256:
        raise BadRequest("Checksum verification failed")
      return data

    return await self.retry_executor.execute(
        _download_and_verify,
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="throw_StorageUnavailable",
    )


class MapperService:
  def ParseCsvRows(self, file_bytes: bytes) -> tuple[List[RawContactRow], int]:
    """
    @meta {"stereotype": "Mapper", "desc": "Parse CSV into a list of raw contact rows, preserving row numbers for error reporting.", "inputs": [{"name": "fileBytes", "type": "byte[]", "source": "context"}], "outputs": [{"name": "rows", "type": "RawContactRow[]", "target": "context"}, {"name": "totalRows", "type": "int", "target": "context"}]}
    """
    if file_bytes is None:
      raise BadRequest("fileBytes is required")
    decoded = file_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded))
    rows: List[RawContactRow] = []
    for idx, item in enumerate(reader, start=1):
      tags_value = item.get("tags") or ""
      tags = [x.strip() for x in tags_value.split("|") if x.strip()]
      rows.append(
          RawContactRow(
              row_number=idx,
              email=item.get("email"),
              phone=item.get("phone"),
              first_name=item.get("first_name"),
              last_name=item.get("last_name"),
              locale=item.get("locale"),
              tags=tags,
          )
      )
    return rows, len(rows)

  def ReadNextRow(self, rows: List[RawContactRow], row_index: int) -> RawContactRow:
    """
    @meta {"stereotype": "Mapper", "desc": "Read current row and normalize values (trim, casing, locale).", "inputs": [{"name": "rows", "type": "RawContactRow[]", "source": "context"}, {"name": "rowIndex", "type": "int", "source": "context"}], "outputs": [{"name": "currentRow", "type": "RawContactRow", "target": "context"}]}
    """
    if row_index < 0 or row_index >= len(rows):
      raise BadRequest("rowIndex out of bounds")
    source = rows[row_index]
    return RawContactRow(
        row_number=source.row_number,
        email=source.email.strip().lower() if source.email else None,
        phone=source.phone.strip() if source.phone else None,
        first_name=source.first_name.strip() if source.first_name else None,
        last_name=source.last_name.strip() if source.last_name else None,
        locale=source.locale.strip().lower() if source.locale else None,
        tags=[tag.strip() for tag in source.tags],
    )

  def MapRowToContact(self, tenant_id: UUID, current_row: RawContactRow) -> Contact:
    """
    @meta {"stereotype": "Mapper", "desc": "Convert row into domain Contact object using tenant defaults.", "inputs": [{"name": "tenantId", "type": "UUID", "source": "request"}, {"name": "currentRow", "type": "RawContactRow", "source": "context"}], "outputs": [{"name": "contact", "type": "Contact", "target": "context"}]}
    """
    if current_row is None:
      raise BadRequest("currentRow is required")
    # Assumption: tenant default locale falls back to "en" when absent because spec does not define the default source.
    return Contact(
        tenant_id=tenant_id,
        email=current_row.email,
        phone=current_row.phone,
        first_name=current_row.first_name,
        last_name=current_row.last_name,
        locale=current_row.locale or "en",
        tags=current_row.tags,
    )

  def ReturnExistingImport(self, existing_import_id: UUID) -> ContactImportAcceptedResponse:
    """
    @meta {"stereotype": "Mapper", "desc": "Return existing import reference without processing the file again.", "inputs": [{"name": "existingImportId", "type": "UUID", "source": "context"}], "outputs": [{"name": "response", "type": "ContactImportAcceptedResponse", "target": "response"}]}
    """
    if existing_import_id is None:
      raise BadRequest("existingImportId is required")
    return ContactImportAcceptedResponse(
        import_id=existing_import_id,
        results_url=f"/contacts/imports/{existing_import_id}",
    )

  def BuildAcceptedResponse(self, import_id: UUID) -> ContactImportAcceptedResponse:
    """
    @meta {"stereotype": "Mapper", "desc": "Build 202 response containing importId and a link to fetch results.", "inputs": [{"name": "importId", "type": "UUID", "source": "context"}], "outputs": [{"name": "response", "type": "ContactImportAcceptedResponse", "target": "response"}]}
    """
    if import_id is None:
      raise BadRequest("importId is required")
    return ContactImportAcceptedResponse(
        import_id=import_id,
        results_url=f"/contacts/imports/{import_id}",
    )


class UtilityService:
  def __init__(self, audit_logger: AuditLogger):
    self.audit_logger = audit_logger

  def InitializeImportCounters(self, total_rows: int) -> tuple[int, List[ImportErrorItem]]:
    """
    @meta {"stereotype": "Utility", "desc": "Initialize counters and an error collection for the import session.", "inputs": [{"name": "totalRows", "type": "int", "source": "context"}], "outputs": [{"name": "rowIndex", "type": "int", "target": "context"}, {"name": "importErrors", "type": "ImportError[]", "target": "context"}]}
    """
    if total_rows < 0:
      raise BadRequest("totalRows cannot be negative")
    return 0, []

  def AddRowValidationError(
      self, row_index: int, current_row: RawContactRow, import_errors: List[ImportErrorItem]
  ) -> List[ImportErrorItem]:
    """
    @meta {"stereotype": "Utility", "desc": "Append row-level validation errors with a human-friendly message.", "inputs": [{"name": "rowIndex", "type": "int", "source": "context"}, {"name": "currentRow", "type": "RawContactRow", "source": "context"}], "outputs": [{"name": "importErrors", "type": "ImportError[]", "target": "context"}]}
    """
    message = (
      f"Row {row_index + 1} failed validation: at least one valid email or phone is required, "
      f"and field lengths/formats must be valid."
    )
    import_errors.append(ImportErrorItem(row_index=row_index, message=message))
    return import_errors

  def AddBatchDuplicateError(
      self, row_index: int, current_row: RawContactRow, import_errors: List[ImportErrorItem]
  ) -> List[ImportErrorItem]:
    """
    @meta {"stereotype": "Utility", "desc": "Record a batch-duplicate error for the row and continue the loop.", "inputs": [{"name": "rowIndex", "type": "int", "source": "context"}, {"name": "currentRow", "type": "RawContactRow", "source": "context"}], "outputs": [{"name": "importErrors", "type": "ImportError[]", "target": "context"}]}
    """
    message = f"Row {row_index + 1} is a duplicate within the batch."
    import_errors.append(ImportErrorItem(row_index=row_index, message=message))
    return import_errors

  def IncrementRowIndex(self, row_index: int) -> int:
    """
    @meta {"stereotype": "Utility", "desc": "Increment row index and continue processing remaining rows.", "inputs": [{"name": "rowIndex", "type": "int", "source": "context"}], "outputs": [{"name": "rowIndex", "type": "int", "target": "context"}]}
    """
    return row_index + 1


class FilterService:
  def __init__(self):
    self.seen_keys: set[str] = set()

  def FilterDuplicateInBatch(self, current_row: RawContactRow, rows: List[RawContactRow]) -> bool:
    """
    @meta {"stereotype": "Filter", "desc": "Skip duplicates within the current batch using email and phone as keys.", "inputs": [{"name": "currentRow", "type": "RawContactRow", "source": "context"}, {"name": "rows", "type": "RawContactRow[]", "source": "context"}], "outputs": [{"name": "notDuplicate", "type": "bool", "target": "context"}]}
    """
    # Assumption: duplicate detection is based on rows already processed in the current loop order.
    key = f"{(current_row.email or '').lower()}|{current_row.phone or ''}"
    if key in self.seen_keys:
      return False
    self.seen_keys.add(key)
    return True


class PublisherService:
  def __init__(self, event_bus: InMemoryEventBus):
    self.event_bus = event_bus

  def PublishImportCompleted(self, import_id: UUID, total_rows: int) -> bool:
    """
    @meta {"stereotype": "Publisher", "desc": "Publish ContactImportCompleted with importId and number of processed rows.", "inputs": [{"name": "importId", "type": "UUID", "source": "context"}, {"name": "totalRows", "type": "int", "source": "context"}], "outputs": [{"name": "eventPublished", "type": "bool", "target": "event"}], "sideEffects": true, "idempotent": true, "consistencyScope": "eventual"}
    """
    payload = {"importId": str(import_id), "totalRows": total_rows}
    return self.event_bus.publish("ContactImportCompleted", payload)


# ============================================================
# Orchestrator / Controller flow implementation
# ============================================================

class BulkImportContactsOrchestrator:
  def __init__(
      self,
      security_service: SecurityService,
      validation_service: ValidationService,
      repository_service: RepositoryService,
      external_call_service: ExternalCallService,
      mapper_service: MapperService,
      utility_service: UtilityService,
      filter_service: FilterService,
      publisher_service: PublisherService,
  ):
    self.security_service = security_service
    self.validation_service = validation_service
    self.repository_service = repository_service
    self.external_call_service = external_call_service
    self.mapper_service = mapper_service
    self.utility_service = utility_service
    self.filter_service = filter_service
    self.publisher_service = publisher_service

  async def main_Bulkimportcontacts(self, request_envelope: BulkImportRequestEnvelope) -> ContactImportAcceptedResponse:
    context = BulkImportContext()

    # AuthorizeOperator()
    context.operator_id = self.security_service.AuthorizeOperator(
        auth_token=request_envelope.auth_token,
        tenant_id=request_envelope.tenant_id,
    )

    # ValidateImportRequest()
    context.validated_import = self.validation_service.ValidateImportRequest(
        import_request=request_envelope.import_request,
        idempotency_key=request_envelope.idempotency_key,
    )

    # CheckImportIdempotency()
    context.existing_import_id = self.repository_service.CheckImportIdempotency(
        tenant_id=request_envelope.tenant_id,
        idempotency_key=request_envelope.idempotency_key,
    )

    # Decision(importAlreadyExists)
    import_already_exists = context.existing_import_id is not None
    if import_already_exists:
      # ReturnExistingImport()
      context.response = self.mapper_service.ReturnExistingImport(
          existing_import_id=context.existing_import_id
      )
      # FlowJoinBulkImportContacts()
      return self.FlowJoinBulkImportContacts(context)

    else:
      # LoadImportFile()
      context.file_bytes = await self.external_call_service.LoadImportFile(
          validated_import=context.validated_import
      )

      # ParseCsvRows()
      context.rows, context.total_rows = self.mapper_service.ParseCsvRows(
          file_bytes=context.file_bytes
      )

      # InitializeImportCounters()
      context.row_index, context.import_errors = self.utility_service.InitializeImportCounters(
          total_rows=context.total_rows
      )

      # FlowJoinProcessRow()
      await self.FlowJoinProcessRow(context, request_envelope)
      return self.FlowJoinBulkImportContacts(context)

  def FlowJoinBulkImportContacts(self, context: BulkImportContext) -> ContactImportAcceptedResponse:
    # end()
    if context.response is None:
      raise BadRequest("Response was not built before FlowJoinBulkImportContacts")
    return context.response

  async def FlowJoinProcessRow(
      self, context: BulkImportContext, request_envelope: BulkImportRequestEnvelope
  ) -> None:
    while True:
      # Decision(rowIndexLessTotalRows)
      row_index_less_total_rows = context.row_index < context.total_rows
      if row_index_less_total_rows:
        # ReadNextRow()
        context.current_row = self.mapper_service.ReadNextRow(
            rows=context.rows,
            row_index=context.row_index,
        )

        # ValidateRow()
        context.row_valid = self.validation_service.ValidateRow(
            current_row=context.current_row
        )

        # Decision(rowValid)
        if context.row_valid is False:
          # AddRowValidationError()
          context.import_errors = self.utility_service.AddRowValidationError(
              row_index=context.row_index,
              current_row=context.current_row,
              import_errors=context.import_errors,
          )

          # FlowJoinAfterRow()
          self.FlowJoinAfterRow(context)
          continue

        if context.row_valid is True:
          # FilterDuplicateInBatch()
          context.not_duplicate = self.filter_service.FilterDuplicateInBatch(
              current_row=context.current_row,
              rows=context.rows,
          )

          # Decision(notDuplicate)
          if context.not_duplicate is False:
            # AddBatchDuplicateError()
            context.import_errors = self.utility_service.AddBatchDuplicateError(
                row_index=context.row_index,
                current_row=context.current_row,
                import_errors=context.import_errors,
            )

            # FlowJoinDuplicate()
            self.FlowJoinDuplicate(context)
            continue

          if context.not_duplicate is True:
            # MapRowToContact()
            context.contact = self.mapper_service.MapRowToContact(
                tenant_id=request_envelope.tenant_id,
                current_row=context.current_row,
            )

            # UpsertContact()
            context.contact_id = self.repository_service.UpsertContact(
                tenant_id=request_envelope.tenant_id,
                contact=context.contact,
            )

            # FlowJoinDuplicate()
            self.FlowJoinDuplicate(context)
            continue

          raise BadRequest("Decision(notDuplicate) produced no valid branch")

        raise BadRequest("Decision(rowValid) produced no valid branch")

      else:
        # PersistImportSummary()
        context.import_id = self.repository_service.PersistImportSummary(
            tenant_id=request_envelope.tenant_id,
            import_errors=context.import_errors,
        )

        # Maintain idempotency record after importId exists.
        # Assumption: spec checks idempotency before processing and returns existing import when found;
        # storing the key after successful summary persistence preserves that behavior.
        self.repository_service.store_idempotency_record(
            tenant_id=request_envelope.tenant_id,
            idempotency_key=request_envelope.idempotency_key,
            import_id=context.import_id,
        )

        # PublishImportCompleted()
        self.publisher_service.PublishImportCompleted(
            import_id=context.import_id,
            total_rows=context.total_rows,
        )

        # BuildAcceptedResponse()
        context.response = self.mapper_service.BuildAcceptedResponse(
            import_id=context.import_id
        )

        # FlowJoinBulkImportContacts()
        return

  def FlowJoinAfterRow(self, context: BulkImportContext) -> None:
    # IncrementRowIndex()
    context.row_index = self.utility_service.IncrementRowIndex(
        row_index=context.row_index
    )
    # FlowJoinProcessRow() -> loop re-entry is handled explicitly by while loop in FlowJoinProcessRow()

  def FlowJoinDuplicate(self, context: BulkImportContext) -> None:
    # FlowJoinAfterRow()
    self.FlowJoinAfterRow(context)


# ============================================================
# FastAPI app / Controller
# ============================================================

app = FastAPI(title="Bulk Import Contacts API", version="1.0.0")


# ============================================================
# Dependency bootstrap (single-file self-contained setup)
# ============================================================

audit_logger = AuditLogger()
tx_manager = InMemoryTransactionManager()
db = InMemoryDatabase()
storage = InMemoryObjectStorage()
event_bus = InMemoryEventBus()
retry_executor = RetryPolicyExecutor()

security_service = SecurityService(audit_logger)
validation_service = ValidationService()
repository_service = RepositoryService(db, tx_manager)
external_call_service = ExternalCallService(storage, retry_executor)
mapper_service = MapperService()
utility_service = UtilityService(audit_logger)
filter_service = FilterService()
publisher_service = PublisherService(event_bus)

orchestrator = BulkImportContactsOrchestrator(
    security_service=security_service,
    validation_service=validation_service,
    repository_service=repository_service,
    external_call_service=external_call_service,
    mapper_service=mapper_service,
    utility_service=utility_service,
    filter_service=filter_service,
    publisher_service=publisher_service,
)


# ============================================================
# Seed demo storage content for self-contained execution
# ============================================================

def _seed_demo_file() -> None:
  csv_content = (
    "email,phone,first_name,last_name,locale,tags\n"
    "alice@example.com,+12025550101,Alice,Smith,en,customer|vip\n"
    "bob@example.com,+12025550102,Bob,Jones,en,lead\n"
    "alice@example.com,+12025550101,Alice,Smith,en,duplicate\n"
    ",+12025550103,Charlie,Brown,en,prospect\n"
  ).encode("utf-8")
  checksum = hashlib.sha256(csv_content).hexdigest()
  storage.put_object("demo-file-ref", csv_content)
  logger.info("Seeded object storage with file_ref=demo-file-ref checksum=%s", checksum)


_seed_demo_file()


# ============================================================
# Controller endpoint implementation
# ============================================================

@app.post(
    "/contacts/import",
    response_model=ContactImportAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def main_Bulkimportcontacts_endpoint(
    import_request: ContactImportRequest = Body(...),
    authorization: str = Header(..., alias="Authorization"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    tenant_id: UUID = Header(..., alias="X-Tenant-Id"),
):
  """
  Controller mapped from:
  function main_Bulkimportcontacts()
  """
  try:
    request_envelope = BulkImportRequestEnvelope(
        tenant_id=tenant_id,
        auth_token=authorization,
        idempotency_key=idempotency_key,
        import_request=import_request,
    )
    response = await orchestrator.main_Bulkimportcontacts(request_envelope)
    return response

  except ApplicationError as exc:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.error_code, "message": exc.message},
    ) from exc
  except Exception as exc:
    logger.exception("Unhandled error during bulk import")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "InternalServerError", "message": str(exc)},
    ) from exc


# ============================================================
# Optional helper endpoint for demo/testing result lookup
# Not part of the requested spec orchestration.
# ============================================================

@app.get("/contacts/imports/{import_id}")
async def get_import_summary(import_id: UUID = Path(...)):
  summary = db.import_summaries.get(import_id)
  if not summary:
    raise HTTPException(status_code=404, detail="Import not found")
  return summary


# ============================================================
# Example startup hint
# ============================================================
# To run:
#   uvicorn this_module_name:app --reload
#
# Example request:
#   POST /contacts/import
#   Headers:
#       Authorization: Bearer operator:11111111-1111-1111-1111-111111111111
#       Idempotency-Key: idem-123
#       X-Tenant-Id: 22222222-2222-2222-2222-222222222222
#   Body:
#       {
#         "file_ref": "demo-file-ref",
#         "format": "csv",
#         "checksum_sha256": "<see startup log for seeded checksum>"
#       }

if __name__ == "__main__":
  import uvicorn
  print("Starting FastAPI server on http://localhost:8000")
  uvicorn.run(app, host="0.0.0.0", port=8004)
