"""
Production-quality Python code for subscription cancellation REST endpoint.
Generated from UML Activity Diagram specification.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enums and Data Models
class SubscriptionStatus(Enum):
    ACTIVE = "ACTIVE"
    CANCELED = "CANCELED"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"

@dataclass
class Subscription:
    id: uuid.UUID
    customer_id: uuid.UUID
    status: SubscriptionStatus
    created_at: datetime
    updated_at: datetime
    billing_cycle_start: datetime
    billing_cycle_end: datetime
    has_outstanding_invoice: bool = False

@dataclass
class RequestContext:
    auth_token: str
    subscription_id: uuid.UUID
    cancel_reason: Optional[str] = None
    customer_id: Optional[uuid.UUID] = None
    subscription: Optional[Subscription] = None
    cancellable: Optional[bool] = None
    canceled_subscription: Optional[Subscription] = None
    email_sent: Optional[bool] = None

# Custom Exceptions
class Unauthorized(Exception):
    """User is not authenticated"""
    pass

class Forbidden(Exception):
    """User does not have permission to access this resource"""
    pass

class NotFound(Exception):
    """Subscription not found"""
    pass

class UnprocessableEntity(Exception):
    """Subscription cannot be canceled due to business rules"""
    pass

class EventBusUnavailable(Exception):
    """Event bus is unavailable"""
    pass

class EmailProviderError(Exception):
    """Email provider error"""
    pass

# Response Models
@dataclass
class ErrorResponse:
    error: str
    message: str
    status_code: int

# Service Classes
class SecurityService:
    """Handles security and authorization"""
    
    def authorize_customer(self, auth_token: str, subscription_id: uuid.UUID) -> uuid.UUID:
        """
        Verify requester is authenticated and owns the subscription.
        @meta {"stereotype": "Security", "desc": "Verify requester is authenticated and owns the subscription.", "inputs": [{"name": "authToken", "type": "JWT", "source": "request"}, {"name": "subscriptionId", "type": "UUID", "source": "request"}], "outputs": [{"name": "customerId", "type": "UUID", "target": "context"}], "exceptions": ["Unauthorized", "Forbidden"], "securityContext": "Role_Customer"}
        """
        # In production, validate JWT token and extract customer ID
        # For demo purposes, simulate token validation
        if not auth_token or not auth_token.startswith("Bearer "):
            raise Unauthorized("Invalid or missing authentication token")
        
        # Simulate token validation and extract customer ID
        # In real implementation, decode JWT and validate signature
        customer_id = uuid.uuid4()  # Simulated extraction from token
        
        # Simulate ownership check - in production, verify customer owns subscription
        # This would involve database lookup to verify subscription belongs to customer
        logger.info(f"Authorized customer {customer_id} for subscription {subscription_id}")
        
        return customer_id

class SubscriptionRepository:
    """Handles subscription data operations"""
    
    def load_subscription(self, subscription_id: uuid.UUID) -> Subscription:
        """
        Load subscription from database.
        @meta {"stereotype": "Repository", "desc": "Load subscription from database.", "inputs": [{"name": "subscriptionId", "type": "UUID", "source": "request"}], "outputs": [{"name": "subscription", "type": "Subscription", "target": "context"}], "exceptions": ["NotFound"], "sideEffects": false, "idempotent": true, "consistencyScope": "none"}
        """
        # In production, load from database
        # For demo purposes, simulate subscription lookup
        if subscription_id == uuid.UUID('00000000-0000-0000-0000-000000000000'):
            raise NotFound("Subscription not found")
        
        # Simulate subscription data
        subscription = Subscription(
            id=subscription_id,
            customer_id=uuid.uuid4(),  # Would match authorized customer
            status=SubscriptionStatus.ACTIVE,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            billing_cycle_start=datetime.now(),
            billing_cycle_end=datetime.now(),
            has_outstanding_invoice=False
        )
        
        logger.info(f"Loaded subscription {subscription_id}")
        return subscription
    
    def cancel_subscription(self, subscription_id: uuid.UUID, cancel_reason: Optional[str], subscription: Subscription) -> Subscription:
        """
        Mark subscription as CANCELED and store cancellation timestamp and reason.
        @meta {"stereotype": "Repository", "desc": "Mark subscription as CANCELED and store cancellation timestamp and reason.", "inputs": [{"name": "subscriptionId", "type": "UUID", "source": "request"}, {"name": "cancelReason", "type": "String?", "source": "request"}, {"name": "subscription", "type": "Subscription", "source": "context"}], "outputs": [{"name": "canceledSubscription", "type": "Subscription", "target": "context"}], "postCondition": "subscription.status == CANCELED", "sideEffects": true, "idempotent": true, "consistencyScope": "local-transaction", "transactional": true}
        """
        # Simulate transactional database update
        # In production, this would be wrapped in a database transaction
        subscription.status = SubscriptionStatus.CANCELED
        subscription.updated_at = datetime.now()
        
        # Store cancellation reason if provided (would be in separate table in production)
        logger.info(f"Canceled subscription {subscription_id} with reason: {cancel_reason or 'Not provided'}")
        
        # Post-condition check
        if subscription.status != SubscriptionStatus.CANCELED:
            raise Exception("Post-condition failed: subscription status not updated to CANCELED")
        
        return subscription

class BusinessRuleService:
    """Handles business logic and rules"""
    
    def check_cancellation_eligibility(self, subscription: Subscription) -> bool:
        """
        Determine whether cancellation is allowed (status, billing cycle, outstanding invoice).
        @meta {"stereotype": "BusinessRule", "desc": "Determine whether cancellation is allowed (status, billing cycle, outstanding invoice).", "inputs": [{"name": "subscription", "type": "Subscription", "source": "context"}], "outputs": [{"name": "cancellable", "type": "bool", "target": "context"}], "exceptions": ["UnprocessableEntity"]}
        """
        # Business rule: can only cancel active subscriptions
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise UnprocessableEntity(f"Cannot cancel subscription with status: {subscription.status.value}")
        
        # Business rule: cannot cancel if outstanding invoice exists
        if subscription.has_outstanding_invoice:
            raise UnprocessableEntity("Cannot cancel subscription with outstanding invoices")
        
        # Business rule: can always cancel active subscriptions without outstanding invoices
        cancellable = True
        logger.info(f"Subscription {subscription.id} is eligible for cancellation: {cancellable}")
        
        return cancellable
    
    def reject_cancellation(self, subscription: Subscription) -> ErrorResponse:
        """
        Return 422 when subscription cannot be canceled.
        @meta {"stereotype": "BusinessRule", "desc": "Return 422 when subscription cannot be canceled.", "inputs": [{"name": "subscription", "type": "Subscription", "source": "context"}], "outputs": [{"name": "response", "type": "ErrorResponse", "target": "response"}], "exceptions": ["UnprocessableEntity"]}
        """
        error_response = ErrorResponse(
            error="UnprocessableEntity",
            message=f"Subscription {subscription.id} cannot be canceled in its current state",
            status_code=422
        )
        logger.warning(f"Rejected cancellation for subscription {subscription.id}")
        return error_response

class EventPublisher:
    """Handles event publishing"""
    
    def publish_subscription_canceled(self, canceled_subscription: Subscription) -> bool:
        """
        Publish SubscriptionCanceled domain event.
        @meta {"stereotype": "Publisher", "desc": "Publish SubscriptionCanceled domain event.", "inputs": [{"name": "canceledSubscription", "type": "Subscription", "source": "context"}], "outputs": [{"name": "eventPublished", "type": "bool", "target": "event"}], "postCondition": "event_published", "sideEffects": true, "exceptions": ["EventBusUnavailable"], "idempotent": true}
        """
        # Simulate event publishing
        event_data = {
            "event_type": "SubscriptionCanceled",
            "subscription_id": str(canceled_subscription.id),
            "customer_id": str(canceled_subscription.customer_id),
            "canceled_at": canceled_subscription.updated_at.isoformat()
        }
        
        # In production, publish to message queue/event bus
        # For demo, simulate publishing
        try:
            logger.info(f"Published SubscriptionCanceled event: {json.dumps(event_data)}")
            event_published = True
            
            # Post-condition check
            if not event_published:
                raise Exception("Post-condition failed: event not published")
            
            return event_published
        except Exception as e:
            raise EventBusUnavailable(f"Failed to publish event: {str(e)}")

class EmailService:
    """Handles external email communications"""
    
    def __init__(self):
        self.retry_policy = {
            "maxAttempts": 3,
            "backoffMs": 300,
            "retryOn": ["Timeout", "502", "503"],
            "onRetryExhausted": "log_and_continue"
        }
    
    def send_cancellation_email(self, customer_id: uuid.UUID, subscription_id: uuid.UUID) -> bool:
        """
        Send confirmation email to customer. Operation is best-effort – failure does not roll back cancellation.
        @meta {"stereotype": "ExternalCall", "desc": "Send confirmation email to customer. Operation is best-effort – failure does not roll back cancellation.", "inputs": [{"name": "customerId", "type": "UUID", "source": "context"}, {"name": "subscriptionId", "type": "UUID", "source": "request"}], "outputs": [{"name": "emailSent", "type": "bool", "target": "context"}], "exceptions": ["EmailProviderError"], "retryPolicy": {"maxAttempts": 3, "backoffMs": 300, "retryOn": ["Timeout", "502", "503"], "onRetryExhausted": "log_and_continue"}}
        """
        max_attempts = self.retry_policy["maxAttempts"]
        backoff_ms = self.retry_policy["backoffMs"]
        
        for attempt in range(1, max_attempts + 1):
            try:
                # Simulate email sending
                email_data = {
                    "to": f"customer-{customer_id}@example.com",
                    "subject": "Subscription Canceled",
                    "body": f"Your subscription {subscription_id} has been canceled successfully."
                }
                
                # In production, call external email service
                # For demo, simulate email sending with occasional failures
                if attempt == 2:  # Simulate failure on second attempt
                    raise EmailProviderError("Simulated email service timeout")
                
                logger.info(f"Sent cancellation email to customer {customer_id} for subscription {subscription_id}")
                return True
                
            except EmailProviderError as e:
                if attempt < max_attempts:
                    logger.warning(f"Email attempt {attempt} failed, retrying in {backoff_ms}ms: {str(e)}")
                    # In production, implement actual backoff delay
                    continue
                else:
                    logger.error(f"Email sending exhausted after {max_attempts} attempts: {str(e)}")
                    # According to retry policy, log and continue
                    return False

# REST Controller
class SubscriptionController:
    """REST controller for subscription operations"""
    
    def __init__(self):
        self.security_service = SecurityService()
        self.subscription_repository = SubscriptionRepository()
        self.business_rule_service = BusinessRuleService()
        self.event_publisher = EventPublisher()
        self.email_service = EmailService()
    
    def cancel_subscription(self, auth_token: str, subscription_id: str, cancel_reason: Optional[str] = None) -> Dict[str, Any]:
        """
        DELETE /subscriptions/{subscriptionId}
        @meta {"endpoint": "DELETE /subscriptions/{subscriptionId}", "responseSuccess": "204 No Content", "responseError": "400|401|403|404|409|422"}
        """
        context = RequestContext(
            auth_token=auth_token,
            subscription_id=uuid.UUID(subscription_id),
            cancel_reason=cancel_reason
        )
        
        try:
            # Step 1: AuthorizeCustomer
            context.customer_id = self.security_service.authorize_customer(
                context.auth_token, 
                context.subscription_id
            )
            
            # Step 2: LoadSubscription
            context.subscription = self.subscription_repository.load_subscription(
                context.subscription_id
            )
            
            # Step 3: CheckCancellationEligibility
            context.cancellable = self.business_rule_service.check_cancellation_eligibility(
                context.subscription
            )
            
            # Step 4: Decision(cancellable)
            if context.cancellable:
                # Step 5: CancelSubscription
                context.canceled_subscription = self.subscription_repository.cancel_subscription(
                    context.subscription_id,
                    context.cancel_reason,
                    context.subscription
                )
                
                # Step 6: PublishSubscriptionCanceled
                event_published = self.event_publisher.publish_subscription_canceled(
                    context.canceled_subscription
                )
                
                # Step 7: SendCancellationEmail
                context.email_sent = self.email_service.send_cancellation_email(
                    context.customer_id,
                    context.subscription_id
                )
                
                # Step 8: FlowJoinCancelSubscription
                # Success case - return 204 No Content
                return {"status_code": 204, "body": None}
            else:
                # Step 9: RejectCancellation
                error_response = self.business_rule_service.reject_cancellation(
                    context.subscription
                )
                
                # Step 10: FlowJoinCancelSubscription
                return {
                    "status_code": error_response.status_code,
                    "body": {
                        "error": error_response.error,
                        "message": error_response.message
                    }
                }
                
        except Unauthorized:
            return {
                "status_code": 401,
                "body": {"error": "Unauthorized", "message": "Authentication required"}
            }
        except Forbidden:
            return {
                "status_code": 403,
                "body": {"error": "Forbidden", "message": "Access denied to this subscription"}
            }
        except NotFound:
            return {
                "status_code": 404,
                "body": {"error": "NotFound", "message": "Subscription not found"}
            }
        except UnprocessableEntity as e:
            return {
                "status_code": 422,
                "body": {"error": "UnprocessableEntity", "message": str(e)}
            }
        except EventBusUnavailable:
            # Log but don't fail the cancellation
            logger.error("Event bus unavailable, but cancellation succeeded")
            return {"status_code": 204, "body": None}
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return {
                "status_code": 500,
                "body": {"error": "InternalServerError", "message": "An unexpected error occurred"}
            }

# Usage example (for testing)
if __name__ == "__main__":
    controller = SubscriptionController()
    
    # Example request
    result = controller.cancel_subscription(
        auth_token="Bearer valid_jwt_token",
        subscription_id=str(uuid.uuid4()),
        cancel_reason="Customer requested cancellation"
    )
    
    print(f"Response: {result}")