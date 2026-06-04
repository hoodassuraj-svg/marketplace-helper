"""
scraper.py
----------
Pulls listing details from a real-estate URL.

Strategy (most reliable first), which is why adding new sites is usually easy:
  1. JSON-LD structured data (schema.org) -- many sites embed this; very stable
  2. OpenGraph / <meta> tags  (og:title, og:description, og:image, price)
  3. Site-specific CSS selectors (realtor.ca built in; add your own below)
  4. Manual entry fallback (you type/paste the details -- see poster.py)

We render the page with Playwright (handles JavaScript and is less likely to be
blocked than a bare HTTP request), then parse the HTML with BeautifulSoup.

NOTE on realtor.ca: it actively fights scrapers and may show a "verify you are
human" page. When that happens, extraction returns partial/empty data and
poster.py drops you into the manual fallback -- you paste the details once and
the rest of the automation still saves you the time.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

try:
    # Imported lazily so you can unit-test the parsing without a browser.
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False


# A realistic desktop user-agent reduces trivial bot blocks.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------
@dataclass
class ListingData:
    """Everything we try to pull from a listing page."""
    source_url: str
    title: str = ""
    price: str = ""             # digits only, e.g. "750000" (Marketplace wants a number)
    raw_price: str = ""         # what we saw, e.g. "$750,000"
    street: str = ""            # best-guess street line, e.g. "123 Main St"
    city: str = ""              # best-guess city, used for the location picker
    description: str = ""
    listing_type: str = "sale"  # "sale" or "rent"
    bedrooms: str = ""          # e.g. "3"
    bathrooms: str = ""         # e.g. "4"
    property_type: str = ""     # Facebook label: "House", "Townhouse", "Condo", etc.
    square_feet: str = ""       # e.g. "1200"
    street_name: str = ""       # street without house number, e.g. "Lindenshire Ave"
    photos: list = field(default_factory=list)        # remote image URLs
    photo_paths: list = field(default_factory=list)   # local files after download

    def is_usable(self) -> bool:
        """True if we got enough to bother pre-filling the form.

        Accept a listing that has either a title OR a street address, plus at
        least one substantive field (price, description, or photos). This keeps
        partially-scraped Realm listings (street + price + photos but no title)
        from being thrown away.
        """
        has_identity = bool(self.title or self.street)
        has_content = bool(self.price or self.description or self.photos)
        return has_identity and has_content

    def summary(self) -> str:
        """Human-readable summary for the review step."""
        desc = self.description[:160] + ("..." if len(self.description) > 160 else "")
        return (
            f"  Title       : {self.title}\n"
            f"  Type        : {'For rent' if self.listing_type == 'rent' else 'For sale'}\n"
            f"  Price       : {self.raw_price or self.price}\n"
            f"  Street      : {self.street}\n"
            f"  City        : {self.city}\n"
            f"  Property    : {self.property_type or '(unknown)'}\n"
            f"  Beds/Baths  : {self.bedrooms or '?'} bed / {self.bathrooms or '?'} bath\n"
            f"  Sq ft       : {self.square_feet or '(unknown)'}\n"
            f"  Street name : {self.street_name}\n"
            f"  Photos found: {len(self.photos)}\n"
            f"  Description : {desc}"
        )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_html(url: str, wait_ms: int = 3500) -> str:
    """Load the page in a real browser and return its rendered HTML.

    Falls back to a plain HTTP GET if Playwright isn't installed.
    """
    if _HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-CA")
            page = context.new_page()
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms)  # let JS-rendered content populate
                return page.content()
            finally:
                context.close()
                browser.close()
    # Fallback: no browser available, just fetch the raw HTML.
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Property type mapping: Realm/TREB labels → Facebook Marketplace options
# ---------------------------------------------------------------------------
# Facebook's dropdown text for property type (add more as you encounter them).
REALM_TYPE_TO_FB = {
    "detached":           "House",
    "semi-detached":      "House",
    "link":               "House",
    "att/row/twnhouse":   "Townhouse",
    "condo townhouse":    "Townhouse",
    "condo apt":          "Condo",
    "apartment":          "Apartment",
    "duplex":             "House",
    "triplex":            "House",
    "multiplex":          "House",
    "vacant land":        "Land",
    "rural residential":  "House",
    "mobile/trailer":     "Other",
}

def _realm_type_to_fb(type_name: str) -> str:
    """Map a Realm typeName to the closest Facebook property-type label."""
    key = (type_name or "").lower().strip()
    for realm_key, fb_label in REALM_TYPE_TO_FB.items():
        if realm_key in key:
            return fb_label
    return "House"  # safest default


# ---------------------------------------------------------------------------
# Realm MLP portal -- intercept API responses to get structured listing data
# ---------------------------------------------------------------------------
def _fullres_photo_url(cdn_url: str) -> str:
    """Extract the full-resolution source image from a stratuscollab imgproxy URL.

    The CDN URL encodes the original image URL as a base64 segment at the end,
    e.g. .../rs:fill:600:400:0/.../aHR0cH...  → decode last segment → 1920px TREB URL.
    Falls back to the CDN URL with the resize param bumped up if decoding fails.
    """
    import base64
    try:
        # The last path segment is the base64url-encoded original URL + file ext.
        last = cdn_url.rstrip("/").split("/")[-1]
        last = re.sub(r"\.[a-z]{3,4}$", "", last)   # strip .jpg / .png etc.
        padded = last.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if decoded.startswith("http"):
            return decoded   # full-res original (1920×1920 from TRREB/TREB)
    except Exception:
        pass
    # Fallback: bump the resize param from 600×400 to 1920×1920 in-place.
    return re.sub(r"rs:fill:\d+:\d+:\d+", "rs:fit:1920:1920:0", cdn_url)


def _realm_description_from_html(html_str: str) -> str:
    """Strip HTML tags from the Realm 'html' field and extract the remarks.

    The html blob contains the full listing sheet. The actual agent remarks
    follow 'Client Remarks' in the text -- we grab everything after that marker
    up to the next section divider.
    """
    if not html_str:
        return ""
    # Strip all HTML tags.
    text = re.sub(r"<[^>]+>", " ", html_str)
    # Decode common HTML entities before stripping the rest.
    entities = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                "&#x27;": "'", "&#39;": "'", "&nbsp;": " ", "&darr;": "↓"}
    for ent, char in entities.items():
        text = text.replace(ent, char)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)  # strip any remaining entities
    text = re.sub(r"\s+", " ", text).strip()

    # Find the Client Remarks block -- it follows this label in the rendered text.
    for marker in ("Client Remarks", "Remarks", "PublicRemarks"):
        idx = text.find(marker)
        if idx != -1:
            snippet = text[idx + len(marker):].strip()
            # Trim at the next recognizable section heading if present.
            for stopper in ("Extras", "Inclusions", "Exclusions", "Room", "Legal Description"):
                stop_idx = snippet.find(stopper)
                if stop_idx > 50:   # keep at least a bit of text
                    snippet = snippet[:stop_idx].strip()
                    break
            return snippet[:3000]  # cap at a sensible length

    return ""


def fetch_realm(url: str) -> ListingData:
    """Fetch a Realm MLP portal listing by intercepting its API responses.

    The portal is a React SPA -- the page HTML is just a shell. The real data
    arrives in two JSON API calls after page load:
      1. /shared/...  -- portal view with title, meta (price/type), html (remarks)
      2. /search?listingID=... -- search results with full image URLs, street, city

    We capture both, extract from each, and merge the best fields.
    """
    from urllib.parse import urlparse, parse_qs

    # Extract the active listing ID from the URL — two formats Realm uses:
    #   Old: ?active=TREB-N13173246   (shared portal with multiple listings)
    #   New: /view/listing/TREB-N13171452  (direct listing view)
    parsed_url = urlparse(url)
    parsed_qs = parse_qs(parsed_url.query)
    active_param = parsed_qs.get("active", [""])[0]
    active_id = active_param.split("-", 1)[-1] if "-" in active_param else active_param
    if not active_id:
        # New URL format: extract from path e.g. /view/listing/TREB-N13171452
        path_match = re.search(r"/listing/([A-Z]+-\w+)", parsed_url.path)
        if path_match:
            active_id = path_match.group(1).split("-", 1)[-1]  # strip prefix → N13171452

    data = ListingData(source_url=url)
    portal_blob: dict = {}    # /shared/... response
    search_item: dict = {}    # the matched listing from /search response
    captured_html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-CA")
        page = context.new_page()

        def on_response(response):
            nonlocal portal_blob, search_item
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct or response.status != 200:
                    return
                body = response.body()
                if not body:
                    return
                blob = json.loads(body.decode("utf-8", errors="ignore"))
            except Exception:
                return

            # Portal view response: has 'title', 'meta', 'html', 'summary' keys.
            if isinstance(blob, dict) and "meta" in blob and "html" in blob:
                portal_blob = blob

            # Search results response: has 'searchResults.data' list.
            if isinstance(blob, dict) and "searchResults" in blob:
                items = blob["searchResults"].get("data") or []
                if items and isinstance(items[0], dict):
                    matched = None
                    for item in items:
                        item_id = str(item.get("listingID", "") or item.get("_id", ""))
                        if active_id and active_id in item_id:
                            matched = item
                            break
                    search_item = matched or items[0]

            # Single-listing response (new /view/listing/... route).
            # Shows up as a flat dict with listing fields OR nested under a key.
            for key in ("listing", "listingData", "data", "result"):
                if isinstance(blob, dict) and key in blob and isinstance(blob[key], dict):
                    candidate = blob[key]
                    if any(f in candidate for f in ("listingID", "streetAddress", "listPrice", "images")):
                        if not search_item:
                            search_item = candidate
                        break
            # Also accept a flat blob that looks like a listing dict directly.
            if not search_item and isinstance(blob, dict):
                if any(f in blob for f in ("streetAddress", "listPrice", "images", "bedrooms")):
                    search_item = blob

        page.on("response", on_response)
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_timeout(6000)
            captured_html = page.content()
        finally:
            context.close()
            browser.close()

    # ---- Pull fields from the responses ------------------------------------
    # For the old shared-portal URL, data comes from two blobs:
    #   portal_blob  → title, meta (price/type), html (remarks), images
    #   search_item  → streetAddress, city, bedrooms, bathrooms, images
    # For the new /view/listing/ URL, everything is in portal_blob, with
    # detailed fields nested under portal_blob["summary"].
    summary = portal_blob.get("summary") or {}  # new format nested fields

    # Title: top-level in portal blob, or build from address.
    data.title = (portal_blob.get("title") or "").strip()

    # Street + city: search result (old) or summary (new).
    data.street = (search_item.get("streetAddress") or summary.get("streetAddress") or "").strip()
    data.city   = (search_item.get("city")          or summary.get("city")          or "").strip()
    if not data.street and data.title:
        data.street, data.city = _split_address(data.title)

    # Street name without house number.
    raw_street_name = (search_item.get("streetName") or summary.get("streetName") or "").strip()
    if raw_street_name:
        data.street_name = raw_street_name
    elif data.street:
        data.street_name = re.sub(r"^\d+\s*", "", data.street).strip()

    # Price.
    price_num = (search_item.get("listPrice") or search_item.get("price")
                 or summary.get("listPrice") or summary.get("price") or "")
    price_fmt = (search_item.get("listPriceFormatted")
                 or summary.get("listPriceFormatted")
                 or portal_blob.get("meta", {}).get("price") or "")
    data.price, data.raw_price = _clean_price(price_fmt or str(price_num))

    # Type: sale vs rent.
    sale_or_rent = (
        search_item.get("saleOrRent")
        or summary.get("saleOrRent")
        or portal_blob.get("meta", {}).get("saleOrRent")
        or "SALE"
    ).lower()
    data.listing_type = "rent" if any(w in sale_or_rent for w in ("rent", "lease")) else "sale"

    # Description.
    data.description = _realm_description_from_html(portal_blob.get("html", ""))

    # Bedrooms / bathrooms -- check search_item first (old), then summary (new).
    beds = search_item.get("bedrooms", "") or summary.get("bedrooms", "")
    beds_extra = search_item.get("bedroomsPossible", 0) or summary.get("bedroomsPossible", 0)
    total_beds = (int(beds) + int(beds_extra or 0)) if beds else 0
    data.bedrooms = str(total_beds) if total_beds > 0 else ""
    baths = int(search_item.get("bathrooms", "") or summary.get("bathrooms", "") or 0)
    data.bathrooms = str(baths) if baths > 0 else ""
    data.property_type = _realm_type_to_fb(
        search_item.get("typeName", "") or summary.get("typeName", ""))
    sq = search_item.get("squareFeet", "") or summary.get("squareFeet", "") or ""
    data.square_feet = re.sub(r"[^\d]", "", str(sq).split("-")[0]) if sq else ""

    # Photos: prefer search_item images (old), fall back to portal_blob images (new).
    raw_photos = (search_item.get("images")
                  or portal_blob.get("images")
                  or summary.get("images") or [])
    realm_base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    for p in raw_photos:
        raw_u = ""
        if isinstance(p, str):
            raw_u = p
        elif isinstance(p, dict):
            raw_u = p.get("url") or ""
        if not raw_u:
            continue
        # Make relative URLs absolute using the Realm domain.
        if raw_u.startswith("/"):
            raw_u = realm_base + raw_u
        if raw_u.startswith("http"):
            data.photos.append(_fullres_photo_url(raw_u))
    data.photos = _dedupe_photos(data.photos)

    # Fallback: if we got almost nothing, try the standard HTML extractors.
    if not data.title and not data.street:
        data = parse_listing(captured_html, url)

    return data


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _clean_price(text) -> tuple:
    """Return (digits_only, raw) from a price string like '$1,250/mo'."""
    if not text:
        return "", ""
    raw = str(text).strip()
    digits = re.sub(r"[^\d]", "", raw.split(".")[0])  # drop cents + symbols
    return digits, raw


def _guess_type(text: str, price_digits: str) -> str:
    """Heuristic: is this a rental or a sale?"""
    t = (text or "").lower()
    rent_words = ("for rent", "for lease", "rental", "/mo", "per month", "monthly", "lease")
    if any(w in t for w in rent_words):
        return "rent"
    # Price-based fallback: rents are usually small, sale prices large.
    if price_digits.isdigit() and int(price_digits) < 15000:
        return "rent"
    return "sale"


def _split_address(address: str) -> tuple:
    """Split 'Street, City, Prov' into (street, city), best-effort."""
    if not address:
        return "", ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 1 else ""
    return street, city


def _normalize_images(img) -> list:
    """JSON-LD 'image' can be a string, list of strings, or list of objects."""
    out = []
    if isinstance(img, str):
        out = [img]
    elif isinstance(img, list):
        for it in img:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict) and it.get("url"):
                out.append(it["url"])
    elif isinstance(img, dict) and img.get("url"):
        out = [img["url"]]
    return out


def _dedupe_photos(urls: list) -> list:
    """Drop duplicates and obvious non-photos (icons, blanks)."""
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen or u.lower().endswith(".svg"):
            continue
        seen.add(u)
        out.append(u)
    return out


# ---------------------------------------------------------------------------
# Extractors (run in order; each only fills gaps left by the previous one)
# ---------------------------------------------------------------------------
def _from_json_ld(soup: BeautifulSoup, data: ListingData) -> None:
    """Fill fields from schema.org JSON-LD blocks, if present (most reliable)."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            blob = json.loads(tag.string or "")
        except Exception:
            continue
        # JSON-LD can be a dict, a list, or wrapped in @graph.
        if isinstance(blob, list):
            candidates = blob
        elif isinstance(blob, dict):
            candidates = blob.get("@graph", [blob])
        else:
            continue
        for node in candidates:
            if not isinstance(node, dict):
                continue
            if not data.title:
                data.title = (node.get("name") or "").strip()
            if not data.description:
                data.description = (node.get("description") or "").strip()
            data.photos += _normalize_images(node.get("image"))
            offers = node.get("offers")
            if isinstance(offers, dict) and not data.raw_price:
                price = offers.get("price") or (offers.get("priceSpecification") or {}).get("price")
                if price:
                    data.price, data.raw_price = _clean_price(price)
            addr = node.get("address")
            if isinstance(addr, dict) and not data.street:
                data.street = (addr.get("streetAddress") or "").strip()
                data.city = (addr.get("addressLocality") or "").strip()


