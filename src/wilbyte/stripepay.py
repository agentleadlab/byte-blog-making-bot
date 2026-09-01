"""Payment links, as Stripe makes them.

Somebody asks Nicole for a link, Nicole makes one by hand, and the wording on
it is whatever she typed that afternoon. This does the same job from one
catalogue, so the client reads the same sentence every time and the dashboard
adds a lead type up across every sale rather than across forty one-off
charges.

Three calls: find or make the product, find or make the price, find or make
the link. Each one looks before it writes, so asking twice for the same thing
gives back the same link instead of a second one.

The key is a restricted one - payment links, products and prices, nothing
else. If it ever leaks, what it can do is make a payment link.
"""

from __future__ import annotations

import os

import httpx

API_ROOT = "https://api.stripe.com/v1"

# Money is in cents everywhere in Stripe. Dollars belong in the message
# somebody typed and nowhere else.
CENTS = 100


class StripeError(RuntimeError):
    """Raised when Stripe refuses, or when the key isn't set."""


def api_key() -> str:
    key = (os.getenv("STRIPE_API_KEY") or "").strip()
    if not key:
        raise StripeError(
            "Stripe isn't set up — STRIPE_API_KEY is blank in .env. It wants a "
            "restricted key with payment links, products and prices."
        )
    return key


def configured() -> bool:
    return bool((os.getenv("STRIPE_API_KEY") or "").strip())


def live() -> bool:
    """Whether the key makes links that take real money."""
    return (os.getenv("STRIPE_API_KEY") or "").strip().startswith(
        ("sk_live", "rk_live")
    )


def _call(method: str, path: str, **kwargs) -> dict:
    try:
        response = httpx.request(
            method,
            f"{API_ROOT}/{path}",
            auth=(api_key(), ""),
            timeout=30,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise StripeError(f"Couldn't reach Stripe: {exc}") from exc
    if response.status_code >= 400:
        raise StripeError(explain(response))
    return response.json()


def explain(response: httpx.Response) -> str:
    """Stripe's refusal in terms of what to do about it."""
    try:
        said = (response.json().get("error") or {}).get("message") or ""
    except ValueError:
        said = response.text[:200]

    if response.status_code == 401:
        return (
            "Stripe wouldn't take the key. Check STRIPE_API_KEY in .env is the "
            "restricted key you made, pasted whole."
        )
    if response.status_code == 403 or "permission" in said.lower():
        return (
            f"That key isn't allowed to do this — {said} Give it Write on "
            "payment links, products and prices."
        )
    return f"Stripe said: {said or response.text[:200]}"


# ------------------------------------------------------------ the product


def find_product(name: str) -> dict | None:
    """The product with this exact name, or None.

    By name because the catalogue is by name: RYTE holds no Stripe ids, so a
    product made by hand in the dashboard is found the same as one it made.
    """
    wanted = " ".join((name or "").split()).casefold()
    if not wanted:
        return None

    starting_after = ""
    for _page in range(10):  # A thousand products is far more than they sell.
        params = {"limit": 100, "active": "true"}
        if starting_after:
            params["starting_after"] = starting_after
        payload = _call("GET", "products", params=params)
        found = payload.get("data") or []
        for product in found:
            if " ".join(str(product.get("name") or "").split()).casefold() == wanted:
                return product
        if not payload.get("has_more") or not found:
            return None
        starting_after = str(found[-1].get("id") or "")
    return None


def ensure_product(name: str, description: str) -> dict:
    """The product, made if it wasn't there. Its description is the catalogue's.

    An existing product's description is left as it is: somebody may have
    reworded it in the dashboard, and that is a decision, not a drift.
    """
    found = find_product(name)
    if found:
        return found
    return _call(
        "POST", "products", data={"name": name, "description": description}
    )


# -------------------------------------------------------------- the price


def find_price(product_id: str, cents: int, currency: str = "usd") -> dict | None:
    """An active price on this product for exactly this much, or None."""
    payload = _call(
        "GET",
        "prices",
        params={"product": product_id, "active": "true", "limit": 100},
    )
    for price in payload.get("data") or []:
        if (
            int(price.get("unit_amount") or 0) == cents
            and str(price.get("currency") or "").lower() == currency
            and not price.get("recurring")
        ):
            return price
    return None


def ensure_price(product_id: str, cents: int, currency: str = "usd") -> dict:
    found = find_price(product_id, cents, currency)
    if found:
        return found
    return _call(
        "POST",
        "prices",
        data={"product": product_id, "unit_amount": cents, "currency": currency},
    )


# --------------------------------------------------------------- the link


def find_link(price_id: str) -> dict | None:
    """An active payment link already selling this price, or None.

    This is the whole point of looking first: the same package at the same
    price asked for twice should hand back one link, not two that report
    separately.
    """
    starting_after = ""
    for _page in range(10):
        params = {"limit": 100, "active": "true", "expand[]": "data.line_items"}
        if starting_after:
            params["starting_after"] = starting_after
        payload = _call("GET", "payment_links", params=params)
        found = payload.get("data") or []
        for link in found:
            for item in ((link.get("line_items") or {}).get("data")) or []:
                if str((item.get("price") or {}).get("id") or "") == price_id:
                    return link
        if not payload.get("has_more") or not found:
            return None
        starting_after = str(found[-1].get("id") or "")
    return None


def make_link(price_id: str, *, quantity: int = 1, note: str = "") -> dict:
    """A payment link for one price. `note` rides along as metadata.

    The note is who asked and what for - it turns a row in the dashboard from
    "$621.00" into "$621.00, Santiago Villegas, 40 Spanish Instant IUL Leads".
    """
    data = {
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": max(int(quantity or 1), 1),
    }
    if note:
        data["metadata[asked_for]"] = note[:499]
    return _call("POST", "payment_links", data=data)


def dollars(cents: int) -> str:
    """4000 -> "$40.00". What a client is being asked to pay, written out."""
    return f"${cents / CENTS:,.2f}"


def as_cents(amount: float) -> int:
    """Dollars to cents, rounded the way money rounds."""
    return int(round(float(amount) * CENTS))
