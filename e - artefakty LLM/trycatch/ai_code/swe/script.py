"""
User Registration REST API Controller
Generated from UML Activity Diagram specification

Implements deterministic flow with exception handling and timeout management.
"""

import uuid
import hashlib
import time
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum
import json
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Custom Exception Classes
class ValidationException(Exception):
    """Raised when user input validation fails"""
    pass

class ConflictException(Exception):
    """Raised when resource conflict occurs (e.g., duplicate email)"""
    pass

class TimeoutException(Exception):
    """Raised when operation times out"""
    pass

class DisposableEmailDetected(Exception):
    """Raised when disposable email domain is detected"""
    pass

class RegistrationException(Exception):
    """Base registration exception"""
    pass

# Data Models
@dataclass
class RegistrationDTO:
    """User registration request data"""
    email: str
    password: str
    first_name: str
    last_name: str
    phone: Optional[str] = None

@dataclass
class ValidatedUserData:
    """Validated user data"""
    email: str
    password_hash: str
    first_name: str
    last_name: str
    phone: Optional[str]

@dataclass
class ErrorResponse:
    """Error response model"""
    error_code: str
    message: str
    timestamp: datetime
    request_id: Optional[str] = None

@dataclass
class RegistrationContext:
    """Context for storing flow data"""
    validated_user_data: Optional[ValidatedUserData] = None
    email_allowed: Optional[bool] = None
    user_id: Optional[uuid.UUID] = None
    email: Optional[str] = None
    email_sent_status: Optional[bool] = None
    request_id: Optional[uuid.UUID] = None

# Service Classes
class ValidationService:
    """Handles input validation"""
    
    @staticmethod
    def validate_user_input(registration_dto: RegistrationDTO) -> ValidatedUserData:
        """Validate registration DTO structure and required fields."""
        try:
            # Validate email format
            if not registration_dto.email or '@' not in registration_dto.email:
                raise ValidationException("Invalid email format")
            
            # Validate password
            if not registration_dto.password or len(registration_dto.password) < 8:
                raise ValidationException("Password must be at least 8 characters")
            
            # Validate names
            if not registration_dto.first_name or not registration_dto.last_name:
                raise ValidationException("First and last name are required")
            
            # Hash password
            password_hash = hashlib.sha256(registration_dto.password.encode()).hexdigest()
            
            return ValidatedUserData(
                email=registration_dto.email.lower(),
                password_hash=password_hash,
                first_name=registration_dto.first_name.strip(),
                last_name=registration_dto.last_name.strip(),
                phone=registration_dto.phone
            )
            
        except Exception as e:
            if isinstance(e, ValidationException):
                raise
            raise ValidationException(f"Validation failed: {str(e)}")

class UserRepository:
    """Handles user database operations"""
    
    # Simulated database
    _existing_emails = set(["test@example.com", "admin@example.com"])
    _users = {}
    
    @staticmethod
    def check_email_uniqueness(user_data: ValidatedUserData) -> None:
        """Verify email address is not already registered."""
        # Simulate database query
        time.sleep(0.1)  # Simulate DB latency
        
        if user_data.email in UserRepository._existing_emails:
            raise ConflictException(f"Email {user_data.email} already registered")
    
    @staticmethod
    def create_user_account(user_data: ValidatedUserData) -> uuid.UUID:
        """Persist new user account with hashed credentials."""
        # Simulate database operation
        time.sleep(0.2)  # Simulate DB write latency
        
        user_id = uuid.uuid4()
        UserRepository._users[user_id] = {
            "email": user_data.email,
            "password_hash": user_data.password_hash,
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "phone": user_data.phone,
            "status": "pending",
            "created_at": datetime.now()
        }
        
        # Add to existing emails
        UserRepository._existing_emails.add(user_data.email)
        
        logger.info(f"Created user account for {user_data.email} with ID {user_id}")
        return user_id
    
    @staticmethod
    def finalize_registration(user_id: uuid.UUID) -> uuid.UUID:
        """Mark registration as complete and set user status active."""
        # Simulate database update
        time.sleep(0.1)
        
        if user_id not in UserRepository._users:
            raise RegistrationException(f"User {user_id} not found")
        
        UserRepository._users[user_id]["status"] = "active"
        UserRepository._users[user_id]["activated_at"] = datetime.now()
        
        logger.info(f"Finalized registration for user {user_id}")
        return user_id

