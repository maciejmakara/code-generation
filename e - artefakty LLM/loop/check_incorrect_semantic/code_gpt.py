from __future__ import annotations

import asyncio
import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# =========================
# Exceptions (@meta.exceptions)
# =========================

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


class FileNotFound(DomainException):
  pass


class StorageUnavailable(DomainException):
  pass


# =========================
# Models / DTOs
# =========================

class ContactImportRequest(BaseModel):
  # Assumption: request contains object key + checksum + format as required by ValidateImportRequest.
  fileRef: str = Field(min_length=1)
  checksum: str = Field(min_length=1)
  format: str = Field(min_length=1, description="Expected: csv")


class ValidatedImport(BaseModel):
  fileRef: str
  checksum: str
  format: str


class RawContactRow(BaseModel):
  rowNumber: int
  email: Optional[str] = None
  phone: Optional[str] = None
  firstName: Optional[str] = None
  lastName: Optional[str] = None
  tags: List[str] = Field(default_factory=list)


class ImportError(BaseModel):
  rowIndex: int
  code: str
  message: str


class Contact(BaseModel):
  tenantId: UUID
  email: Optional[str] = None
  phone: Optional[str] = None
  firstName: Optional[str] = None
  lastName: Optional[str] = None
  tags: List[str] = Field(default_factory=list)


class ContactImportAcceptedResponse(BaseModel):
  importId: UUID
  resultUrl: str


@dataclass
class ImportContext:
  operatorId: Optional[UUID] = None
  validatedImport: Optional[ValidatedImport] = None
  existingImportId: Optional[UUID] = None

  fileBytes: Optional[bytes] = None
  rows: List[RawContactRow] = field(default_factory=list)
  totalRows: int = 0
  rowIndex: int = 0
  importErrors: List[ImportError] = field(default_factory=list)

  currentRow: Optional[RawContactRow] = None
  rowValid: Optional[bool] = None
  notDuplicate: Optional[bool] = None
  contact: Optional[Contact] = None
  contactId: Optional[UUID] = None

  importId: Optional[UUID] = None
  response: Optional[ContactImportAcceptedResponse] = None


# =========================
# Helpers
# =========================

@contextmanager
def transaction_boundary(name: str):
  # Best-effort transaction boundary for single-file demo.
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
        label = self._to_label(exc)
        can_retry = (attempt < max_attempts) and (label in retry_on)
        if can_retry:
          await asyncio.sleep(backoff_ms / 1000.0)
          continue
        if on_retry_exhausted == "throw_StorageUnavailable":
          raise StorageUnavailable("Storage unavailable after retries.") from exc
        raise

  @staticmethod
  def _to_label(exc: Exception) -> str:
    msg = str(exc)
    if msg in {"502", "503", "Timeout"}:
      return msg
    return exc.__class__.__name__


# =========================
# In-memory stores
# =========================

class InMemoryStorage:
  def __init__(self):
    self.files: Dict[str, Tuple[bytes, str]] = {}

  def put_file(self, key: str, data: bytes, checksum: str):
    self.files[key] = (data, checksum)

  def get_file(self, key: str) -> Tuple[bytes, str]:
    if key not in self.files:
      raise FileNotFound(f"File not found: {key}")
    return self.files[key]


class InMemoryImportRepo:
  def __init__(self):
    self.idempotency_map: Dict[Tuple[UUID, str], Optional[UUID]] = {}
    self.import_summaries: Dict[UUID, Dict[str, Any]] = {}
    self.contacts: Dict[Tuple[UUID, str, str], UUID] = {}

  def check_idempotency(self, tenant_id: UUID, key: str) -> Optional[UUID]:
    return self.idempotency_map.get((tenant_id, key))

  def reserve_idempotency(self, tenant_id: UUID, key: str):
    self.idempotency_map.setdefault((tenant_id, key), None)

  def save_summary(self, tenant_id: UUID, errors: List[ImportError]) -> UUID:
    import_id = uuid4()
    self.import_summaries[import_id] = {
      "tenantId": tenant_id,
      "errors": [e.model_dump() for e in errors],
    }
    return import_id

  def bind_idempotency(self, tenant_id: UUID, key: str, import_id: UUID):
    self.idempotency_map[(tenant_id, key)] = import_id

  def upsert_contact(self, tenant_id: UUID, contact: Contact) -> UUID:
    k = (tenant_id, (contact.email or "").lower(), (contact.phone or "").strip())
    if k in self.contacts:
      return self.contacts[k]
    cid = uuid4()
    self.contacts[k] = cid
    return cid


