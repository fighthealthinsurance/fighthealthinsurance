"""
Serializers package for Fight Health Insurance REST API.

This package provides a modular structure for DRF serializers,
organized by domain (denial, appeal, chat, etc.).

For backwards compatibility, all serializers are re-exported here.
New code should import from specific submodules.
"""

from fighthealthinsurance.serializers.common import (
    AbsoluteStatisticsSerializer,
    ActorHealthStatusSerializer,
    ErrorSerializer,
    LiveModelsStatusSerializer,
    NextStepInfoSerializableSerializer,
    NotPaidErrorSerializer,
    SearchResultSerializer,
    StatisticsSerializer,
    StatusResponseSerializer,
    SuccessSerializer,
)
from fighthealthinsurance.serializers.fields import (
    DenialTypesListField,
    DictionaryListField,
    DictionaryStringField,
    StringListField,
)

# Legacy alias for backwards compatibility (typo in original name)
NextStepInfoSerizableSerializer = NextStepInfoSerializableSerializer

__all__ = [
    # Fields
    "StringListField",
    "DictionaryListField",
    "DictionaryStringField",
    "DenialTypesListField",
    # Common
    "NextStepInfoSerializableSerializer",
    "NextStepInfoSerizableSerializer",  # Legacy alias
    "StatusResponseSerializer",
    "ErrorSerializer",
    "NotPaidErrorSerializer",
    "SuccessSerializer",
    "StatisticsSerializer",
    "AbsoluteStatisticsSerializer",
    "SearchResultSerializer",
    "LiveModelsStatusSerializer",
    "ActorHealthStatusSerializer",
]
