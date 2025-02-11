import stripe
from django.conf import settings
from django.db import models
from typing import Optional, Tuple, Dict, Any
from django.apps import apps
from fighthealthinsurance.models import StripeProduct, StripePrice


def get_or_create_price(
    product_name: str, amount: int, currency: str = "usd", recurring: bool = False
) -> Tuple[str, str]:
    """Get or create Stripe product and price, returns (product_id, price_id)"""
    stripe.api_key = settings.STRIPE_API_SECRET_KEY

    # Try to get from DB first
    try:
        product = StripeProduct.objects.get(name=product_name, active=True)
        price = StripePrice.objects.get(
            product=product, amount=amount, currency=currency, active=True
        )
        return product.stripe_id, price.stripe_id
    except (StripeProduct.DoesNotExist, StripePrice.DoesNotExist):
        # Create in Stripe and save to DB
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
            price_data["recurring"] = {"interval": "month"}

        try:
            stripe_price = stripe.Price.create(**price_data)  # type: ignore
            price = StripePrice.objects.create(
                product=product,
                stripe_id=stripe_price.id,
                amount=amount,
                currency=currency,
            )
            return product.stripe_id, price.stripe_id
        except Exception:
            # Clean up product if price creation fails
            stripe.Product.delete(stripe_product.id)
            product.delete()
            raise
