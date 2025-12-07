from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.decorators import action

from loguru import logger

from fighthealthinsurance.models import ChatLeads, MailingListSubscriber
from fighthealthinsurance.chat_lead_serializers import ChatLeadsSerializer


class ChatLeadsViewSet(viewsets.GenericViewSet):
    """ViewSet for submitting chat trial leads."""

    serializer_class = ChatLeadsSerializer

    def create(self, request: Request) -> Response:
        """
        Create a new chat lead from the trial form.
        Returns the session_id for use in the websocket connection.
        """
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Generate a unique session ID
            import uuid

            chat_lead = serializer.save(session_id=str(uuid.uuid4()))

            # Handle mailing list subscription if requested
            if serializer.validated_data.get("subscribe", False):
                email = serializer.validated_data.get("email")
                name = serializer.validated_data.get("name", "")
                phone = serializer.validated_data.get("phone", "")
                
                defaults = {"comments": "From chat leads form"}
                if name:
                    defaults["name"] = name
                if phone:
                    defaults["phone"] = phone
                
                # Use get_or_create to avoid duplicate subscriptions
                try:
                    MailingListSubscriber.objects.get_or_create(
                        email=email,
                        defaults=defaults,
                    )
                except Exception as e:
                    logger.debug(f"Error subscribing {email} to mailing list: {e}")
                    try:
                        MailingListSubscriber.objects.filter(email=email).update(
                            **defaults
                        )
                    except Exception as e2:
                        logger.warning(f"Error updating subscriber {email}: {e2}")

            # Return the session ID to be used for chat
            return Response(
                {
                    "status": "success",
                    "message": "Your trial chat session has been created.",
                    "session_id": str(chat_lead.session_id),
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(
            {"status": "error", "errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
