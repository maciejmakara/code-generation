"""
Bulk Contact Import System
Generated from UML Activity Diagram specification
REST endpoint for importing contacts from CSV files with idempotency and error handling
"""

from fastapi import FastAPI, HTTPException, Header, Body, status
from pydantic import BaseModel, EmailStr, validator
from typing import Optional, List, Dict, Any
import uuid
import asyncio
from datetime import datetime
import csv
import io
import hashlib
import logging
from dataclasses import dataclass
from enum import Enum

app = FastAPI()

# Data Models
class ContactImportRequest(BaseModel):
    file_reference: str
    format: str = "csv"
    checksum: Optional[str] = None
    
    @validator('format')
    def validate_format(cls, v):
        if v.lower() != 'csv':
            raise ValueError("Only CSV format is supported")
        return v.lower()

class RawContactRow(BaseModel):
    row_number: int
    email: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    tags: Optional[List[str]] = []
    custom_fields: Optional[Dict[str, Any]] = {}

class Contact(BaseModel):
    id: Optional[uuid.UUID] = None
    tenant_id: uuid.UUID
    email: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    tags: List[str] = []
    custom_fields: Dict[str, Any] = {}
    created_at: datetime = datetime.utcnow()

class ImportError(BaseModel):
    row_number: int
    error_type: str
    message: str
    field: Optional[str] = None

class ValidatedImport(BaseModel):
    file_reference: str
    format: str
    checksum: Optional[str] = None

class ContactImportAcceptedResponse(BaseModel):
    import_id: uuid.UUID
    status: str = "accepted"
    results_url: str

# Context object for flow execution
@dataclass
class ImportContext:
    operator_id: Optional[uuid.UUID] = None
    tenant_id: Optional[uuid.UUID] = None
    validated_import: Optional[ValidatedImport] = None
    existing_import_id: Optional[uuid.UUID] = None
    file_bytes: Optional[bytes] = None
    rows: List[RawContactRow] = None
    total_rows: int = 0
    row_index: int = 0
    import_errors: List[ImportError] = None
    current_row: Optional[RawContactRow] = None
    row_valid: bool = False
    not_duplicate: bool = True
    contact: Optional[Contact] = None
    contact_id: Optional[uuid.UUID] = None
    import_id: Optional[uuid.UUID] = None
    event_published: bool = False
    response: Optional[ContactImportAcceptedResponse] = None

    def __post_init__(self):
        if self.rows is None:
            self.rows = []
        if self.import_errors is None:
            self.import_errors = []

# Exception Classes
class UnauthorizedException(Exception):
    pass

class ForbiddenException(Exception):
    pass

class BadRequestException(Exception):
    pass

class ConflictException(Exception):
    pass

class FileNotFoundException(Exception):
    pass

class StorageUnavailableException(Exception):
    pass

# Stereotype Implementations

class SecurityService:
    """Security stereotype implementation"""
    
    @staticmethod
    def authorize_operator(auth_token: str, tenant_id: uuid.UUID) -> uuid.UUID:
        """Ensure the caller is an operator allowed to import contacts for a tenant."""
        # Mock implementation - in real system would validate JWT token
        if not auth_token or not auth_token.startswith("Bearer "):
            raise UnauthorizedException("Invalid auth token")
        
        # Mock tenant validation
        if str(tenant_id) == "00000000-0000-0000-0000-000000000000":
            raise ForbiddenException("Access forbidden for this tenant")
        
        return uuid.uuid4()  # Mock operator ID

class ValidationService:
    """Validation stereotype implementation"""
    
    @staticmethod
    def validate_import_request(import_request: ContactImportRequest, idempotency_key: str) -> ValidatedImport:
        """Validate import request includes file reference, format, and idempotency key."""
        if not import_request.file_reference:
            raise BadRequestException("File reference is required")
        
        if not idempotency_key:
            raise BadRequestException("Idempotency key is required")
        
        return ValidatedImport(
            file_reference=import_request.file_reference,
            format=import_request.format,
            checksum=import_request.checksum
        )

