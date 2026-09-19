"""Tests for cheapfood_watch.parse_products.

NOTE ON THE FIXTURE: I couldn't pull raw HTML for cheapfood.co.uk from my
sandbox (only a markdown-rendered view was available), so SAMPLE_HTML below
is a hand-built stand-in for the standard BigCommerce Stencil/Cornerstone
product-grid markup this store's platform uses — not a byte-for-byte capture
of the live page. Price extraction deliberately doesn't depend on exact CSS
class names (see extract_prices) precisely because a first attempt at
guessing those classes turned out wrong against the real site — it just
scans each card's text for £ amounts instead.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cheapfood_watch import (  # noqa: E402
    DEFAULT_SELECTORS,
    Product,
    extract_prices,
    matches_keywords,
    parse_products,
)

BASE_URL = "https://cheapfood.co.uk/food/sports-energy/"

SAMPLE_HTML = """
<html><body>
<ul class="productGrid">
  <li class="product">
    <figure class="card-figure">
      <a href="/nicks-peanut-butter-protein-bar-50g/">
        <img data-src="//cdn11.bigcommerce.com/images/peanut.png" src="/img/loading.svg">
      </a>
    </figure>
    <div class="card-body">
      <h4 class="card-title"><a href="/nicks-peanut-butter-protein-bar-50g/">N!ck's - Peanut Butter Protein Bar - 50g</a></h4>
      <div class="card-text--price"><span class="price price--withoutTax">&#163;1.25</span></div>
    </div>
  </li>
  <li class="product">
    <figure class="card-figure">
      <a href="/grenade-oreo-protein-bar-60g/?omnisendContactID=abc123">
        <img data-src="//cdn11.bigcommerce.com/images/oreo.png" src="/img/loading.svg">
      </a>
    </figure>
    <div class="card-body">
      <h4 class="card-title"><a href="/grenade-oreo-protein-bar-60g/">Grenade - Oreo - Protein Bar - 60g</a></h4>
      <div class="card-text--price">
        <span class="price price--rrp"><s>&#163;1.99</s></span>
        <span class="price price--withoutTax">&#163;0.55</span>
      </div>
    </div>
  </li>
  <!-- a malformed card with no link at all should just be skipped -->
  <li class="product">
    <div class="card-body"><h4 class="card-title">No link here</h4></div>
  </li>
