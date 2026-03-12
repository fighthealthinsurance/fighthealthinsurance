from loguru import logger
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from fighthealthinsurance.chat_lead_serializers import ChatLeadsSerializer
from fighthealthinsurance.models import ChatLeads
from fighthealthinsurance.helpers.subscription_helpers import subscribe_to_mailing_list


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
            # Read subscribe from request data since it's not stored in the model
            if request.data.get("subscribe", False):
                email = serializer.validated_data.get("email")
                name = serializer.validated_data.get("name", "")
                phone = serializer.validated_data.get("phone", "")

                try:
                    subscribe_to_mailing_list(
                        email=email,
                        source="From chat leads form",
                        name=name,
                        phone=phone,
                    )
                except Exception:
                    logger.opt(exception=True).error(
                        f"Unexpected error subscribing {email} from chat leads form"
                    )

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
