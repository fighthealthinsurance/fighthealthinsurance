import stripe
from django.conf import settings
from typing import Tuple, Dict, Any
from fighthealthinsurance.models import StripeProduct, StripePrice, StripeMeter
from loguru import logger


def get_or_create_price(
    product_name: str,
    amount: int,
    currency: str = "usd",
    recurring: bool = False,
    metered: bool = False,
) -> Tuple[str, str]:
    """Get or create Stripe product and price, returns (product_id, price_id)"""
    stripe.api_key = settings.STRIPE_API_SECRET_KEY

    # Try to get from DB first
    meter_id = None
    if metered:
        try:
            meter = StripeMeter.objects.filter(name=product_name, active=True).get()
            meter_id = meter.stripe_meter_id
        except StripeMeter.DoesNotExist:
            meter_request = stripe.billing.Meter.create(
                display_name=product_name,
                event_name=product_name,
                default_aggregation={"formula": "sum"},
            )
            meter_id = meter_request.id
            meter = StripeMeter.objects.create(
                name=product_name, stripe_meter_id=meter_id
            )
    try:
        product = StripeProduct.objects.get(name=product_name, active=True)
        price = StripePrice.objects.get(
            product=product, amount=amount, currency=currency, active=True
        )
        return product.stripe_id, price.stripe_id
    except (StripeProduct.DoesNotExist, StripePrice.DoesNotExist):
        # Create in Stripe and save to DB
        stripe_product = None
        product = None

        try:
            stripe_product = stripe.Product.create(name=product_name)
            product = StripeProduct.objects.create(
                name=product_name,
                stripe_id=stripe_product.id,
            )

            price_data: Dict[str, Any] = {
                "unit_amount": amount,
                "currency": currency,
                "product": stripe_product.id,
            }
            if recurring:
                if not metered:
                    price_data["recurring"] = {"interval": "month"}
                else:
                    price_data["recurring"] = {
                        "interval": "month",
                        "usage_type": "metered",
                        "metered": {
                            "usage_type": "metered",
                            "amount": 1,
                            "billing_scheme": "per_unit",
                            "meter": meter_id,
                        },
                    }

            stripe_price = stripe.Price.create(**price_data)  # type: ignore
            price = StripePrice.objects.create(
                product=product,
                stripe_id=stripe_price.id,
                amount=amount,
                currency=currency,
            )
            return product.stripe_id, price.stripe_id
        except Exception as e:
            logger.error(f"Error creating Stripe price: {str(e)}")

            # Clean up product in Stripe if it was created
            if stripe_product:
                try:
                    stripe.Product.delete(stripe_product.id)
                except Exception as cleanup_error:
                    logger.error(
                        f"Error cleaning up Stripe product: {str(cleanup_error)}"
                    )

            # Clean up product in DB if it was created
            if product:
                try:
                    product.delete()
                except Exception as db_cleanup_error:
                    logger.error(
                        f"Error cleaning up product in database: {str(db_cleanup_error)}"
                    )

            raise
