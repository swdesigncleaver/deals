#!/usr/bin/env python3
"""cheapfood_watch.py

cheapfood.co.uk (a BigCommerce "Stencil" storefront) doesn't publish an RSS
feed — Stencil themes never expose ``?rss=1`` the way older BigCommerce
"Blueprint" themes did, and there's no ``/feed`` or ``/sitemap.xml``-style
product feed either. This tool builds the equivalent yourself:

  1. Fetch one or more listing pages (a category, a brand page, or a search
     results page all share the same product-grid markup) — ideally sorted
     newest-first, e.g. ``?sort=newest``.
  2. Parse out each product card: name, URL, price, image.
  3. Diff against a small JSON "seen" store on disk to find products never
     observed before.
  4. (Re)write a standard RSS 2.0 file so any feed reader can subscribe to
     it like any other site.

Run it on a schedule (cron / systemd timer / Scheduled Task) — see README.md
for how to wire it up and where to host the resulting feed.xml so a reader
can fetch it.

Design notes
------------
* No native "date added" exists on the site, so an item's ``pubDate`` in the
  feed is the first time *this tool* observed it — accurate enough for a
  personal alert feed, not a claim about when the store actually listed it.
* Selectors default to the standard BigCommerce Stencil/Cornerstone product
  grid markup, but are fully overridable from the CLI (``--selector-*``)
  without touching code, and ``--dump-html`` lets you inspect the live page
  to correct them if this store's theme has been customised.
* robots.txt is honoured by default (``--ignore-robots`` to override).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import re
import sys
import time
import urllib.robotparser as robotparser
from email.utils import format_datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This script needs 'requests'. Install with: pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("This script needs 'beautifulsoup4'. Install with: pip install beautifulsoup4 lxml")


LOG = logging.getLogger("cheapfood_watch")

USER_AGENT = (
    "cheapfood-watch/1.0 (personal RSS watcher; "
    "https://github.com/ - replace with your contact info)"
)

# Standard BigCommerce Stencil/Cornerstone product-grid markup. Each is a
# comma-separated CSS selector list tried in order against BeautifulSoup;
# override any of these from the CLI if this store's theme differs.
DEFAULT_SELECTORS = {
    "card": "li.product, article.product, div.product, .productGrid > li",
    "title_link": "h4.card-title a, h3.card-title a, a.card-title, .card-title a",
    "image": "img",
}

# Matches "£0.55", "£ 1.99", "£12" — deliberately loose since price markup
# varies by theme customisation far more than title/link markup does, and
# scanning for the currency symbol in the card's plain text is more robust
# than guessing CSS class names for a "sale price" span vs an "RRP" span.
PRICE_PATTERN = re.compile(r"£\s?\d{1,4}(?:\.\d{2})?")

# Common ways storefronts flag an unavailable product directly in the card
# text, rather than removing it from the listing page entirely. This is a
# best-effort text match, not a confirmed selector for this specific store
# (same caveat as PRICE_PATTERN) — if a real out-of-stock card slips
# through, the exact wording likely needs adding here.
OUT_OF_STOCK_PATTERN = re.compile(
    r"out of stock|sold out|currently unavailable|notify me when available",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class Product:
    """A single scraped product listing."""

    url: str
    title: str
    price: Optional[str] = None       # the current/display price, e.g. "£0.55"
    was_price: Optional[str] = None   # the original price, if this is a discount
    image: Optional[str] = None
    in_stock: bool = True


def extract_prices(card_text: str) -> tuple[Optional[str], Optional[str]]:
    """Find (current_price, was_price) from a product card's rendered text.

    Rather than depend on a specific "sale price" vs. "RRP price" CSS class
    (which varies a lot between store theme customisations and is easy to
    guess wrong), this just finds every £ amount mentioned anywhere in the
    card and assumes: one amount means no discount is shown; two or more
    means the lowest is what you'd actually pay and the highest is the
    original/RRP price being struck through.

    Returns (None, None) if no £ amount is found at all.
    """
    amounts = sorted(
        float(match.replace("£", "").replace(" ", ""))
        for match in PRICE_PATTERN.findall(card_text)
    )
    if not amounts:
        return None, None
    current = amounts[0]
    was = amounts[-1] if len(amounts) > 1 and amounts[-1] > current else None
    return f"£{current:.2f}", (f"£{was:.2f}" if was is not None else None)


class WatchError(RuntimeError):
    """Raised for recoverable scan-time failures (network, parsing)."""


def matches_keywords(title: str, keywords: Optional[list[str]]) -> bool:
    """Return True if ``title`` should be kept given ``keywords``.

    No keywords means no filtering (everything matches). Otherwise it's a
    case-insensitive substring match, OR'd across all keywords — a title
    needs to contain at least one of them.
    """
    if not keywords:
        return True
    title_lower = title.lower()
    return any(keyword.lower() in title_lower for keyword in keywords)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def robots_allow(url: str, user_agent: str) -> bool:
    """Return whether robots.txt permits fetching ``url``.

    Fails open (returns True) if robots.txt can't be read at all, since an
    unreachable robots.txt is not the same as an explicit disallow.
    """
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
    except Exception as exc:  # noqa: BLE001 - robots.txt fetch is best-effort
        LOG.warning("Couldn't read %s (%s); proceeding", robots_url, exc)
        return True
    return rp.can_fetch(user_agent, url)


def fetch(url: str, user_agent: str, timeout: float, retries: int = 3) -> str:
    """GET ``url`` with retries and exponential backoff. Raises WatchError."""
    headers = {"User-Agent": user_agent}
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = 2 ** attempt
                LOG.warning(
                    "Fetch failed (%d/%d) for %s: %s — retrying in %ds",
                    attempt, retries, url, exc, wait,
                )
                time.sleep(wait)
    raise WatchError(f"Giving up fetching {url}: {last_exc}")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_products(html: str, base_url: str, selectors: dict) -> list[Product]:
    """Extract product cards from a listing page's HTML.

    ``selectors`` must have the same keys as DEFAULT_SELECTORS. Malformed or
    incomplete cards (e.g. a card with no link) are skipped rather than
    raising, since a handful of odd cards shouldn't sink the whole scan.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(selectors["card"])
    products: list[Product] = []
    seen_urls: set[str] = set()

    for card in cards:
        link = card.select_one(selectors["title_link"])
        href = link.get("href") if link else None
        if not href:
            continue

        url = urljoin(base_url, href.split("?")[0])
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = link.get_text(strip=True)
        if not title:
            continue

        card_text = card.get_text(" ", strip=True)
        price, was_price = extract_prices(card_text)
        in_stock = not OUT_OF_STOCK_PATTERN.search(card_text)

        image = None
        img_el = card.select_one(selectors["image"])
        if img_el is not None:
            # Stencil lazy-loads images: `src` is often a "loading.svg"
            # placeholder and the real URL sits in `data-src`, so prefer that.
            src = img_el.get("data-src") or img_el.get("src")
            if src and "loading.svg" not in src:
                image = urljoin(base_url, src)

        products.append(Product(
            url=url, title=title, price=price, was_price=was_price,
            image=image, in_stock=in_stock,
        ))

    return products


