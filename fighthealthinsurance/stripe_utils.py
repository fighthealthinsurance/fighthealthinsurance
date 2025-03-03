import stripe
from django.conf import settings
from django.db import models
from typing import Optional, Tuple, Dict, Any
from django.apps import apps
from fighthealthinsurance.models import StripeProduct, StripePrice
from loguru import logger

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
                price_data["recurring"] = {"interval": "month"}

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
                    logger.error(f"Error cleaning up Stripe product: {str(cleanup_error)}")
            
            # Clean up product in DB if it was created
            if product:
                try:
                    product.delete()
                except Exception as db_cleanup_error:
                    logger.error(f"Error cleaning up product in database: {str(db_cleanup_error)}")
            
            raise