# =========================
# Services (one action => one method)
# =========================

class SecurityService:
  def AuthorizeOperator(self, authToken: Optional[str], tenantId: UUID) -> UUID:
    if not authToken or not authToken.startswith("Bearer "):
      raise Unauthorized("Missing or invalid auth token.")
    token_value = authToken.removeprefix("Bearer ").strip()
    try:
      operator_id = UUID(token_value)
    except ValueError as exc:
      raise Unauthorized("Invalid token payload.") from exc
    # Assumption: token-to-tenant authorization check mocked by simple deterministic rule.
    if operator_id.int % 2 != tenantId.int % 2:
      raise Forbidden("Operator not allowed for tenant.")
    return operator_id


class ValidationService:
  def ValidateImportRequest(self, importRequest: ContactImportRequest, idempotencyKey: str) -> ValidatedImport:
    if not idempotencyKey.strip():
      raise BadRequest("idempotencyKey is required.")
    if importRequest.format.lower() != "csv":
      raise BadRequest("Only CSV format is supported.")
    return ValidatedImport(
        fileRef=importRequest.fileRef,
        checksum=importRequest.checksum,
        format=importRequest.format.lower(),
    )

  def ValidateRow(self, currentRow: RawContactRow) -> bool:
    email = (currentRow.email or "").strip()
    phone = (currentRow.phone or "").strip()
    if not email and not phone:
      return False
    if email and "@" not in email:
      return False
    if len((currentRow.firstName or "")) > 100 or len((currentRow.lastName or "")) > 100:
      return False
    return True


class ImportRepositoryService:
  def __init__(self, repo: InMemoryImportRepo):
    self.repo = repo

  def CheckImportIdempotency(self, tenantId: UUID, idempotencyKey: str) -> Optional[UUID]:
    # sideEffects=true + consistencyScope=local-transaction
    with transaction_boundary("CheckImportIdempotency"):
      existing = self.repo.check_idempotency(tenantId, idempotencyKey)
      if existing is None:
        self.repo.reserve_idempotency(tenantId, idempotencyKey)
      return existing

  def UpsertContact(self, tenantId: UUID, contact: Contact) -> UUID:
    return self.repo.upsert_contact(tenantId, contact)

  def PersistImportSummary(self, tenantId: UUID, importErrors: List[ImportError]) -> UUID:
    with transaction_boundary("PersistImportSummary"):
      return self.repo.save_summary(tenantId, importErrors)


class ExternalService:
  def __init__(self, storage: InMemoryStorage, retry: RetryExecutor):
    self.storage = storage
    self.retry = retry

  async def LoadImportFile(self, validatedImport: ValidatedImport) -> bytes:
    async def _load_once() -> bytes:
      file_bytes, checksum = self.storage.get_file(validatedImport.fileRef)
      if checksum != validatedImport.checksum:
        raise StorageUnavailable("Checksum mismatch.")
      return file_bytes

    return await self.retry.run(
        _load_once,
        max_attempts=3,
        backoff_ms=300,
        retry_on=["Timeout", "502", "503"],
        on_retry_exhausted="throw_StorageUnavailable",
    )

  async def SendCancellationEmail(self):  # Not used in this spec.
    return None


class MapperService:
  def ReturnExistingImport(self, existingImportId: UUID) -> ContactImportAcceptedResponse:
    return ContactImportAcceptedResponse(
        importId=existingImportId,
        resultUrl=f"/contacts/import/{existingImportId}/result",
    )

  def ParseCsvRows(self, fileBytes: bytes) -> Tuple[List[RawContactRow], int]:
    text = fileBytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    out: List[RawContactRow] = []
    row_no = 0
    for r in reader:
      row_no += 1
      tags = [t.strip() for t in (r.get("tags") or "").split("|") if t.strip()]
      out.append(
          RawContactRow(
              rowNumber=row_no,
              email=r.get("email"),
              phone=r.get("phone"),
              firstName=r.get("firstName"),
              lastName=r.get("lastName"),
              tags=tags,
          )
      )
    return out, len(out)

  def ReadNextRow(self, rows: List[RawContactRow], rowIndex: int) -> RawContactRow:
    return rows[rowIndex]

  def MapRowToContact(self, tenantId: UUID, currentRow: RawContactRow) -> Contact:
    return Contact(
        tenantId=tenantId,
        email=(currentRow.email or "").strip().lower() or None,
        phone=(currentRow.phone or "").strip() or None,
        firstName=(currentRow.firstName or "").strip() or None,
        lastName=(currentRow.lastName or "").strip() or None,
        tags=currentRow.tags,
    )

  def BuildAcceptedResponse(self, importId: UUID) -> ContactImportAcceptedResponse:
    return ContactImportAcceptedResponse(
        importId=importId,
        resultUrl=f"/contacts/import/{importId}/result",
    )


