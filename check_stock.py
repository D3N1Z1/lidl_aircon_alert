#!/usr/bin/env python3
"""Lidl stock monitor — checks a product page and pushes a phone
notification via ntfy.sh when the item becomes orderable.

Reusable for any Lidl product: override PRODUCT_URL env var.
State is kept in state.txt so you only get notified on the
transition to IN_STOCK, not every 30 minutes.
"""

import json
import os
import re
import sys
import urllib.request

URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.lidl.nl/p/tronic-lokale-airco-9000-btu/p100407256",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = "state.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "nl-NL,nl;q=0.9",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def check_availability(html: str) -> str:
    """Return IN_STOCK, COMING_SOON, OUT_OF_STOCK or UNKNOWN."""
    lowered = html.lower()

    # 1) Explicit lidl.nl page states. These MUST be checked before the
    #    JSON-LD data: Lidl marks announced products as "InStock" in its
    #    structured data even while the page still shows the notify-me
    #    bell and "Binnen 48 uur te bestellen".
    if "binnen 48 uur te bestellen" in lowered:
        return "COMING_SOON"
    if "waarschuw mij" in lowered or "bericht mij" in lowered:
        return "COMING_SOON"
    if any(s in lowered for s in ("uitverkocht", "niet meer beschikbaar", "niet leverbaar")):
        return "OUT_OF_STOCK"

    # 2) schema.org JSON-LD embedded in the page
    pattern = r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>'
    for match in re.finditer(pattern, html, re.S):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if not offers:
                continue
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                availability = str(offer.get("availability", "")).lower()
                if "instock" in availability or "limitedavailability" in availability:
                    return "IN_STOCK"
                if "outofstock" in availability or "soldout" in availability:
                    return "OUT_OF_STOCK"

    # 3) Fallback: the add-to-cart button label. Full phrase only —
    #    the bare word "winkelwagen" also appears in the site header.
    if "in winkelwagen" in lowered or "toevoegen aan winkelwagen" in lowered:
        return "IN_STOCK"
    return "UNKNOWN"


def notify(title: str, message: str, priority: str = "default") -> None:
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set; skipping notification")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Click": URL},
    )
    urllib.request.urlopen(req, timeout=30)


def main() -> None:
    prev = ""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            prev = f.read().strip()

    try:
        html = fetch(URL)
        status = check_availability(html)
    except Exception as exc:  # noqa: BLE001
        status = "ERROR"
        print(f"Fetch failed: {exc}", file=sys.stderr)

    print(f"Status: {status} (previous: {prev or 'none'})")

    if status == "IN_STOCK" and prev != "IN_STOCK":
        notify(
            "Lidl airco is bestelbaar!",
            "Tronic 9000 BTU is nu echt te bestellen — tik om de pagina te openen.",
            priority="high",
        )
    elif status in ("UNKNOWN", "ERROR") and prev not in ("UNKNOWN", "ERROR", ""):
        notify(
            "Airco-monitor: check faalt",
            f"Status: {status}. Lidl blokkeert het verzoek mogelijk — controleer handmatig.",
        )

    with open(STATE_FILE, "w") as f:
        f.write(status)


if __name__ == "__main__":
    main()
