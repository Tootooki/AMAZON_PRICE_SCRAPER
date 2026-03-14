"""
Amazon SP-API Product Pricing Module
Fetches pricing data using Amazon's official Selling Partner API.
Returns: List Price, Buybox/Sale Price, Your Price, and seller details.
"""

import time
import requests


# --- Configuration ---
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_ID = "ATVPDKIKX0DER"  # US

# SP-API credentials (MUST be set as environment variables)
import os
CLIENT_ID = os.environ.get("AMAZON_SP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AMAZON_SP_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("AMAZON_SP_REFRESH_TOKEN", "")


def get_access_token():
    """Get a fresh access token from Amazon."""
    res = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=15)
    res.raise_for_status()
    return res.json()["access_token"]


def get_item_offers(access_token, asin):
    """
    Get full pricing data for a single ASIN via getItemOffers.
    Returns: list_price, buybox_price, landed_price, buybox_seller, num_offers, error
    """
    url = f"{SP_API_BASE}/products/pricing/v0/items/{asin}/offers"
    params = {
        "MarketplaceId": MARKETPLACE_ID,
        "ItemCondition": "New",
    }
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json",
    }

    try:
        res = requests.get(url, params=params, headers=headers, timeout=15)

        if res.status_code == 429:
            return None, None, None, None, None, "Rate limited (429) - too many requests"
        if res.status_code == 403:
            return None, None, None, None, None, "Access denied (403)"
        if res.status_code != 200:
            return None, None, None, None, None, f"API error ({res.status_code})"

        data = res.json()
        payload = data.get("payload", {})

        if payload.get("status") != "Success":
            return None, None, None, None, None, f"API status: {payload.get('status')}"

        summary = payload.get("Summary", {})

        # List Price (MSRP)
        list_price_data = summary.get("ListPrice")
        list_price = list_price_data.get("Amount") if list_price_data else None

        # Buybox Price
        buybox_prices = summary.get("BuyBoxPrices", [])
        buybox_price = None
        buybox_landed = None
        if buybox_prices:
            bb = buybox_prices[0]
            buybox_price = bb.get("ListingPrice", {}).get("Amount")
            buybox_landed = bb.get("LandedPrice", {}).get("Amount")

        # Total offers
        total_offers = summary.get("TotalOfferCount", 0)

        # Buybox seller info
        offers = payload.get("Offers", [])
        buybox_seller = None
        for offer in offers:
            if offer.get("IsBuyBoxWinner"):
                seller_id = offer.get("SellerId", "Unknown")
                is_fba = offer.get("IsFulfilledByAmazon", False)
                is_prime = offer.get("PrimeInformation", {}).get("IsPrime", False)
                buybox_seller = {
                    "seller_id": seller_id,
                    "is_fba": is_fba,
                    "is_prime": is_prime,
                }
                break

        return list_price, buybox_price, buybox_landed, buybox_seller, total_offers, None

    except requests.exceptions.Timeout:
        return None, None, None, None, None, "Request timed out"
    except Exception as e:
        return None, None, None, None, None, f"Error: {str(e)[:100]}"


def get_my_pricing_batch(access_token, asins):
    """
    Get your own pricing for a batch of ASINs (up to 20 at a time).
    Returns dict: {asin: your_price} or {asin: None} if not selling.
    """
    results = {}
    asin_params = "&".join([f"Asins={a}" for a in asins])
    url = f"{SP_API_BASE}/products/pricing/v0/price?MarketplaceId={MARKETPLACE_ID}&ItemType=Asin&{asin_params}"
    headers = {
        "x-amz-access-token": access_token,
        "Content-Type": "application/json",
    }

    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            for item in data.get("payload", []):
                asin = item.get("ASIN")
                product = item.get("Product", {})
                offers = product.get("Offers", [])
                if offers:
                    # Get the first offer's listing price
                    your_price = offers[0].get("BuyingPrice", {}).get("ListingPrice", {}).get("Amount")
                    results[asin] = your_price
                else:
                    results[asin] = None
    except Exception:
        pass

    return results


def format_price(amount):
    """Format a numeric price as $X.XX string."""
    if amount is None:
        return "N/A"
    try:
        return f"${float(amount):,.2f}"
    except (ValueError, TypeError):
        return "N/A"


def run_pricing_job(asins, job_state):
    """
    Background job: fetch pricing for all ASINs via SP-API.
    Updates job_state dict with progress.
    """
    job_state["status"] = "authenticating"
    job_state["total"] = len(asins)
    job_state["progress"] = 0
    job_state["results"] = []

    try:
        # Get access token
        access_token = get_access_token()
        job_state["status"] = "fetching_your_prices"

        # Get "Your Price" in batches of 20
        your_prices = {}
        for i in range(0, len(asins), 20):
            batch = asins[i:i+20]
            batch_prices = get_my_pricing_batch(access_token, batch)
            your_prices.update(batch_prices)
            time.sleep(0.2)  # Rate limit

        job_state["status"] = "fetching_prices"

        # Get item offers for each ASIN (1 call per ASIN, max ~10/sec)
        for i, asin in enumerate(asins):
            list_price, buybox_price, landed_price, buybox_seller, num_offers, error = \
                get_item_offers(access_token, asin)

            your_price = your_prices.get(asin)

            result = {
                "asin": asin,
                "list_price": format_price(list_price),
                "buybox_price": format_price(buybox_price),
                "landed_price": format_price(landed_price),
                "your_price": format_price(your_price),
                "num_offers": num_offers or 0,
                "buybox_seller": buybox_seller,
                "error": error,
                "status": "error" if error else "ok",
            }
            job_state["results"].append(result)
            job_state["progress"] = i + 1

            # Rate limiting: SP-API allows ~10 requests/sec for this endpoint
            # Be conservative to avoid 429s
            if i < len(asins) - 1:
                time.sleep(0.3)

            # Refresh token periodically (every 500 ASINs)
            if (i + 1) % 500 == 0:
                try:
                    access_token = get_access_token()
                except Exception:
                    pass

        job_state["status"] = "complete"

    except Exception as e:
        job_state["status"] = "error"
        job_state["error"] = str(e)
