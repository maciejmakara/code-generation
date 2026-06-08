import asyncio
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, field
from enum import Enum
import json
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enums
class StatementStatus(Enum):
    COMPLETED = "completed"
    RUNNING = "running"
    FAILED = "failed"
    NOT_FOUND = "not_found"

class JobProgress(Enum):
    STALLED = "stalled"
    ACTIVE = "active"

# Custom Exceptions
class Unauthorized(Exception):
    pass

class Forbidden(Exception):
    pass

class BadRequest(Exception):
    pass

class Conflict(Exception):
    pass

class NotFound(Exception):
    pass

class JobNotFound(Exception):
    pass

class AnalysisUnavailable(Exception):
    pass

class ProfileServiceUnavailable(Exception):
    pass

class StorageUnavailable(Exception):
    pass

class EventPublishFailed(Exception):
    pass

class DataCorruption(Exception):
    pass

# Data Models
@dataclass
class StatementRequest:
    period_start: datetime
    period_end: datetime
    format: str = "PDF"

@dataclass
class ValidatedStatement:
    account_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    format: str
    idempotency_key: str

@dataclass
class Transaction:
    id: uuid.UUID
    amount: float
    date: datetime
    description: str
    type: str

@dataclass
class FeeSchedule:
    fees: List[Dict[str, Any]]

@dataclass
class CalculationResult:
    fee_amount: float
    calculation_type: str

@dataclass
class AccountProfile:
    customer_id: uuid.UUID
    preferences: Dict[str, Any]

@dataclass
class FormattingPreferences:
    language: str = "en"
    date_format: str = "%Y-%m-%d"
    currency: str = "USD"

@dataclass
class StatementTotals:
    total_amount: float
    total_fees: float
    net_amount: float

@dataclass
class FailureAnalysis:
    reason: str
    retryable: bool
    error_code: str

@dataclass
class RecoveryResult:
    recovered: bool
    resume_point: str

@dataclass
class ResumePoint:
    step: str
    checkpoint_data: Dict[str, Any]

@dataclass
class StatementAcceptedResponse:
    statement_id: Optional[uuid.UUID] = None
    status: str = "accepted"
    message: str = "Statement generation initiated"
    download_url: Optional[str] = None

@dataclass
class Context:
    customer_id: Optional[uuid.UUID] = None
    validated_statement: Optional[ValidatedStatement] = None
    existing_statement_id: Optional[uuid.UUID] = None
    account_id: Optional[uuid.UUID] = None
    statement_status: Optional[StatementStatus] = None
    job_progress: Optional[JobProgress] = None
    failure_analysis: Optional[FailureAnalysis] = None
    recovery_result: Optional[RecoveryResult] = None
    resume_point: Optional[ResumePoint] = None
    new_statement_id: Optional[uuid.UUID] = None
    statement_id: Optional[uuid.UUID] = None
    transactions: List[Transaction] = field(default_factory=list)
    fee_schedule: Optional[FeeSchedule] = None
    preliminary_calculations: List[CalculationResult] = field(default_factory=list)
    account_profile: Optional[AccountProfile] = None
    formatting_preferences: Optional[FormattingPreferences] = None
    statement_totals: Optional[StatementTotals] = None
    statement_document: Optional[bytes] = None
    download_url: Optional[str] = None
    job_status: Optional[str] = None
    event_published: bool = False

# Service Classes
class SecurityService:
    def authenticate_request(self, auth_token: str) -> uuid.UUID:
        """Authenticate customer and validate JWT token."""
        if not auth_token or not auth_token.startswith("Bearer "):
            raise Unauthorized("Invalid token format")
        
        # Mock JWT validation - in production would verify signature and claims
        customer_id = uuid.uuid4()  # Mock extracted from token
        logger.info(f"Customer {customer_id} authenticated")
        return customer_id