class UtilityService:
  def InitializeImportCounters(self, totalRows: int) -> Tuple[int, List[ImportError]]:
    _ = totalRows
    return 0, []

  def AddRowValidationError(self, rowIndex: int, currentRow: RawContactRow, importErrors: List[ImportError]) -> List[ImportError]:
    importErrors.append(
        ImportError(
            rowIndex=rowIndex,
            code="ROW_VALIDATION_ERROR",
            message=f"Invalid row at index {rowIndex} (rowNumber={currentRow.rowNumber}).",
        )
    )
    return importErrors

  def AddBatchDuplicateError(self, rowIndex: int, currentRow: RawContactRow, importErrors: List[ImportError]) -> List[ImportError]:
    importErrors.append(
        ImportError(
            rowIndex=rowIndex,
            code="BATCH_DUPLICATE",
            message=f"Duplicate row in batch at index {rowIndex} (rowNumber={currentRow.rowNumber}).",
        )
    )
    return importErrors

  def IncrementRowIndex(self, rowIndex: int) -> int:
    return rowIndex + 1


class FilterService:
  def FilterDuplicateInBatch(self, currentRow: RawContactRow, rows: List[RawContactRow]) -> bool:
    key = ((currentRow.email or "").strip().lower(), (currentRow.phone or "").strip())
    first_idx = None
    for i, r in enumerate(rows):
      k = ((r.email or "").strip().lower(), (r.phone or "").strip())
      if k == key:
        first_idx = i
        break
    return first_idx == (currentRow.rowNumber - 1)


class PublisherService:
  def __init__(self):
    self.events: List[Dict[str, Any]] = []

  def PublishImportCompleted(self, importId: UUID, totalRows: int) -> bool:
    self.events.append(
        {"type": "ContactImportCompleted", "importId": str(importId), "totalRows": totalRows}
    )
    return True


# =========================
# FlowJoin functions
# =========================

def FlowJoinBulkImportContacts():
  return None


# =========================
# App wiring
# =========================

app = FastAPI(title="Bulk Import Contacts API")

storage = InMemoryStorage()
repo = InMemoryImportRepo()
retry = RetryExecutor()

security_service = SecurityService()
validation_service = ValidationService()
import_repo_service = ImportRepositoryService(repo)
external_service = ExternalService(storage, retry)
mapper_service = MapperService()
utility_service = UtilityService()
filter_service = FilterService()
publisher_service = PublisherService()

# Seed sample file for deterministic local run.
sample_csv = (
  "email,phone,firstName,lastName,tags\n"
  "alice@example.com,,Alice,Blue,lead|vip\n"
  "bob@example.com,+12025550123,Bob,Green,customer\n"
).encode("utf-8")
storage.put_file("tenant/sample.csv", sample_csv, "ok-checksum")


# =========================
# Controller / orchestration
# REST: POST /contacts/import
# =========================