class RepositoryService:
    """Repository stereotype implementation"""
    
    @staticmethod
    def check_import_idempotency(tenant_id: uuid.UUID, idempotency_key: str) -> Optional[uuid.UUID]:
        """Ensure the idempotency key was not used before for this tenant to avoid duplicate imports."""
        # Mock implementation - would check database
        mock_existing_key = "existing-key-123"
        if idempotency_key == mock_existing_key:
            return uuid.uuid4()  # Mock existing import ID
        return None
    
    @staticmethod
    def upsert_contact(tenant_id: uuid.UUID, contact: Contact) -> uuid.UUID:
        """Upsert contact by email/phone and attach tags from import."""
        # Mock implementation - would perform database upsert
        contact.id = uuid.uuid4()
        return contact.id
    
    @staticmethod
    def persist_import_summary(tenant_id: uuid.UUID, import_errors: List[ImportError]) -> uuid.UUID:
        """Persist import summary including counts and error list for later retrieval."""
        # Mock implementation - would save to database
        return uuid.uuid4()

class ExternalCallService:
    """ExternalCall stereotype implementation"""
    
    @staticmethod
    async def load_import_file(validated_import: ValidatedImport) -> bytes:
        """Download import file from object storage and verify checksum."""
        # Mock implementation - would download from storage
        mock_file_content = "email,first_name,last_name,phone\ntest@example.com,John,Doe,+1234567890\n"
        
        # Simulate retry logic
        for attempt in range(3):
            try:
                file_bytes = mock_file_content.encode('utf-8')
                
                # Verify checksum if provided
                if validated_import.checksum:
                    calculated_checksum = hashlib.md5(file_bytes).hexdigest()
                    if calculated_checksum != validated_import.checksum:
                        raise BadRequestException("File checksum mismatch")
                
                return file_bytes
            except Exception as e:
                if attempt == 2:  # Last attempt
                    if "timeout" in str(e).lower():
                        raise StorageUnavailableException("Storage timeout")
                    raise
                await asyncio.sleep(0.3)  # 300ms backoff

class MapperService:
    """Mapper stereotype implementation"""
    
    @staticmethod
    def return_existing_import(existing_import_id: uuid.UUID) -> ContactImportAcceptedResponse:
        """Return existing import reference without processing the file again."""
        return ContactImportAcceptedResponse(
            import_id=existing_import_id,
            results_url=f"/api/imports/{existing_import_id}/results"
        )
    
    @staticmethod
    def parse_csv_rows(file_bytes: bytes) -> tuple[List[RawContactRow], int]:
        """Parse CSV into a list of raw contact rows, preserving row numbers for error reporting."""
        rows = []
        content = file_bytes.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(content))
        
        for row_num, row_data in enumerate(csv_reader, start=2):  # Start at 2 (after header)
            raw_row = RawContactRow(
                row_number=row_num,
                email=row_data.get('email', '').strip() if row_data.get('email') else None,
                phone=row_data.get('phone', '').strip() if row_data.get('phone') else None,
                first_name=row_data.get('first_name', '').strip() if row_data.get('first_name') else None,
                last_name=row_data.get('last_name', '').strip() if row_data.get('last_name') else None,
                tags=[tag.strip() for tag in row_data.get('tags', '').split(',')] if row_data.get('tags') else []
            )
            rows.append(raw_row)
        
        return rows, len(rows)
    
    @staticmethod
    def read_next_row(rows: List[RawContactRow], row_index: int) -> RawContactRow:
        """Read current row and normalize values (trim, casing, locale)."""
        return rows[row_index]
    
    @staticmethod
    def map_row_to_contact(tenant_id: uuid.UUID, current_row: RawContactRow) -> Contact:
        """Convert row into domain Contact object using tenant defaults."""
        return Contact(
            tenant_id=tenant_id,
            email=current_row.email.lower() if current_row.email else None,
            phone=current_row.phone,
            first_name=current_row.first_name.title() if current_row.first_name else None,
            last_name=current_row.last_name.title() if current_row.last_name else None,
            tags=current_row.tags or []
        )
    
    @staticmethod
    def build_accepted_response(import_id: uuid.UUID) -> ContactImportAcceptedResponse:
        """Build 202 response containing importId and a link to fetch results."""
        return ContactImportAcceptedResponse(
            import_id=import_id,
            results_url=f"/api/imports/{import_id}/results"
        )