</ul>
</body></html>
"""


def test_parses_expected_number_of_products():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    assert len(products) == 2


def test_extracts_title_url_price_was_price_and_image():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    grenade = next(p for p in products if "Grenade" in p.title)
    assert grenade == Product(
        url="https://cheapfood.co.uk/grenade-oreo-protein-bar-60g/",
        title="Grenade - Oreo - Protein Bar - 60g",
        price="£0.55",
        was_price="£1.99",
        image="https://cdn11.bigcommerce.com/images/oreo.png",
    )


def test_product_with_no_discount_has_no_was_price():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    nicks = next(p for p in products if "N!ck's" in p.title)
    assert nicks.price == "£1.25"
    assert nicks.was_price is None


def test_extract_prices_single_amount_has_no_was_price():
    assert extract_prices("Grenade Protein Bar £0.55") == ("£0.55", None)


def test_extract_prices_two_amounts_lowest_is_current():
    assert extract_prices("was £1.99 now £0.55") == ("£0.55", "£1.99")


def test_extract_prices_no_amount_returns_none_none():
    assert extract_prices("No price shown here") == (None, None)


def test_build_feed_wraps_description_in_real_cdata_with_line_break():
    import cheapfood_watch as cw
    from xml.etree import ElementTree as ET

    discounted = Product(
        url="https://cheapfood.co.uk/grenade-oreo-protein-bar-60g/",
        title="Grenade - Oreo - Protein Bar - 60g",
        price="£0.55",
        was_price="£1.99",
    )
    plain = Product(
        url="https://cheapfood.co.uk/nicks-peanut-butter-protein-bar-50g/",
        title="N!ck's - Peanut Butter Protein Bar - 50g",
        price="£1.25",
    )
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    feed_bytes = cw.build_feed("t", "https://cheapfood.co.uk/", "d", [(discounted, now), (plain, now)], 10)
    feed_text = feed_bytes.decode("utf-8")

    # The raw serialized bytes must contain a REAL, unescaped <br/> inside
    # CDATA — not the HTML-escaped "&lt;br/&gt;" ElementTree would produce
    # for plain .text — otherwise readers show the literal tag as text.
    assert "<![CDATA[Price: £0.55<br/>Was: £1.99]]>" in feed_text
    assert "&lt;br/&gt;" not in feed_text

    # Titles should be plain product names again, with no price appended.
    assert "<title>£0.55 (was £1.99) Grenade - Oreo - Protein Bar - 60g</title>" in feed_text

    # And the whole thing must still be well-formed XML.
    tree = ET.fromstring(feed_text)
    descriptions = [item.find("description").text for item in tree.findall(".//item")]
    assert descriptions == ["Price: £0.55<br/>Was: £1.99", "Price: £1.25"]


def test_guid_is_cache_busted_but_link_stays_clean():
    import cheapfood_watch as cw
    from xml.etree import ElementTree as ET

    product = Product(url="https://cheapfood.co.uk/some-bar/", title="Some Bar", price="£1.00")
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    feed_bytes = cw.build_feed("t", "https://cheapfood.co.uk/", "d", [(product, now)], 10)
    tree = ET.fromstring(feed_bytes)
    item = tree.find(".//item")

    # <link> is what a reader takes you to on click — must stay the real URL.
    assert item.find("link").text == "https://cheapfood.co.uk/some-bar/"
    # <guid> is what readers dedupe on — carries the cache-bust suffix and
    # is correctly marked as not a literal permalink once it does.
    assert item.find("guid").text == f"https://cheapfood.co.uk/some-bar/#{cw.GUID_CACHE_BUST}"
    assert item.find("guid").get("isPermaLink") == "false"


def test_strips_tracking_query_params_from_url():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    for product in products:
        assert "?" not in product.url


def test_skips_cards_without_a_link():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    assert all(p.title != "No link here" for p in products)


def test_deduplicates_same_url_appearing_twice_in_one_card_set():
    html = SAMPLE_HTML + SAMPLE_HTML  # simulate a page rendering some markup twice
    products = parse_products(html, BASE_URL, DEFAULT_SELECTORS)
    assert len(products) == 2


def test_no_keywords_matches_everything():
    assert matches_keywords("Grenade - Oreo - Protein Bar", None) is True
    assert matches_keywords("Grenade - Oreo - Protein Bar", []) is True


def test_keyword_matches_case_insensitively():
    assert matches_keywords("Grenade - Oreo - PROTEIN Bar", ["protein"]) is True
    assert matches_keywords("Grenade - Oreo - Protein Bar", ["PROTEIN"]) is True


def test_keyword_rejects_non_matching_title():
    assert matches_keywords("Walkers Ready Salted Crisps", ["protein"]) is False


def test_multiple_keywords_are_ored_together():
    keywords = ["protein", "collagen"]
    assert matches_keywords("No Sugar Company - Collagen Bar", keywords) is True
    assert matches_keywords("N!ck's - Peanut Butter Protein Bar", keywords) is True
    assert matches_keywords("Walkers Ready Salted Crisps", keywords) is False


def test_scan_only_stores_and_feeds_matching_products(tmp_path, monkeypatch):
    import cheapfood_watch as cw

    def fake_fetch(url, user_agent, timeout, retries=3):
        return SAMPLE_HTML

    monkeypatch.setattr(cw, "fetch", fake_fetch)
    monkeypatch.setattr(cw, "robots_allow", lambda url, ua: True)

    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"

    new_products = cw.scan(
        urls=[BASE_URL],
        state_path=state_path,
        feed_path=feed_path,
        feed_title="test",
        feed_link=BASE_URL,
        feed_description="test",
        user_agent="test-agent",
        timeout=5.0,
        max_items=50,
        respect_robots=True,
        selectors=DEFAULT_SELECTORS,
        keywords=["peanut butter"],
    )

    # Both SAMPLE_HTML products have "Protein" in the title, but only the
    # N!ck's one has "peanut butter" — confirms filtering happens before
    # anything is written to state/feed, not just cosmetically in the CLI.
    assert [p.title for p in new_products] == ["N!ck's - Peanut Butter Protein Bar - 50g"]

    feed_xml = feed_path.read_text(encoding="utf-8")
    assert "Peanut Butter" in feed_xml
    assert "Oreo" not in feed_xml


# HTML for a second scan where the Grenade bar has sold out / been delisted
# — only N!ck's remains on the page. Used to test pruning.
SAMPLE_HTML_GRENADE_GONE = """
<html><body>
<ul class="productGrid">
  <li class="product">
    <figure class="card-figure">
      <a href="/nicks-peanut-butter-protein-bar-50g/">
        <img data-src="//cdn11.bigcommerce.com/images/peanut.png" src="/img/loading.svg">
      </a>
    </figure>
    <div class="card-body">
      <h4 class="card-title"><a href="/nicks-peanut-butter-protein-bar-50g/">N!ck's - Peanut Butter Protein Bar - 50g</a></h4>
      <div class="card-text--price"><span class="price price--withoutTax">&#163;1.25</span></div>
    </div>
  </li>