class ValidationService:
    def validate_statement_request(
        self, 
        account_id: uuid.UUID, 
        statement_request: StatementRequest,
        idempotency_key: str,
        customer_id: uuid.UUID
    ) -> tuple[ValidatedStatement, Optional[uuid.UUID]]:
        """Validate statement request parameters and check idempotency."""
        if not account_id or not statement_request:
            raise BadRequest("Invalid request parameters")
        
        if statement_request.period_start >= statement_request.period_end:
            raise BadRequest("Invalid date range")
        
        # Mock idempotency check - in production would check database
        existing_statement_id = None
        # existing_statement_id = self._check_existing_idempotency(idempotency_key)
        
        validated = ValidatedStatement(
            account_id=account_id,
            period_start=statement_request.period_start,
            period_end=statement_request.period_end,
            format=statement_request.format,
            idempotency_key=idempotency_key
        )
        
        logger.info(f"Statement request validated for account {account_id}")
        return validated, existing_statement_id

class BusinessRuleService:
    def evaluate_statement_status(
        self, 
        account_id: uuid.UUID, 
        validated_statement: ValidatedStatement
    ) -> StatementStatus:
        """Evaluate current statement status to determine processing path."""
        # Mock status evaluation - in production would check job repository
        return StatementStatus.NOT_FOUND
    
    def analyze_failure_reason(self, existing_statement_id: uuid.UUID) -> FailureAnalysis:
        """Analyze why previous statement generation failed."""
        return FailureAnalysis(
            reason="Timeout during transaction loading",
            retryable=True,
            error_code="TIMEOUT"
        )
    
    def recover_failed_job(self, existing_statement_id: uuid.UUID) -> tuple[RecoveryResult, ResumePoint]:
        """Recover failed job with checkpoint resume."""
        recovery = RecoveryResult(
            recovered=True,
            resume_point="transaction_loading"
        )
        resume_point = ResumePoint(
            step="LoadTransactions",
            checkpoint_data={"last_transaction_id": str(uuid.uuid4())}
        )
        return recovery, resume_point
    
    def compute_statement_totals(
        self, 
        transactions: List[Transaction],
        fee_schedule: FeeSchedule,
        preliminary_calculations: List[CalculationResult]
    ) -> StatementTotals:
        """Compute totals using preliminary calculations and transaction data."""
        total_amount = sum(t.amount for t in transactions)
        total_fees = sum(c.fee_amount for c in preliminary_calculations)
        net_amount = total_amount - total_fees
        
        return StatementTotals(
            total_amount=total_amount,
            total_fees=total_fees,
            net_amount=net_amount
        )

class RepositoryService:
    def check_job_progress(self, existing_statement_id: uuid.UUID) -> JobProgress:
        """Check if running job is stalled or needs to be restarted."""
        # Mock job progress check
        return JobProgress.ACTIVE
    
    def restart_job(self, existing_statement_id: uuid.UUID) -> uuid.UUID:
        """Restart stalled job with monitoring."""
        logger.info(f"Restarting job {existing_statement_id}")
        return existing_statement_id
    
    def create_statement_job(
        self, 
        account_id: uuid.UUID, 
        validated_statement: ValidatedStatement
    ) -> uuid.UUID:
        """Create statement generation job."""
        statement_id = uuid.uuid4()
        logger.info(f"Created statement job {statement_id} for account {account_id}")
        return statement_id
    
    def create_new_statement_job(
        self, 
        account_id: uuid.UUID, 
        validated_statement: ValidatedStatement
    ) -> uuid.UUID:
        """Create new job for non-retryable failures."""
        statement_id = uuid.uuid4()
        logger.info(f"Created new statement job {statement_id} for account {account_id}")
        return statement_id
    
    def load_transactions(
        self, 
        account_id: uuid.UUID, 
        validated_statement: ValidatedStatement
    ) -> List[Transaction]:
        """Load transactions for the account and period."""
        # Mock transaction loading
        transactions = [
            Transaction(
                id=uuid.uuid4(),
                amount=100.0,
                date=validated_statement.period_start,
                description="Deposit",
                type="credit"
            ),
            Transaction(
                id=uuid.uuid4(),
                amount=-25.0,
                date=validated_statement.period_start,
                description="Withdrawal",
                type="debit"
            )
        ]
        logger.info(f"Loaded {len(transactions)} transactions for account {account_id}")
        return transactions

