"""Tests for cheapfood_watch.parse_products.

NOTE ON THE FIXTURE: I couldn't pull raw HTML for cheapfood.co.uk from my
sandbox (only a markdown-rendered view was available), so SAMPLE_HTML below
is a hand-built stand-in for the standard BigCommerce Stencil/Cornerstone
product-grid markup this store's platform uses — not a byte-for-byte capture
of the live page. Before relying on this in production, run once with
--dump-html against a real listing page and diff its structure against this
fixture; adjust DEFAULT_SELECTORS (or pass --selector-*) if the theme has
been customised.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cheapfood_watch import DEFAULT_SELECTORS, Product, matches_keywords, parse_products  # noqa: E402

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
      <div class="card-text--price"><span class="price price--withoutTax">&#163;0.55</span></div>
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


def test_extracts_title_url_price_and_image():
    products = parse_products(SAMPLE_HTML, BASE_URL, DEFAULT_SELECTORS)
    grenade = next(p for p in products if "Grenade" in p.title)
    assert grenade == Product(
        url="https://cheapfood.co.uk/grenade-oreo-protein-bar-60g/",
        title="Grenade - Oreo - Protein Bar - 60g",
        price="£0.55",
        image="https://cdn11.bigcommerce.com/images/oreo.png",
    )


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