class UtilityService:
    """Utility stereotype implementation"""
    
    @staticmethod
    def initialize_import_counters(total_rows: int) -> tuple[int, List[ImportError]]:
        """Initialize counters and an error collection for the import session."""
        return 0, []  # row_index starts at 0, empty errors list
    
    @staticmethod
    def add_row_validation_error(row_index: int, current_row: RawContactRow, import_errors: List[ImportError]) -> List[ImportError]:
        """Append row-level validation errors with a human-friendly message."""
        error = ImportError(
            row_number=current_row.row_number,
            error_type="validation",
            message="Row validation failed: missing required fields",
            field="email_or_phone"
        )
        import_errors.append(error)
        return import_errors
    
    @staticmethod
    def add_batch_duplicate_error(row_index: int, current_row: RawContactRow, import_errors: List[ImportError]) -> List[ImportError]:
        """Record a batch-duplicate error for the row and continue the loop."""
        error = ImportError(
            row_number=current_row.row_number,
            error_type="duplicate",
            message="Duplicate record found within batch",
            field="email_or_phone"
        )
        import_errors.append(error)
        return import_errors
    
    @staticmethod
    def increment_row_index(row_index: int) -> int:
        """Increment row index and continue processing remaining rows."""
        return row_index + 1

class FilterService:
    """Filter stereotype implementation"""
    
    @staticmethod
    def filter_duplicate_in_batch(current_row: RawContactRow, rows: List[RawContactRow]) -> bool:
        """Skip duplicates within the current batch using email and phone as keys."""
        seen_emails = set()
        seen_phones = set()
        
        for i, row in enumerate(rows):
            if i >= current_row.row_number - 1:  # Only check rows before current
                break
            if row.email:
                seen_emails.add(row.email.lower())
            if row.phone:
                seen_phones.add(row.phone)
        
        is_duplicate = False
        if current_row.email and current_row.email.lower() in seen_emails:
            is_duplicate = True
        elif current_row.phone and current_row.phone in seen_phones:
            is_duplicate = True
        
        return not is_duplicate  # Return True if NOT duplicate

class PublisherService:
    """Publisher stereotype implementation"""
    
    @staticmethod
    async def publish_import_completed(import_id: uuid.UUID, total_rows: int) -> bool:
        """Publish ContactImportCompleted with importId and number of processed rows."""
        # Mock implementation - would publish to message queue
        logging.info(f"Published import completed event: {import_id}, rows: {total_rows}")
        return True

# Validation helpers
class ValidationHelper:
    @staticmethod
    def validate_row(current_row: RawContactRow) -> bool:
        """Validate required fields (email/phone), formats, and maximum lengths for the row."""
        # Must have at least email or phone
        if not current_row.email and not current_row.phone:
            return False
        
        # Basic email format validation
        if current_row.email:
            if '@' not in current_row.email or len(current_row.email) > 255:
                return False
        
        # Basic phone validation
        if current_row.phone:
            if len(current_row.phone) > 20:
                return False
        
        return True