class CacheService:
    def load_fees_and_rates(self, validated_statement: ValidatedStatement) -> tuple[FeeSchedule, List[CalculationResult]]:
        """Load fee schedule and FX rates from cache."""
        fee_schedule = FeeSchedule(fees=[{"type": "maintenance", "amount": 5.0}])
        calculations = [CalculationResult(fee_amount=5.0, calculation_type="maintenance")]
        return fee_schedule, calculations

class ExternalCallService:
    async def fetch_account_profile(self, account_id: uuid.UUID) -> Optional[AccountProfile]:
        """Fetch account holder profile and formatting preferences."""
        # Mock external API call with retry logic
        await asyncio.sleep(0.1)  # Simulate network delay
        return AccountProfile(
            customer_id=uuid.uuid4(),
            preferences={"language": "en", "currency": "USD"}
        )
    
    async def store_statement(
        self, 
        statement_id: uuid.UUID, 
        statement_document: bytes
    ) -> tuple[str, str]:
        """Store document to object storage and update job status."""
        await asyncio.sleep(0.1)  # Simulate storage operation
        download_url = f"https://storage.example.com/statements/{statement_id}.pdf"
        job_status = "completed"
        return download_url, job_status

class PublisherService:
    async def publish_statement_ready(self, statement_id: uuid.UUID, download_url: str) -> bool:
        """Publish StatementReady domain event."""
        await asyncio.sleep(0.05)  # Simulate publishing
        logger.info(f"Published StatementReady event for {statement_id}")
        return True

class MapperService:
    def build_completed_response(self, existing_statement_id: uuid.UUID) -> StatementAcceptedResponse:
        """Build 202 response for existing completed statement."""
        return StatementAcceptedResponse(
            statement_id=existing_statement_id,
            status="completed",
            message="Statement already available"
        )
    
    def build_restarted_job_response(self, restarted_job_id: uuid.UUID) -> StatementAcceptedResponse:
        """Build 202 response for restarted job."""
        return StatementAcceptedResponse(
            statement_id=restarted_job_id,
            status="restarted",
            message="Statement generation restarted"
        )
    
    def build_running_response(self, existing_statement_id: uuid.UUID) -> StatementAcceptedResponse:
        """Build 202 response for still-running job."""
        return StatementAcceptedResponse(
            statement_id=existing_statement_id,
            status="running",
            message="Statement generation in progress"
        )
    
    def build_recovery_response(self, recovery_result: RecoveryResult) -> StatementAcceptedResponse:
        """Build 202 response for recovered job."""
        return StatementAcceptedResponse(
            status="recovered",
            message=f"Job recovered from {recovery_result.resume_point}"
        )
    
    def build_new_response(self, new_statement_id: uuid.UUID) -> StatementAcceptedResponse:
        """Build 202 response for newly created job."""
        return StatementAcceptedResponse(
            statement_id=new_statement_id,
            status="created",
            message="New statement job created"
        )
    
    def load_formatting_preferences(self, account_profile: AccountProfile) -> FormattingPreferences:
        """Extract formatting preferences from account profile."""
        prefs = account_profile.preferences
        return FormattingPreferences(
            language=prefs.get("language", "en"),
            date_format=prefs.get("date_format", "%Y-%m-%d"),
            currency=prefs.get("currency", "USD")
        )
    
    def use_default_formatting_preferences(self) -> FormattingPreferences:
        """Use system default formatting preferences when profile is unavailable."""
        return FormattingPreferences()
    
    def render_statement_document(
        self,
        account_profile: Optional[AccountProfile],
        formatting_preferences: FormattingPreferences,
        transactions: List[Transaction],
        statement_totals: StatementTotals
    ) -> bytes:
        """Render document with formatting preferences and computed totals."""
        # Mock PDF generation - in production would use proper PDF library
        document_data = {
            "account_profile": account_profile,
            "formatting": formatting_preferences,
            "transactions": [{"amount": t.amount, "date": t.date, "description": t.description} for t in transactions],
            "totals": {
                "total": statement_totals.total_amount,
                "fees": statement_totals.total_fees,
                "net": statement_totals.net_amount
            }
        }
        return json.dumps(document_data).encode('utf-8')
    
    def build_statement_response(self, statement_id: uuid.UUID, download_url: str) -> StatementAcceptedResponse:
        """Build 202 response with download URL."""
        return StatementAcceptedResponse(
            statement_id=statement_id,
            status="completed",
            message="Statement generated successfully",
            download_url=download_url
        )

