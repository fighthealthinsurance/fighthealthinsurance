"""
Custom exception types for Fight Health Insurance application.
"""


class FaxDocumentError(Exception):
    """Base exception for fax document-related errors."""

    pass


class MissingDocumentError(FaxDocumentError):
    """Raised when a required document cannot be found."""

    pass


class DocumentRegenerationError(FaxDocumentError):
    """Raised when document regeneration fails."""

    pass


class MissingRequiredDataError(DocumentRegenerationError):
    """Raised when required data for regeneration is missing."""

    pass


class AppealAssemblyError(Exception):
    """Raised when a part of an appeal's document can't be turned into a PDF,
    so the document would go out without it."""

    pass