# Main Controller
@app.post("/contacts/import", status_code=status.HTTP_202_ACCEPTED)
async def bulk_import_contacts(
    import_request: ContactImportRequest = Body(...),
    idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    authorization: str = Header(..., alias="Authorization"),
    x_tenant_id: uuid.UUID = Header(..., alias="X-Tenant-ID")
):
    """Main bulk import contacts endpoint implementing the full UML flow"""
    context = ImportContext()
    context.tenant_id = x_tenant_id
    
    try:
        # Main flow execution
        await main_bulk_import_contacts(context, import_request, idempotency_key, authorization)
        return context.response
    except UnauthorizedException:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except ForbiddenException:
        raise HTTPException(status_code=403, detail="Forbidden")
    except BadRequestException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConflictException:
        raise HTTPException(status_code=409, detail="Conflict")
    except StorageUnavailableException:
        raise HTTPException(status_code=503, detail="Storage unavailable")

async def main_bulk_import_contacts(context: ImportContext, import_request: ContactImportRequest, idempotency_key: str, authorization: str):
    """Main flow function implementing the UML specification"""
    
    # AuthorizeOperator
    context.operator_id = SecurityService.authorize_operator(authorization, context.tenant_id)
    
    # ValidateImportRequest
    context.validated_import = ValidationService.validate_import_request(import_request, idempotency_key)
    
    # CheckImportIdempotency
    context.existing_import_id = RepositoryService.check_import_idempotency(context.tenant_id, idempotency_key)
    
    # Decision(importAlreadyExists)
    if context.existing_import_id:
        # ReturnExistingImport
        context.response = MapperService.return_existing_import(context.existing_import_id)
        await flow_join_bulk_import_contacts()
    else:
        # LoadImportFile
        context.file_bytes = await ExternalCallService.load_import_file(context.validated_import)
        
        # ParseCsvRows
        context.rows, context.total_rows = MapperService.parse_csv_rows(context.file_bytes)
        
        # InitializeImportCounters
        context.row_index, context.import_errors = UtilityService.initialize_import_counters(context.total_rows)
        
        await flow_join_process_row(context)

async def flow_join_bulk_import_contacts():
    """Flow join for bulk import completion"""
    pass  # End of flow

async def flow_join_process_row(context: ImportContext):
    """Flow join for row processing loop"""
    # Decision(rowIndexLessTotalRows)
    if context.row_index < context.total_rows:
        # ReadNextRow
        context.current_row = MapperService.read_next_row(context.rows, context.row_index)
        
        # ValidateRow
        context.row_valid = ValidationHelper.validate_row(context.current_row)
        
        # Decision(rowValid)
        if not context.row_valid:
            # AddRowValidationError
            context.import_errors = UtilityService.add_row_validation_error(
                context.row_index, context.current_row, context.import_errors
            )
            await flow_join_after_row(context)
        else:
            # FilterDuplicateInBatch
            context.not_duplicate = FilterService.filter_duplicate_in_batch(context.current_row, context.rows)
            
            # Decision(notDuplicate)
            if not context.not_duplicate:
                # AddBatchDuplicateError
                context.import_errors = UtilityService.add_batch_duplicate_error(
                    context.row_index, context.current_row, context.import_errors
                )
                await flow_join_duplicate(context)
            else:
                # MapRowToContact
                context.contact = MapperService.map_row_to_contact(context.tenant_id, context.current_row)
                
                # UpsertContact
                context.contact_id = RepositoryService.upsert_contact(context.tenant_id, context.contact)
                
                await flow_join_duplicate(context)
    else:
        # PersistImportSummary
        context.import_id = RepositoryService.persist_import_summary(context.tenant_id, context.import_errors)
        
        # PublishImportCompleted
        context.event_published = await PublisherService.publish_import_completed(context.import_id, context.total_rows)
        
        # BuildAcceptedResponse
        context.response = MapperService.build_accepted_response(context.import_id)
        
        await flow_join_bulk_import_contacts()

async def flow_join_after_row(context: ImportContext):
    """Flow join after row processing"""
    # IncrementRowIndex
    context.row_index = UtilityService.increment_row_index(context.row_index)
    await flow_join_process_row(context)

async def flow_join_duplicate(context: ImportContext):
    """Flow join for duplicate handling"""
    await flow_join_after_row(context)

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)