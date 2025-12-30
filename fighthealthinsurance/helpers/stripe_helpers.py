"""
Stripe webhook handling helpers for Fight Health Insurance.

Provides utilities for processing Stripe payment webhooks.
"""

from typing import Any, Optional
from urllib.parse import urlencode

from django.urls import reverse
from loguru import logger

from fighthealthinsurance.models import (
    FaxesToSend,
    InterestedProfessional,
    LostStripeSession,
    StripeWebhookEvents,
)
from fhi_users.models import ProfessionalUser, UserDomain
from fhi_users import emails as fhi_emails


class StripeWebhookHelper:
    """Helper class for processing Stripe webhook events."""

    @staticmethod
    def handle_checkout_session_completed(request: Any, session: Any) -> None:
        """
        Handle a completed Stripe checkout session.

        Processes payment confirmations for various payment types:
        - interested_professional_signup
        - professional_domain_subscription
        - fax

        Args:
            request: Django request object
            session: Stripe session object or dict
        """
        try:
            # Handle both Stripe object and dict access patterns
            try:
                metadata = session.metadata
            except (AttributeError, TypeError):
                metadata = session["metadata"]

            payment_type: Optional[str] = None
            if "payment_type" in metadata:
                payment_type = metadata.get("payment_type")
            else:
                logger.debug(f"No payment type field found in {session}")

            if payment_type == "interested_professional_signup":
                InterestedProfessional.objects.filter(
                    id=metadata.get("interested_professional_id")
                ).update(paid=True)

            elif payment_type == "professional_domain_subscription":
                logger.debug(f"Processing professional domain subscription {session}")
                # Handle both Stripe object and dict access patterns
                if isinstance(session, dict):
                    subscription_id = session.get("subscription")
                    customer_id = session.get("customer")
                else:
                    subscription_id = getattr(session, "subscription", None)
                    customer_id = getattr(session, "customer", None)
                if subscription_id:
                    UserDomain.objects.filter(id=metadata.get("domain_id")).update(
                        stripe_subscription_id=subscription_id,
                        stripe_customer_id=customer_id,
                        active=True,
                        pending=False,
                    )
                    ProfessionalUser.objects.filter(
                        id=metadata.get("professional_id")
                    ).update(active=True)
                    user = ProfessionalUser.objects.get(
                        id=metadata.get("professional_id")
                    ).user
                    fhi_emails.send_verification_email(request, user, first_only=True)
                else:
                    logger.opt(exception=True).error(
                        "No subscription ID in completed checkout session"
                    )

            elif payment_type == "fax":
                # metadata is already extracted above to handle both object and dict
                fax_uuid = metadata.get("uuid") if metadata else None
                if fax_uuid:
                    FaxesToSend.objects.filter(uuid=fax_uuid).update(
                        paid=True, should_send=True
                    )
                else:
                    logger.warning("No uuid in metadata for fax payment")
            else:
                logger.warning(f"Unknown payment type: {payment_type}")
        except Exception as e:
            logger.opt(exception=True).error("Error processing checkout session")
            raise e

    @staticmethod
    def handle_checkout_session_expired(request: Any, session: Any) -> None:
        """
        Handle an expired Stripe checkout session.

        Creates a LostStripeSession record and sends a follow-up email
        to help users complete their payment.

        Args:
            request: Django request object
            session: Stripe session object or dict
        """
        try:
            logger.debug(f"Checkout session expired: {session}")
            # Handle both Stripe object and dict access patterns
            try:
                metadata = session.metadata
            except (AttributeError, TypeError):
                metadata = session["metadata"]
            payment_type = metadata.get("payment_type")
            item = "Fight Health Insurance / Fight paperwork"
            finish_link = None

            if payment_type == "fax":
                item = "Fight Health Insurance Fax"
            elif payment_type == "professional_domain_subscription":
                item = "Fight Paperwork Professional Domain Subscription"
                # Check if the domain is already active (due to another checkout session)
                domain_id = metadata.get("domain_id")
                if domain_id:
                    try:
                        domain = UserDomain.objects.get(id=domain_id)
                        if domain.active and domain.stripe_subscription_id:
                            logger.info(
                                f"Domain {domain_id} is already active with subscription, ignoring expired checkout"
                            )
                            return
                    except UserDomain.DoesNotExist:
                        logger.info(
                            f"Domain {domain_id} no longer exists, might have been recreated"
                        )

                # Temporary until the FPW UI is ready
                finish_base_link = (
                    "https://www.fightpaperwork.com/stripe/finish-checkout"
                )
                params = urlencode(
                    {
                        "domain_id": domain_id,
                        "professional_id": metadata.get("professional_id"),
                    }
                )
                finish_link = f"{finish_base_link}?{params}"

            # Try to extract email from various session locations
            email: Optional[str] = None
            try:
                try:
                    email = session.customer_email
                except (AttributeError, TypeError):
                    email = session["customer_email"]
            except (KeyError, TypeError):
                pass
            if email is None:
                try:
                    try:
                        email = session.customer_details.email
                    except (AttributeError, TypeError):
                        email = session["customer_details"]["email"]
                except (KeyError, TypeError, AttributeError):
                    pass
            if email is None:
                logger.debug(
                    "No email found in expired checkout session can't send e-mail"
                )
                return

            session_id = None
            if hasattr(session, "id"):
                session_id = session.id

            # Check if we already have a record for this session or similar data
            # to avoid sending duplicate emails
            existing_session = None
            if session_id:
                existing_session = LostStripeSession.objects.filter(
                    session_id=session_id
                ).first()

            if not existing_session and email and payment_type:
                existing_session = LostStripeSession.objects.filter(
                    email=email, payment_type=payment_type
                ).first()

            if existing_session:
                logger.debug(
                    f"Skipping duplicate lost stripe session notification for {email}"
                )
                return

            # For professional domain subscriptions, check if the user has successfully
            # created a domain in another session
            if payment_type == "professional_domain_subscription" and metadata.get(
                "professional_id"
            ):
                try:
                    professional_id = metadata.get("professional_id")
                    professional = ProfessionalUser.objects.get(id=professional_id)
                    if professional:
                        # Check if the user has active domains
                        active_domains = UserDomain.objects.filter(
                            professionaldomainrelation__professional=professional,
                            professionaldomainrelation__active_domain_relation=True,
                            active=True,
                        ).exists()

                        if active_domains:
                            logger.info(
                                f"User {professional} already has active domains, not creating LostStripeSession"
                            )
                            return
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Error checking for active domains for user {email}: {e}"
                    )

            lost_session = LostStripeSession.objects.create(
                payment_type=payment_type,
                email=email,
                session_id=session_id,
                metadata=metadata,
            )
            if finish_link is None:
                finish_link_base = reverse("complete_payment")
                finish_link = f"{finish_link_base}?session_id={lost_session.id}"
            if finish_link:
                fhi_emails.send_checkout_session_expired(
                    request,
                    email=email,
                    item=item,
                    link=finish_link,
                )
            else:
                logger.debug(f"Could not create finish link for {payment_type}")
        except Exception as e:
            logger.opt(exception=True).error(
                "Error processing expired checkout session"
            )
            raise

    @staticmethod
    def handle_stripe_webhook(request: Any, event: Any) -> None:
        """
        Main entry point for handling Stripe webhook events.

        Implements idempotency by checking for duplicate events.

        Args:
            request: Django request object
            event: Stripe event object
        """
        # Atomically get or create the webhook event record to avoid TOCTOU race
        event_id = event.id
        webhook_event, created = StripeWebhookEvents.objects.get_or_create(
            event_stripe_id=event_id,
            defaults={"success": False},
        )

        # If the record already existed, this is a duplicate event
        if not created:
            logger.debug(f"Skipping duplicate stripe event {event_id}")
            return

        try:
            if event.type == "checkout.session.completed":
                StripeWebhookHelper.handle_checkout_session_completed(
                    request, event.data.object
                )
            elif event.type == "checkout.session.expired":
                StripeWebhookHelper.handle_checkout_session_expired(
                    request, event.data.object
                )
            else:
                logger.debug(f"Unhandled stripe event type {event.type}")

            # Update the webhook event record on success
            webhook_event.success = True
            webhook_event.save()
        except Exception as e:
            # Record the error
            webhook_event.error = str(e)[:255]  # Limit to field size
            webhook_event.save()
            logger.opt(exception=True).error(
                f"Error processing stripe webhook {event_id}"
            )
            raise
