#!/usr/bin/env python3
"""Lidl stock monitor — checks a product page and pushes a phone
notification via ntfy.sh when the item becomes orderable.

v4: explicitly negotiates gzip and decompresses the response.
(Without an Accept-Encoding header, Lidl's CDN may send compressed
bytes that decode as garbage.) Keeps v3 behavior: retries, last-
known-good state, warning only after FAIL_THRESHOLD consecutive
failed checks.
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib

URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.lidl.nl/p/tronic-lokale-airco-9000-btu/p100407256",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = "state.txt"
FAIL_THRESHOLD = 4
RETRIES = 3
RETRY_WAIT = 15  # seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

BLOCK_MARKERS = ("captcha", "access denied", "unusual traffic", "robot", "akamai")


def decode_body(raw: bytes, content_encoding: str) -> str:
    """Decompress (if needed) and decode a response body."""
    if raw[:2] == b"\x1f\x8b":  # gzip magic bytes
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            print(f"  gzip decompress failed: {exc}", file=sys.stderr)
    elif "deflate" in content_encoding:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error as exc:
                print(f"  deflate decompress failed: {exc}", file=sys.stderr)
    return raw.decode("utf-8", errors="replace")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    print(f"  content-encoding: {encoding or 'none'}, {len(raw)} raw bytes")
    return decode_body(raw, encoding)


def garble_ratio(text: str) -> float:
    """Fraction of unicode replacement chars — high means binary junk."""
    if not text:
        return 1.0
    return text.count("\ufffd") / len(text)


def check_availability(html: str) -> str:
    """Return IN_STOCK, COMING_SOON, OUT_OF_STOCK or UNKNOWN."""
    lowered = html.lower()

    # 1) Explicit lidl.nl page states — checked BEFORE JSON-LD, because
    #    Lidl marks announced products as "InStock" in structured data
    #    while the page still shows the notify-me bell.
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

    # 3) Fallback: add-to-cart button label (full phrase — the bare word
    #    "winkelwagen" also appears in the site header)
    if "in winkelwagen" in lowered or "toevoegen aan winkelwagen" in lowered:
        return "IN_STOCK"
    return "UNKNOWN"


def get_status() -> str:
    """Fetch + parse with retries. Returns a status string."""
    status = "ERROR"
    for attempt in range(1, RETRIES + 1):
        try:
            html = fetch(URL)
        except urllib.error.HTTPError as exc:
            status = "BLOCKED" if exc.code in (403, 429, 503) else "ERROR"
            print(f"attempt {attempt}: HTTP {exc.code} -> {status}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            status = "ERROR"
            print(f"attempt {attempt}: {exc}", file=sys.stderr)
        else:
            status = check_availability(html)
            ratio = garble_ratio(html)
            print(f"attempt {attempt}: parsed {status} "
                  f"(html {len(html)} chars, garble {ratio:.1%})")
            if status == "UNKNOWN":
                snippet = re.sub(r"\s+", " ", html[:300])
                blocked = any(m in html.lower() for m in BLOCK_MARKERS)
                print(f"  looks like a block page: {blocked}")
                print(f"  snippet: {snippet}")
        if status not in ("UNKNOWN", "ERROR", "BLOCKED"):
            return status
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT)
    return status


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


def read_state() -> tuple[str, int]:
    if not os.path.exists(STATE_FILE):
        return "", 0
    lines = open(STATE_FILE).read().splitlines()
    prev = lines[0].strip() if lines else ""
    fails = int(lines[1]) if len(lines) > 1 and lines[1].strip().isdigit() else 0
    return prev, fails


def write_state(status: str, fails: int) -> None:
    with open(STATE_FILE, "w") as f:
        f.write(f"{status}\n{fails}\n")


def main() -> None:
    prev, fails = read_state()
    status = get_status()
    print(f"Result: {status} (previous good: {prev or 'none'}, prior fails: {fails})")

    if status in ("UNKNOWN", "ERROR", "BLOCKED"):
        fails += 1
        print(f"Consecutive failures: {fails}")
        if fails == FAIL_THRESHOLD:
            notify(
                "Airco-monitor: checks falen",
                f"{fails} checks op rij mislukt (laatste: {status}). "
                "Lidl blokkeert GitHub mogelijk structureel — tijd voor plan B.",
            )
        write_state(prev, fails)  # keep last known good status
        return

    if status == "IN_STOCK" and prev != "IN_STOCK":
        notify(
            "Lidl airco is bestelbaar!",
            "Tronic 9000 BTU is nu echt te bestellen — tik om de pagina te openen.",
            priority="high",
        )
    write_state(status, 0)


if __name__ == "__main__":
    main()