# Controller
class StatementController:
    def __init__(self):
        self.security_service = SecurityService()
        self.validation_service = ValidationService()
        self.business_rule_service = BusinessRuleService()
        self.repository_service = RepositoryService()
        self.cache_service = CacheService()
        self.external_call_service = ExternalCallService()
        self.publisher_service = PublisherService()
        self.mapper_service = MapperService()
    
    async def generate_monthly_statement(
        self,
        account_id: uuid.UUID,
        statement_request: StatementRequest,
        idempotency_key: str,
        auth_token: str
    ) -> StatementAcceptedResponse:
        """Main endpoint for generating monthly statements."""
        context = Context()
        
        try:
            # AuthenticateRequest
            context.customer_id = self.security_service.authenticate_request(auth_token)
            
            # ValidateStatementRequest
            context.validated_statement, context.existing_statement_id = \
                self.validation_service.validate_statement_request(
                    account_id, statement_request, idempotency_key, context.customer_id
                )
            context.account_id = account_id
            
            # EvaluateStatementStatus
            context.statement_status = self.business_rule_service.evaluate_statement_status(
                account_id, context.validated_statement
            )
            
            # Decision(statementStatus)
            if context.statement_status == StatementStatus.COMPLETED:
                response = self.mapper_service.build_completed_response(context.existing_statement_id)
                return response
            
            elif context.statement_status == StatementStatus.RUNNING:
                # CheckJobProgress
                context.job_progress = self.repository_service.check_job_progress(context.existing_statement_id)
                
                # Decision(jobStalled)
                if context.job_progress == JobProgress.STALLED:
                    # RestartJob
                    context.restarted_job_id = self.repository_service.restart_job(context.existing_statement_id)
                    response = self.mapper_service.build_restarted_job_response(context.restarted_job_id)
                    return response
                else:
                    # BuildRunningResponse
                    response = self.mapper_service.build_running_response(context.existing_statement_id)
                    return response
            
            elif context.statement_status == StatementStatus.FAILED:
                # AnalyzeFailureReason
                context.failure_analysis = self.business_rule_service.analyze_failure_reason(context.existing_statement_id)
                
                # Decision(retryableFailure)
                if context.failure_analysis.retryable:
                    # RecoverFailedJob
                    context.recovery_result, context.resume_point = \
                        self.business_rule_service.recover_failed_job(context.existing_statement_id)
                    response = self.mapper_service.build_recovery_response(context.recovery_result)
                    return response
                else:
                    # CreateNewStatementJob
                    context.new_statement_id = self.repository_service.create_new_statement_job(
                        account_id, context.validated_statement
                    )
                    response = self.mapper_service.build_new_response(context.new_statement_id)
                    return response
            
            else:  # NOT_FOUND
                # CreateStatementJob
                context.statement_id = self.repository_service.create_statement_job(
                    account_id, context.validated_statement
                )
                
                # Fork with parallel execution
                # LoadTransactions, LoadFeesAndRates, FetchAccountProfile
                transactions_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.repository_service.load_transactions,
                        account_id, context.validated_statement
                    )
                )
                
                fees_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.cache_service.load_fees_and_rates,
                        context.validated_statement
                    )
                )
                
                profile_task = asyncio.create_task(
                    self.external_call_service.fetch_account_profile(account_id)
                )
                
                # Execute parallel tasks
                transactions, (fee_schedule, preliminary_calculations), account_profile = await asyncio.gather(
                    transactions_task, fees_task, profile_task, return_exceptions=True
                )
                
                context.transactions = transactions if not isinstance(transactions, Exception) else []
                context.fee_schedule = fee_schedule if not isinstance(fee_schedule, Exception) else None
                context.preliminary_calculations = preliminary_calculations if not isinstance(preliminary_calculations, Exception) else []
                context.account_profile = account_profile if not isinstance(account_profile, Exception) else None
                
                # Decision(profileAvailable) inside FetchAccountProfile branch
                if context.account_profile:
                    context.formatting_preferences = self.mapper_service.load_formatting_preferences(context.account_profile)
                else:
                    context.formatting_preferences = self.mapper_service.use_default_formatting_preferences()
                
                # ComputeStatementTotals
                context.statement_totals = self.business_rule_service.compute_statement_totals(
                    context.transactions, context.fee_schedule, context.preliminary_calculations
                )
                
                # RenderStatementDocument
                context.statement_document = self.mapper_service.render_statement_document(
                    context.account_profile, context.formatting_preferences,
                    context.transactions, context.statement_totals
                )
                
                # StoreStatement
                context.download_url, context.job_status = await self.external_call_service.store_statement(
                    context.statement_id, context.statement_document
                )
                
                # PublishStatementReady
                context.event_published = await self.publisher_service.publish_statement_ready(
                    context.statement_id, context.download_url
                )
                
                # BuildStatementResponse
                response = self.mapper_service.build_statement_response(
                    context.statement_id, context.download_url
                )
                return response
                
        except (Unauthorized, Forbidden, BadRequest, Conflict, NotFound, 
                ProfileServiceUnavailable, StorageUnavailable, EventPublishFailed,
                DataCorruption, AnalysisUnavailable) as e:
            logger.error(f"Statement generation failed: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during statement generation: {str(e)}")
            raise

