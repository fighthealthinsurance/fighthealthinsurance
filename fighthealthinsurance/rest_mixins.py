import typing
from rest_framework import status
from rest_framework.serializers import Serializer
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from loguru import logger


class SerializerMixin:
    serializer_class: typing.Optional[typing.Type[Serializer]] = None

    def get_serializer_class(self) -> typing.Type[Serializer]:
        if self.serializer_class is None:
            raise ValidationError("serializer_class must not be None")
        return self.serializer_class

    def deserialize(self, data={}) -> Serializer:
        serializer_cls = self.get_serializer_class()
        return serializer_cls(data=data)


class CreateMixin(SerializerMixin):
    def perform_create(self, request, serializer: Serializer) -> Response:
        raise NotImplementedError("Subclasses must implement perform_create()")

    def create(self, request) -> Response:
        try:
            request_serializer = self.deserialize(data=request.data)
            request_serializer.is_valid(raise_exception=True)
            return self.perform_create(request, request_serializer)
        except Exception as e:
            logger.opt(exception=True).error(f"Error during request serialization: {e}")
            raise e


class DeleteMixin(SerializerMixin):
    def perform_delete(self, request, serializer):
        raise NotImplementedError("Subclasses must implement perform_delete()")

    def delete(self, request, *args, **kwargs) -> Response:
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
