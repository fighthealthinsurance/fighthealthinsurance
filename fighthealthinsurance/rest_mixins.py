import typing
from rest_framework import status
from rest_framework.serializers import Serializer
from rest_framework.response import Response
from rest_framework.decorators import action


class SerializerMixin:
    serializer_class: typing.Optional[typing.Type[Serializer]] = None

    def get_serializer_class(self):
        return self.serializer_class

    def deserialize(self, data=None):
        if self.get_serializer_class() is None:
            raise ValueError("serializer_class must be defined and not None")
        else:
            return self.get_serializer_class()(data=data)


class CreateMixin(SerializerMixin):
    def perform_create(self, request, serializer: Serializer) -> Response | Serializer:
        raise NotImplementedError("Subclasses must implement perform_create()")

    def create(self, request) -> Response:
        request_serializer = self.deserialize(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        response_serializer = self.perform_create(request, request_serializer)

        if response_serializer:
            result = response_serializer.data
        else:
            result = None
        if isinstance(result, Response):
            return result
        return Response(result, status=status.HTTP_201_CREATED)


class DeleteMixin(SerializerMixin):
    def perform_delete(self, request, serializer):
        pass

    def delete(self, request, *args, **kwargs):
        """For some reason"""
        serializer = self.deserialize(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_delete(request, serializer, *args, **kwargs)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteOnlyMixin:
    """Extra mixin that allows router display for delete-only resources"""

    def list(self, request, *args, **kwargs):
        # For some reason, delete resources don't show if there's not an
        # associated endpoint for working with their related data. So,
        # this adds one that 404s whenever its used until we can come up
        # with a better solution (or figure out what's wrong)

        return Response(status=status.HTTP_404_NOT_FOUND)
