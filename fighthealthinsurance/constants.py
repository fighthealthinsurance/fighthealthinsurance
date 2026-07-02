"""
Constants used throughout the Fight Health Insurance application.
"""

# Company information for Fight Health Insurance
FHI_COMPANY_NAME = "Fight Health Insurance -- a service of Totally Legit Co"
FHI_PHONE_NUMBER = "202-938-3266"
FHI_FAX_NUMBER = "415-840-7591"
FHI_SUPPORT_EMAIL = "support42@fighthealthinsurance.com"

# Pre-canned messages for the admin-controlled site header banner (see
# ``fighthealthinsurance.models.SiteBanner``). Each entry is a
# ``(short admin label, visitor-facing message)`` pair; the labels populate the
# picker in the SiteBanner admin so staff can post a notice quickly during an
# incident without writing copy from scratch.
SITE_BANNER_CANNED_MESSAGES = [
    (
        "AI models struggling -- try tomorrow",
        (
            "Our AI models are currently having difficulty. If you're able to "
            "wait, we kindly ask that you try again tomorrow. We're sorry for "
            "the inconvenience and appreciate your patience!"
        ),
    ),
    (
        "Investigating -- email support if appeal fails",
        (
            "We're investigating an issue with appeal generation. If your "
            f"appeal fails to generate, please email {FHI_SUPPORT_EMAIL} or "
            "try again tomorrow. Thank you for your patience!"
        ),
    ),
    (
        "Degraded performance / slowness",
        (
            "We're experiencing higher than usual demand, so appeal generation "
            "may be slower than normal. Thanks for bearing with us!"
        ),
    ),
    (
        "Scheduled maintenance",
        (
            "We're performing scheduled maintenance and some features may be "
            "temporarily unavailable. Please check back again shortly."
        ),
    ),
]
