from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response

from fhi_users.models import UserDomain, ProfessionalUser, ProfessionalDomainRelation
from fhi_users.auth import rest_serializers as serializers
from fhi_users.auth.auth_utils import create_user
from fighthealthinsurance.rest_mixins import CreateMixin


class CreateProfessionalUser(viewsets.ViewSet, CreateMixin):
    """Create a professional user"""

    serializer_class = serializers.ProfessionalSignupSerializer

    def perform_create(
        self, request, serializer) -> Response | serializers.ProfessionalSignupResponseSerializer:
        data = serializer.validated_data
        # If we're not making a domain start by checking if the domain exists
        new_domain = data["make_new_domain"]
        if not new_domain:
            # If we can't find the domain return an error to the user
            try:
                UserDomain.objects.filter(name=data["domain_name"]).get()
            except UserDomain.DoesNotExist:
                return Response(
                    {"error": "Domain does not exist"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            # If we're making a new domain make sure it does not exist yet
            if UserDomain.objects.filter(name=data["domain_name"]).count() != 0:
                return Response(
                    {"error": "Domain already exists"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Create the domain
            UserDomain.objects.create(
                name=data["domain_name"],
                active=True,
                display_name=data["user_domain"]["display_name"],
                visible_phone_number=data["user_domain"]["visible_phone_number"],
                internal_phone_number=data["user_domain"]["internal_phone_number"],
                office_fax=data["user_domain"]["office_fax"],
                country=data["user_domain"]["country"],
                state=data["user_domain"]["state"],
                city=data["user_domain"]["city"],
                address1=data["user_domain"]["address1"],
                address2=data["user_domain"]["address2"],
                zipcode=data["user_domain"]["zipcode"],
            )
        user_domain: UserDomain = UserDomain.objects.get(name=data["domain_name"])
        # Create the user
        user = create_user(
            raw_username=data["username"],
            domain_name=data["domain_name"],
            email=data["email"],
            password=data["password"],
            first_name=data["first_name"],
            last_name=data["last_name"],
        )
        # Start by making the professional user -- active is false at the start because we want to hear back from stripe first
        professional_user = ProfessionalUser.objects.create(
            user=user,
            active=False,
        )
        ProfessionalDomainRelation.objects.create(
            professional=professional_user,
            domain=user_domain,
            active=False,
            admin=new_domain,  # New domain needs an admin.
            pending=not new_domain,  # Adding to exisitng domain make pending until admin approval.
        )
        # Send a welcome email to the user
        # send_welcome_email(user) # TODO: Implement this

        return serializers.ProfessionalSignupResponseSerializer({
            "next_url": "https;//farts.com"  # TODO: Point to stripe
        })