</ul>
</body></html>
"""


def test_scan_prunes_items_no_longer_listed_on_a_successful_refetch(tmp_path, monkeypatch):
    import cheapfood_watch as cw

    monkeypatch.setattr(cw, "robots_allow", lambda url, ua: True)
    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"

    def scan_once(html):
        monkeypatch.setattr(cw, "fetch", lambda url, user_agent, timeout, retries=3: html)
        return cw.scan(
            urls=[BASE_URL], state_path=state_path, feed_path=feed_path,
            feed_title="test", feed_link=BASE_URL, feed_description="test",
            user_agent="test-agent", timeout=5.0, max_items=50,
            respect_robots=True, selectors=DEFAULT_SELECTORS, keywords=None,
        )

    scan_once(SAMPLE_HTML)  # both products tracked
    scan_once(SAMPLE_HTML_GRENADE_GONE)  # Grenade no longer on the page

    import json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "https://cheapfood.co.uk/grenade-oreo-protein-bar-60g/" not in state
    assert "https://cheapfood.co.uk/nicks-peanut-butter-protein-bar-50g/" in state

    feed_xml = feed_path.read_text(encoding="utf-8")
    assert "Oreo" not in feed_xml
    assert "Peanut Butter" in feed_xml


def test_scan_does_not_prune_on_a_failed_refetch(tmp_path, monkeypatch):
    """A page that fails to load must never be treated as 'nothing listed'."""
    import cheapfood_watch as cw

    monkeypatch.setattr(cw, "robots_allow", lambda url, ua: True)
    state_path = tmp_path / "state.json"
    feed_path = tmp_path / "feed.xml"

    monkeypatch.setattr(cw, "fetch", lambda url, user_agent, timeout, retries=3: SAMPLE_HTML)
    cw.scan(
        urls=[BASE_URL], state_path=state_path, feed_path=feed_path,
        feed_title="test", feed_link=BASE_URL, feed_description="test",
        user_agent="test-agent", timeout=5.0, max_items=50,
        respect_robots=True, selectors=DEFAULT_SELECTORS, keywords=None,
    )

    def failing_fetch(url, user_agent, timeout, retries=3):
        raise cw.WatchError("simulated network failure")

    monkeypatch.setattr(cw, "fetch", failing_fetch)
    cw.scan(
        urls=[BASE_URL], state_path=state_path, feed_path=feed_path,
        feed_title="test", feed_link=BASE_URL, feed_description="test",
        user_agent="test-agent", timeout=5.0, max_items=50,
        respect_robots=True, selectors=DEFAULT_SELECTORS, keywords=None,
    )

    import json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    # Both products must still be tracked — the failed fetch told us
    # nothing about whether they're still listed.
    assert "https://cheapfood.co.uk/grenade-oreo-protein-bar-60g/" in state
    assert "https://cheapfood.co.uk/nicks-peanut-butter-protein-bar-50g/" in state