class EmailFilterService:
    """Handles email domain filtering"""
    
    # Simulated disposable email domains
    _disposable_domains = {
        "10minutemail.com", "tempmail.org", "guerrillamail.com",
        "mailinator.com", "throwaway.email", "fakeinbox.com"
    }
    
    @staticmethod
    def filter_disposable_email_domains(user_data: ValidatedUserData) -> bool:
        """Reject registrations from known disposable or temporary email domains."""
        try:
            domain = user_data.email.split('@')[1].lower()
            
            if domain in EmailFilterService._disposable_domains:
                logger.warning(f"Disposable email detected: {domain}")
                raise DisposableEmailDetected(f"Disposable email domain not allowed: {domain}")
            
            # Log audit for high-priority validation
            logger.info(f"Email domain approved: {domain}")
            return True
            
        except DisposableEmailDetected:
            raise
        except Exception as e:
            logger.error(f"Email filtering error: {str(e)}")
            return False

class EmailService:
    """Handles external email sending"""
    
    @staticmethod
    def send_activation_email(user_id: uuid.UUID, email: str) -> bool:
        """Send activation email with confirmation link."""
        # Simulate external email service call
        time.sleep(0.5)  # Simulate network latency
        
        # Simulate occasional timeout
        import random
        if random.random() < 0.1:  # 10% chance of timeout
            raise TimeoutException("Email service timeout")
        
        # Simulate email sending
        activation_link = f"https://example.com/activate/{user_id}"
        logger.info(f"Activation email sent to {email} with link {activation_link}")
        
        return True

class ErrorHandlerService:
    """Handles error conditions and responses"""
    
    @staticmethod
    def handle_registration_timeout(user_id: Optional[uuid.UUID] = None) -> ErrorResponse:
        """Handle timeout – log failure and return error response."""
        error_msg = f"Registration timeout occurred"
        if user_id:
            error_msg += f" for user {user_id}"
        
        logger.error(error_msg)
        
        return ErrorResponse(
            error_code="TIMEOUT",
            message="Registration process timed out. Please try again.",
            timestamp=datetime.now(),
            request_id=str(uuid.uuid4())
        )

class AuditService:
    """Handles audit logging"""
    
    @staticmethod
    def log_event(event_type: str, details: Dict[str, Any]) -> None:
        """Log audit event"""
        audit_entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "details": details
        }
        logger.info(f"AUDIT: {json.dumps(audit_entry)}")

# Retry Policy Implementation
class RetryPolicy:
    """Implements retry logic with exponential backoff"""
    
    @staticmethod
    def execute_with_retry(func, *args, max_attempts: int = 3, backoff_ms: int = 300, 
                          retry_on: List[str] = None, **kwargs):
        """Execute function with retry policy"""
        if retry_on is None:
            retry_on = ["Timeout", "502", "503"]
        
        last_exception = None
        
        for attempt in range(max_attempts):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                
                # Check if exception should be retried
                should_retry = any(retry_reason in str(e) for retry_reason in retry_on)
                
                if attempt == max_attempts - 1 or not should_retry:
                    raise e
                
                # Wait with backoff
                wait_time = backoff_ms * (2 ** attempt) / 1000
                logger.warning(f"Attempt {attempt + 1} failed, retrying in {wait_time}s: {str(e)}")
                time.sleep(wait_time)
        
        raise last_exception