# --------------------------------------------------------------------------- #
# State (what have we already seen?)
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WatchError(f"Couldn't read state file {path}: {exc}") from exc


def save_state(path: Path, state: dict) -> None:
    """Write atomically so a crash mid-write can't corrupt the store."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Feed generation
# --------------------------------------------------------------------------- #


# Bumped only when we need to force every subscriber's reader to treat all
# existing items as brand-new (e.g. a reader that cached pre-fix titles and
# offers no manual "clear cache" option). Appending this to the <guid> — but
# NOT to <link>, which still points at the real product page — changes the
# identifier readers dedupe on without touching anything the person clicks.
# Bump it again in future only if the same situation recurs.
GUID_CACHE_BUST = "v4"


def build_feed(
    title: str,
    link: str,
    description: str,
    items: Iterable[tuple[Product, dt.datetime]],
    max_items: int,
) -> bytes:
    """Build an RSS 2.0 document from (product, first_seen) pairs."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        dt.datetime.now(dt.timezone.utc)
    )

    # ElementTree escapes '<' and '>' in .text, which would turn a literal
    # <br/> into "&lt;br/&gt;" instead of an actual line break once a reader
    # renders the description as HTML — the conventional way RSS readers
    # treat <description>. To get a real CDATA section (verbatim markup,
    # not escaped), each description is written as a unique placeholder
    # token and swapped for real CDATA in the serialized bytes afterwards.
    cdata_fills: list[tuple[str, str]] = []

    for idx, (product, first_seen) in enumerate(list(items)[:max_items]):
        item = ET.SubElement(channel, "item")

        # Price is deliberately duplicated in both title and description:
        # some reader views (compact popups/pulldowns) only ever render the
        # title, while others open a fuller detail pane that renders the
        # description as HTML — putting it in only one leaves it invisible
        # in the other. Price leads the title so it's visible even where a
        # reader truncates a long product name.
        if product.price and product.was_price:
            price_prefix = f"{product.price} (was {product.was_price}) "
        elif product.price:
            price_prefix = f"{product.price} "
        else:
            price_prefix = ""
        ET.SubElement(item, "title").text = f"{price_prefix}{product.title}"
        ET.SubElement(item, "link").text = product.url

        guid = ET.SubElement(item, "guid")
        guid.text = f"{product.url}#{GUID_CACHE_BUST}"
        guid.set("isPermaLink", "false")

        if product.price and product.was_price:
            description_html = f"Price: {product.price}<br/>Was: {product.was_price}"
        elif product.price:
            description_html = f"Price: {product.price}"
        else:
            description_html = product.title

        placeholder = f"@@CDATA_{idx}@@"
        ET.SubElement(item, "description").text = placeholder
        cdata_fills.append((placeholder, description_html))

        if product.image:
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", product.image)
            enclosure.set("type", "image/jpeg")

        ET.SubElement(item, "pubDate").text = format_datetime(first_seen)

    body = ET.tostring(rss, encoding="utf-8")
    for placeholder, html in cdata_fills:
        # Guard against the (extremely unlikely, here) case of a price
        # string containing the literal CDATA terminator sequence.
        safe_html = html.replace("]]>", "]]]]><![CDATA[>")
        body = body.replace(placeholder.encode("utf-8"), f"<![CDATA[{safe_html}]]>".encode("utf-8"))

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scan(
    urls: list[str],
    state_path: Path,
    feed_path: Path,
    feed_title: str,
    feed_link: str,
    feed_description: str,
    user_agent: str,
    timeout: float,
    max_items: int,
    respect_robots: bool,
    selectors: dict,
    keywords: Optional[list[str]] = None,
    dump_html_path: Optional[Path] = None,
) -> list[Product]:
    """Run one scan across ``urls``, update the feed, return newly-seen products."""
    state = load_state(state_path)
    now = dt.datetime.now(dt.timezone.utc)
    newly_seen: list[Product] = []

    # Ground truth for pruning: a tracked item only gets dropped if its
    # source page was successfully re-fetched this run AND it's no longer
    # listed there — never because a page failed to load, which would
    # otherwise wrongly wipe out perfectly in-stock items.
    successfully_fetched_urls: set[str] = set()
    currently_listed_urls: set[str] = set()

    for i, url in enumerate(urls):
        if respect_robots and not robots_allow(url, user_agent):
            LOG.error("robots.txt disallows fetching %s — skipping", url)
            continue

        try:
            html = fetch(url, user_agent, timeout)
        except WatchError as exc:
            LOG.error(str(exc))
            continue

        if dump_html_path and i == 0:
            dump_html_path.write_text(html, encoding="utf-8")
            LOG.info("Dumped raw HTML for %s to %s", url, dump_html_path)

        successfully_fetched_urls.add(url)

        products = parse_products(html, url, selectors)
        LOG.info("Parsed %d product card(s) from %s", len(products), url)
        if not products:
            LOG.warning(
                "No products found on %s — the selectors likely don't match "
                "this page. Re-run with --dump-html to inspect the markup.",
                url,
            )

        matching = [p for p in products if matches_keywords(p.title, keywords)]
        if keywords:
            LOG.info(
                "%d of %d card(s) on %s matched %s",
                len(matching), len(products), url, keywords,
            )

        out_of_stock = [p for p in matching if not p.in_stock]
        if out_of_stock:
            LOG.info(
                "%d card(s) on %s are marked out of stock — excluded: %s",
                len(out_of_stock), url, ", ".join(p.title for p in out_of_stock),
            )
        matching = [p for p in matching if p.in_stock]

        for product in matching:
            currently_listed_urls.add(product.url)
            if product.url not in state:
                state[product.url] = {
                    "title": product.title,
                    "price": product.price,
                    "was_price": product.was_price,
                    "image": product.image,
                    "first_seen": now.isoformat(),
                    "source": url,
                }
                newly_seen.append(product)
            else:
                # Already tracked — refresh price/title/image in case they've
                # changed (e.g. a deeper discount) without resetting when it
                # was first spotted, so the feed reflects current pricing.
                state[product.url].update(
                    title=product.title,
                    price=product.price,
                    was_price=product.was_price,
                    image=product.image,
                )

    # Prune anything that's no longer listed on a page we successfully
    # re-fetched — sold out, delisted, or simply fallen off a "newest
    # first" page. This is what keeps the feed showing current deals only
    # instead of accumulating every product ever spotted.
    no_longer_listed = [
        url for url, info in state.items()
        if info.get("source") in successfully_fetched_urls and url not in currently_listed_urls
    ]
    for url in no_longer_listed:
        LOG.info("REMOVED (no longer listed): %s — %s", state[url]["title"], url)
        del state[url]

    save_state(state_path, state)

    all_items: list[tuple[Product, dt.datetime]] = []
    for url, info in state.items():
        product = Product(
            url=url,
            title=info["title"],
            price=info.get("price"),
            was_price=info.get("was_price"),
            image=info.get("image"),
        )
        all_items.append((product, dt.datetime.fromisoformat(info["first_seen"])))
    all_items.sort(key=lambda pair: pair[1], reverse=True)

    feed_bytes = build_feed(feed_title, feed_link, feed_description, all_items, max_items)
    feed_path.write_bytes(feed_bytes)
    LOG.info(
        "Wrote %d item(s) to %s (%d new this run)",
        min(len(all_items), max_items), feed_path, len(newly_seen),
    )
    return newly_seen


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch cheapfood.co.uk listing pages and maintain an RSS feed of new products.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "urls", nargs="+",
        help="One or more category/brand/search page URLs to watch (?sort=newest recommended)",
    )
    parser.add_argument("--state", type=Path, default=Path("cheapfood_state.json"), help="Path to the JSON 'seen products' store")
    parser.add_argument("--feed", type=Path, default=Path("feed.xml"), help="Path to write the RSS feed to")
    parser.add_argument("--title", default="cheapfood.co.uk — watched products", help="RSS channel title")
    parser.add_argument("--link", default="https://cheapfood.co.uk/", help="RSS channel link")
    parser.add_argument("--description", default="New products spotted by cheapfood_watch.py", help="RSS channel description")
    parser.add_argument("--user-agent", default=USER_AGENT)
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout in seconds")
    parser.add_argument("--max-items", type=int, default=100, help="Cap on items kept in the feed")
    parser.add_argument(
        "-k", "--keyword", dest="keywords", action="append", default=None,
        help="Only keep products whose title contains this word (case-insensitive). "
             "Repeatable — matches if ANY keyword is found, e.g. -k protein -k oreo",
    )
    parser.add_argument("--interval", type=float, default=0.0, help="If > 0, loop forever, sleeping this many seconds between scans, instead of exiting after one run")
    parser.add_argument("--ignore-robots", action="store_true", help="Skip the robots.txt check (not recommended)")
    parser.add_argument("--dump-html", type=Path, default=None, help="Write the raw HTML of the first URL here, for debugging selectors")
    parser.add_argument("--selector-card", default=DEFAULT_SELECTORS["card"])
    parser.add_argument("--selector-title", default=DEFAULT_SELECTORS["title_link"])
    parser.add_argument("--selector-image", default=DEFAULT_SELECTORS["image"])
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    selectors = {
        "card": args.selector_card,
        "title_link": args.selector_title,
        "image": args.selector_image,
    }

    def run_once() -> None:
        new_products = scan(
            urls=args.urls,
            state_path=args.state,
            feed_path=args.feed,
            feed_title=args.title,
            feed_link=args.link,
            feed_description=args.description,
            user_agent=args.user_agent,
            timeout=args.timeout,
            max_items=args.max_items,
            respect_robots=not args.ignore_robots,
            selectors=selectors,
            keywords=args.keywords,
            dump_html_path=args.dump_html,
        )
        for product in new_products:
            price_bit = product.price or "price n/a"
            if product.was_price:
                price_bit += f", was {product.was_price}"
            LOG.info("NEW: %s (%s) — %s", product.title, price_bit, product.url)

    if args.interval > 0:
        LOG.info("Looping every %.0fs — Ctrl+C to stop", args.interval)
        while True:
            try:
                run_once()
            except Exception:  # noqa: BLE001 - keep the loop alive across bad scans
                LOG.exception("Scan failed; will retry next interval")
            time.sleep(args.interval)
    else:
        run_once()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