def _from_opengraph(soup: BeautifulSoup, data: ListingData) -> None:
    """Fill any still-empty fields from <meta> / OpenGraph tags."""
    def meta(prop):
        tag = (soup.find("meta", attrs={"property": prop})
               or soup.find("meta", attrs={"name": prop}))
        return (tag.get("content") if tag else "") or ""

    if not data.title:
        data.title = meta("og:title").strip()
    if not data.description:
        data.description = meta("og:description").strip()
    if not data.raw_price:
        price = meta("product:price:amount") or meta("og:price:amount")
        if price:
            data.price, data.raw_price = _clean_price(price)
    for tag in soup.find_all("meta", attrs={"property": "og:image"}):
        if tag.get("content"):
            data.photos.append(tag["content"])


def _from_realtor_ca(soup: BeautifulSoup, data: ListingData) -> None:
    """realtor.ca-specific selectors. These DO change over time -- update freely.

    To add another site: copy this function, swap the selectors, then register
    it in SITE_EXTRACTORS below keyed by the site's domain.
    """
    def text_of(*selectors):
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return ""

    if not data.title:
        data.title = text_of("h1", "#listingAddress", ".listingAddress")
    if not data.raw_price:
        price = text_of("#listingPriceValue", ".listingPrice", "[data-testid='listing-price']")
        if price:
            data.price, data.raw_price = _clean_price(price)
    if not data.description:
        data.description = text_of(
            "#propertyDescriptionCon",
            ".propertyDescriptionCon",
            "[data-testid='listing-description']",
        )