# Main Controller
class UserRegistrationController:
    """REST Controller for user registration"""
    
    def __init__(self):
        self.validation_service = ValidationService()
        self.user_repository = UserRepository()
        self.email_filter_service = EmailFilterService()
        self.email_service = EmailService()
        self.error_handler_service = ErrorHandlerService()
        self.audit_service = AuditService()
    
    def register_user(self, registration_dto: RegistrationDTO) -> Dict[str, Any]:
        """
        POST /users/register
        Register a new user with validation and email verification
        """
        context = RegistrationContext()
        request_id = uuid.uuid4()
        
        try:
            # Step 1: Validate User Input
            context.validated_user_data = self.validation_service.validate_user_input(registration_dto)
            self.audit_service.log_event("validation_success", {"email": context.validated_user_data.email})
            
            # Step 2: Check Email Uniqueness
            try:
                self.user_repository.check_email_uniqueness(context.validated_user_data)
            except ConflictException as e:
                self.audit_service.log_event("email_conflict", {"email": context.validated_user_data.email})
                return self._create_error_response(409, "Email already registered", request_id)
            
            # Step 3: Filter Disposable Email Domains
            try:
                context.email_allowed = self.email_filter_service.filter_disposable_email_domains(context.validated_user_data)
                self.audit_service.log_event("email_filter_success", {"email": context.validated_user_data.email})
            except DisposableEmailDetected as e:
                self.audit_service.log_event("disposable_email_blocked", {"email": context.validated_user_data.email})
                return self._create_error_response(422, "Disposable email domains not allowed", request_id)
            
            # Decision: isTimeout - wrapped in try-catch for timeout handling
            try:
                # Step 4: Create User Account (if email allowed)
                if context.email_allowed:
                    try:
                        context.user_id = self.user_repository.create_user_account(context.validated_user_data)
                        context.email = context.validated_user_data.email
                        self.audit_service.log_event("user_created", {"user_id": str(context.user_id)})
                    except TimeoutException:
                        # Timeout during user creation
                        error_response = self.error_handler_service.handle_registration_timeout()
                        return self._format_error_response(error_response, 504)
                    
                    # Step 5: Send Activation Email
                    try:
                        context.email_sent_status = RetryPolicy.execute_with_retry(
                            self.email_service.send_activation_email,
                            context.user_id,
                            context.email,
                            max_attempts=3,
                            backoff_ms=300,
                            retry_on=["Timeout", "502", "503"]
                        )
                        self.audit_service.log_event("activation_email_sent", {"user_id": str(context.user_id)})
                    except TimeoutException:
                        # Timeout during email sending
                        error_response = self.error_handler_service.handle_registration_timeout(context.user_id)
                        return self._format_error_response(error_response, 504)
                    
                    # Step 6: Finalize Registration
                    if context.email_sent_status:
                        try:
                            context.request_id = self.user_repository.finalize_registration(context.user_id)
                            self.audit_service.log_event("registration_completed", {"user_id": str(context.user_id)})
                        except TimeoutException:
                            # Timeout during finalization
                            error_response = self.error_handler_service.handle_registration_timeout(context.user_id)
                            return self._format_error_response(error_response, 504)
                
                # Success response
                return {
                    "status_code": 201,
                    "body": {
                        "message": "User registered successfully",
                        "user_id": str(context.user_id) if context.user_id else None,
                        "email": context.email,
                        "request_id": str(context.request_id) if context.request_id else str(request_id)
                    },
                    "headers": {"Content-Type": "application/json"}
                }
                
            except TimeoutException:
                # General timeout handling
                error_response = self.error_handler_service.handle_registration_timeout(context.user_id)
                return self._format_error_response(error_response, 504)
                
        except ValidationException as e:
            self.audit_service.log_event("validation_failed", {"error": str(e)})
            return self._create_error_response(400, str(e), request_id)
        except Exception as e:
            logger.error(f"Unexpected error during registration: {str(e)}")
            self.audit_service.log_event("registration_error", {"error": str(e)})
            return self._create_error_response(500, "Internal server error", request_id)
    
    def _create_error_response(self, status_code: int, message: str, request_id: uuid.UUID) -> Dict[str, Any]:
        """Create standardized error response"""
        return {
            "status_code": status_code,
            "body": {
                "error": message,
                "request_id": str(request_id),
                "timestamp": datetime.now().isoformat()
            },
            "headers": {"Content-Type": "application/json"}
        }
    
    def _format_error_response(self, error_response: ErrorResponse, status_code: int) -> Dict[str, Any]:
        """Format error response from ErrorHandlerService"""
        return {
            "status_code": status_code,
            "body": {
                "error_code": error_response.error_code,
                "message": error_response.message,
                "request_id": error_response.request_id,
                "timestamp": error_response.timestamp.isoformat()
            },
            "headers": {"Content-Type": "application/json"}
        }

# Flask/FastAPI style endpoint wrapper
def create_registration_endpoint():
    """Create the registration endpoint function"""
    controller = UserRegistrationController()
    
    def register_user_endpoint(request_data: Dict[str, Any]) -> Dict[str, Any]:
        """POST /users/register endpoint implementation"""
        try:
            # Parse request data
            registration_dto = RegistrationDTO(
                email=request_data.get("email", ""),
                password=request_data.get("password", ""),
                first_name=request_data.get("first_name", ""),
                last_name=request_data.get("last_name", ""),
                phone=request_data.get("phone")
            )
            
            # Process registration
            return controller.register_user(registration_dto)
            
        except Exception as e:
            request_id = uuid.uuid4()
            return {
                "status_code": 400,
                "body": {
                    "error": f"Invalid request format: {str(e)}",
                    "request_id": str(request_id),
                    "timestamp": datetime.now().isoformat()
                },
                "headers": {"Content-Type": "application/json"}
            }
    
    return register_user_endpoint

# Example usage
if __name__ == "__main__":
    # Test the registration flow
    endpoint = create_registration_endpoint()
    
    # Test case 1: Valid registration
    test_request = {
        "email": "newuser@example.com",
        "password": "securepassword123",
        "first_name": "John",
        "last_name": "Doe"
    }
    
    result = endpoint(test_request)
    print("Test Result:", json.dumps(result, indent=2, default=str))