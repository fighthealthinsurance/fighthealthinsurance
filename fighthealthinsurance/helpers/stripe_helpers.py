"""
Stripe webhook handling helpers for Fight Health Insurance.

Provides utilities for processing Stripe payment webhooks.
"""

from typing import Any, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse

from loguru import logger

from fhi_users import emails as fhi_emails
from fhi_users.audit import bound_client_ip
from fhi_users.models import ProfessionalUser, UserDomain
from fighthealthinsurance.models import (
    FaxesToSend,
    InterestedProfessional,
    LostStripeSession,
    StripeWebhookEvents,
)


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
    def _build_recovery_link(
        payment_type: Optional[str], metadata: Any, lost_session: LostStripeSession
    ) -> Optional[str]:
        """Build the abandoned-cart recovery link for an expired checkout.

        The link has to point at whichever app actually serves the resume
        route, otherwise the customer lands on a 404 / SPA catch-all:

        * Professional domain subscriptions are completed in the Fight
          Paperwork SPA at ``/stripe/finish-checkout``.
        * Everything else (patient appeals, fax, pay-what-you-want, ...) is
          completed by this Django backend's ``complete_payment`` view, which
          is served on the Fight Health Insurance domain. Pointing these at
          the Fight Paperwork SPA is what produced the "Invalid Demo Type"
          error: that SPA has no ``/stripe/finish`` route, so its router
          treated ``stripe`` as a demo-type path segment.

        Returns ``None`` when we can't build a link the target app could
        actually act on, so the caller skips emailing a dead resume link.

        Hosts are read from settings so non-prod environments don't email
        users production links.
        """
        if payment_type == "professional_domain_subscription":
            # Completed in the Fight Paperwork SPA. Without both ids the SPA
            # can't resume the subscription, so skip rather than email a dead
            # link.
            domain_id = metadata.get("domain_id")
            professional_id = metadata.get("professional_id")
            if not domain_id or not professional_id:
                logger.warning(
                    "Skipping professional recovery link: missing domain_id/professional_id"
                )
                return None
            params = urlencode(
                {
                    "domain_id": domain_id,
                    "professional_id": professional_id,
                }
            )
            return (
                f"https://{settings.FIGHT_PAPERWORK_DOMAIN}"
                f"/stripe/finish-checkout?{params}"
            )
        # Completed by the Django `complete_payment` view on the Fight Health
        # Insurance domain. That view rebuilds the checkout from either a
        # `recovery_info_id` or serialized `line_items` in the metadata; if
        # neither is present (e.g. pay-what-you-want donations) it can't
        # recreate the session, so skip emailing a link that would only error.
        if not metadata.get("recovery_info_id") and not metadata.get("line_items"):
            logger.warning(
                f"Skipping recovery link for payment_type={payment_type}: "
                "no recovery_info_id or line_items in metadata"
            )
            return None
        # Use the unguessable secure_token rather than the row id so the
        # recovery URL can't be brute-forced by enumerating ids.
        params = urlencode({"token": lost_session.secure_token})
        return (
            f"https://{settings.FIGHT_HEALTH_INSURANCE_DOMAIN}"
            f"{reverse('complete_payment')}?{params}"
        )

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

            # Client IP/ASN was stamped into the session metadata at checkout
            # creation (tracking_metadata_for_request); surface it onto its own
            # columns so abandoned-checkout abuse can be queried by IP/ASN.
            lost_session = LostStripeSession.objects.create(
                payment_type=payment_type,
                email=email,
                session_id=session_id,
                metadata=metadata,
                # ip_address comes verbatim from the client-controlled
                # X-Forwarded-For header (never validated), so it may be
                # malformed/spoofed. Store it bounded (LostStripeSession.ip_address
                # is a CharField) so a bad value can't raise and make Stripe retry
                # this webhook.
                ip_address=bound_client_ip(metadata.get("ip_address")),
                asn=metadata.get("asn") or "",
                asn_name=metadata.get("asn_name") or "",
            )
            finish_link = StripeWebhookHelper._build_recovery_link(
                payment_type, metadata, lost_session
            )
            if finish_link:
                fhi_emails.send_checkout_session_expired(
                    request,
                    email=email,
                    item=item,
                    link=finish_link,
                )
            else:
                logger.debug(f"Could not create finish link for {payment_type}")
        except Exception:
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