# Flask-like REST endpoint (for demonstration)
from flask import Flask, request, jsonify

app = Flask(__name__)
controller = StatementController()

@app.route("/accounts/<account_id>/statements", methods=["POST"])
async def generate_statement(account_id):
    try:
        # Extract request data
        data = request.get_json()
        auth_token = request.headers.get("Authorization", "")
        idempotency_key = request.headers.get("Idempotency-Key", str(uuid.uuid4()))
        
        statement_request = StatementRequest(
            period_start=datetime.fromisoformat(data["period_start"]),
            period_end=datetime.fromisoformat(data["period_end"]),
            format=data.get("format", "PDF")
        )
        
        # Process request
        response = await controller.generate_monthly_statement(
            uuid.UUID(account_id),
            statement_request,
            idempotency_key,
            auth_token
        )
        
        return jsonify({
            "statement_id": str(response.statement_id) if response.statement_id else None,
            "status": response.status,
            "message": response.message,
            "download_url": response.download_url
        }), 202
        
    except Unauthorized:
        return jsonify({"error": "Unauthorized"}), 401
    except Forbidden:
        return jsonify({"error": "Forbidden"}), 403
    except NotFound:
        return jsonify({"error": "Not Found"}), 404
    except Conflict:
        return jsonify({"error": "Conflict"}), 409
    except BadRequest:
        return jsonify({"error": "Bad Request"}), 400
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "detail": str(e)}), 500

if __name__ == "__main__":
    # Example usage
    async def main():
        # Mock request data
        account_id = uuid.uuid4()
        statement_request = StatementRequest(
            period_start=datetime(2024, 1, 1),
            period_end=datetime(2024, 1, 31),
            format="PDF"
        )
        
        controller = StatementController()
        response = await controller.generate_monthly_statement(
            account_id,
            statement_request,
            str(uuid.uuid4()),
            "Bearer mock-jwt-token"
        )
        
        print(f"Response: {response}")
    
    # Run example
    asyncio.run(main())