@app.post("/contacts/import", status_code=status.HTTP_202_ACCEPTED)
async def main_Bulkimportcontacts(
    importRequest: ContactImportRequest,
    tenantId: UUID = Query(...),
    idempotencyKey: str = Header(..., alias="Idempotency-Key"),
    authToken: Optional[str] = Header(None, alias="Authorization"),
):
  ctx = ImportContext()

  try:
    # AuthorizeOperator()
    ctx.operatorId = security_service.AuthorizeOperator(authToken=authToken, tenantId=tenantId)

    # ValidateImportRequest()
    ctx.validatedImport = validation_service.ValidateImportRequest(
        importRequest=importRequest, idempotencyKey=idempotencyKey
    )

    # CheckImportIdempotency()
    ctx.existingImportId = import_repo_service.CheckImportIdempotency(
        tenantId=tenantId, idempotencyKey=idempotencyKey
    )

    # Decision(importAlreadyExists)
    importAlreadyExists = ctx.existingImportId is not None

    if importAlreadyExists:
      # if yes:
      # ReturnExistingImport()
      ctx.response = mapper_service.ReturnExistingImport(existingImportId=ctx.existingImportId)
      # FlowJoinBulkImportContacts()
      FlowJoinBulkImportContacts()
      return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=ctx.response.model_dump(mode="json"))

    # if no:
    # LoadImportFile()
    ctx.fileBytes = await external_service.LoadImportFile(validatedImport=ctx.validatedImport)

    # ParseCsvRows()
    ctx.rows, ctx.totalRows = mapper_service.ParseCsvRows(fileBytes=ctx.fileBytes)

    # InitializeImportCounters()
    ctx.rowIndex, ctx.importErrors = utility_service.InitializeImportCounters(totalRows=ctx.totalRows)

    # FlowJoinProcessRow()
    async def FlowJoinProcessRow():
      while True:
        # Decision(rowIndexLessTotalRows)
        rowIndexLessTotalRows = ctx.rowIndex < ctx.totalRows

        if rowIndexLessTotalRows:
          # if yes:
          # ReadNextRow()
          ctx.currentRow = mapper_service.ReadNextRow(rows=ctx.rows, rowIndex=ctx.rowIndex)

          # ValidateRow()
          ctx.rowValid = validation_service.ValidateRow(currentRow=ctx.currentRow)

          # Decision(rowValid)
          if ctx.rowValid is False:
            # if false:
            # AddRowValidationError()
            ctx.importErrors = utility_service.AddRowValidationError(
                rowIndex=ctx.rowIndex,
                currentRow=ctx.currentRow,
                importErrors=ctx.importErrors,
            )
            # FlowJoinProcessRow() -> loop re-entry through FlowJoinAfterRow chain in this implementation
            await FlowJoinAfterRow()
            continue

          # if true:
          # FilterDuplicateInBatch()
          ctx.notDuplicate = filter_service.FilterDuplicateInBatch(
              currentRow=ctx.currentRow, rows=ctx.rows
          )

          # Decision(notDuplicate)
          if ctx.notDuplicate is False:
            # if false:
            # AddBatchDuplicateError()
            ctx.importErrors = utility_service.AddBatchDuplicateError(
                rowIndex=ctx.rowIndex,
                currentRow=ctx.currentRow,
                importErrors=ctx.importErrors,
            )
            # FlowJoinDuplicate()
            await FlowJoinDuplicate()
            continue

          # if true:
          # MapRowToContact()
          ctx.contact = mapper_service.MapRowToContact(
              tenantId=tenantId, currentRow=ctx.currentRow
          )

          # UpsertContact()
          ctx.contactId = import_repo_service.UpsertContact(
              tenantId=tenantId, contact=ctx.contact
          )

          # FlowJoinDuplicate()
          await FlowJoinDuplicate()
          continue

        # if no:
        # PersistImportSummary()
        ctx.importId = import_repo_service.PersistImportSummary(
            tenantId=tenantId, importErrors=ctx.importErrors
        )

        # Bind idempotency key to final importId after summary persist.
        repo.bind_idempotency(tenantId=tenantId, key=idempotencyKey, import_id=ctx.importId)

        # PublishImportCompleted()
        _eventPublished = publisher_service.PublishImportCompleted(
            importId=ctx.importId, totalRows=ctx.totalRows
        )

        # BuildAcceptedResponse()
        ctx.response = mapper_service.BuildAcceptedResponse(importId=ctx.importId)

        # FlowJoinBulkImportContacts()
        FlowJoinBulkImportContacts()
        return

    async def FlowJoinDuplicate():
      # FlowJoinDuplicate() -> FlowJoinAfterRow()
      await FlowJoinAfterRow()

    async def FlowJoinAfterRow():
      # IncrementRowIndex()
      ctx.rowIndex = utility_service.IncrementRowIndex(rowIndex=ctx.rowIndex)
      # FlowJoinProcessRow() handled by caller loop continuation.
      return

    await FlowJoinProcessRow()
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=ctx.response.model_dump(mode="json"))

  except BadRequest as exc:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
  except Unauthorized as exc:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
  except Forbidden as exc:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
  except Conflict as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
  except (FileNotFound, StorageUnavailable) as exc:
    # responseError allows 422
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