# Map a domain substring -> extractor function. Add new sites here.
SITE_EXTRACTORS = {
    "realtor.ca": _from_realtor_ca,
}


# ---------------------------------------------------------------------------
# Top-level parse + scrape
# ---------------------------------------------------------------------------
def parse_listing(html: str, url: str) -> ListingData:
    """Run all extractors in order of reliability and merge the results."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")  # if lxml isn't installed

    data = ListingData(source_url=url)
    _from_json_ld(soup, data)     # 1) structured data (best)
    _from_opengraph(soup, data)   # 2) OpenGraph meta (good)

    domain = urlparse(url).netloc.lower()  # 3) site-specific gap-filling
    for key, extractor in SITE_EXTRACTORS.items():
        if key in domain:
            extractor(soup, data)

    # Post-processing.
    if data.title and not (data.street or data.city):
        data.street, data.city = _split_address(data.title)
    data.listing_type = _guess_type(
        f"{data.title} {data.description} {data.raw_price} {url}", data.price
    )
    data.photos = _dedupe_photos(data.photos)
    return data


def scrape_listing(url: str) -> ListingData:
    """Top-level: fetch the page and parse it into a ListingData.

    Routes to a site-specific fetcher when one exists (e.g. Realm's API
    interceptor), otherwise falls back to the standard HTML extractor.
    """
    domain = urlparse(url).netloc.lower()
    if "realmmlp.ca" in domain:
        return fetch_realm(url)
    return parse_listing(fetch_html(url), url)


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------
def download_photos(urls: list, dest_dir: str, limit: int = 20) -> list:
    """Download up to `limit` photos into dest_dir. Returns local file paths."""
    os.makedirs(dest_dir, exist_ok=True)
    paths = []
    for i, url in enumerate(urls[:limit]):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
            path = os.path.join(dest_dir, f"photo_{i + 1:02d}{ext}")
            with open(path, "wb") as f:
                f.write(resp.content)
            paths.append(path)
        except Exception as e:
            print(f"  ! Skipped a photo ({str(url)[:60]}...): {e}")
    return paths


# ---------------------------------------------------------------------------
# Manual fallback
# ---------------------------------------------------------------------------
def _read_block(prompt: str) -> str:
    """Read multi-line input until a blank line."""
    print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def manual_listing(url: str) -> ListingData:
    """Prompt you to type/paste the details when scraping comes up short."""
    print("\n--- Manual entry ---")
    data = ListingData(source_url=url)
    data.title = input("  Title / address: ").strip()
    data.raw_price = input("  Price (e.g. 750000 or 2200): ").strip()
    data.price, _ = _clean_price(data.raw_price)
    data.street = input("  Street (for the location field): ").strip()
    data.city = input("  City: ").strip()
    kind = input("  For [s]ale or for [r]ent? ").strip().lower()
    data.listing_type = "rent" if kind.startswith("r") else "sale"
    data.description = _read_block("  Description (paste, then a blank line to finish):")
    photo_block = _read_block("  Photo URLs (one per line, blank to finish) -- optional:")
    data.photos = [u.strip() for u in photo_block.splitlines() if u.strip()]
    return data
