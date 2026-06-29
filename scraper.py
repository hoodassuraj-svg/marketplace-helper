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
from urllib.parse import urlparse, parse_qs

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


def _pick(*values) -> str:
    """Return the first non-empty, non-None value as a stripped string.

    Skips None, empty strings, and whitespace-only strings.
    Does NOT skip "0" — callers that want to skip zero handle it themselves.
    """
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def fetch_realm(url: str) -> ListingData:
    """Fetch a Realm MLP portal listing by intercepting its API responses.

    Both URL formats Realm uses return a portal blob that always contains:
      - top-level: title, meta (price/type), html (remarks), images, imageSets
      - portal_blob["summary"]: streetAddress, city, bedrooms, bathrooms,
        listPrice, typeName, squareFeet, listingID

    The old ?active= format also fires a separate searchResults API call.
    We use portal_blob["summary"] as the primary source for all formats,
    and searchResults as a secondary source when available.
    """
    # ---- Extract active listing ID from the URL ----------------------------
    # Handles all known Realm URL formats:
    #   ?active=TREB-N13173246          (old shared portal, query param)
    #   /view/listing/TREB-N13171452    (new direct view, with board prefix)
    #   /view/listing/N13171452         (new direct view, no prefix)
    parsed_url = urlparse(url)
    qs = parse_qs(parsed_url.query)
    active_id = ""
    active_param = qs.get("active", [""])[0]
    if active_param:
        # Strip board prefix: TREB-N13173246 → N13173246
        active_id = active_param.split("-", 1)[-1] if "-" in active_param else active_param
    if not active_id:
        # Path: /view/listing/TREB-N13171452 or /view/listing/N13171452
        m = re.search(r"/listing/(?:[A-Z]+-)?(\w+)", parsed_url.path)
        if m:
            active_id = m.group(1)
    if not active_id:
        # Last resort: any board-prefixed ID anywhere in the path
        m = re.search(r"/([A-Z]{2,6}-[A-Z]\d+)", parsed_url.path)
        if m:
            active_id = m.group(1).split("-", 1)[-1]
    realm_base = f"{parsed_url.scheme}://{parsed_url.netloc}"

    data = ListingData(source_url=url)
    portal_blob: dict = {}
    search_item: dict = {}
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
                if not isinstance(blob, dict):
                    return
            except Exception:
                return

            # Portal blob: reliably identified by having BOTH "html" (remarks)
            # AND at least one of the structured-data keys. Requiring "html"
            # prevents analytics/search blobs (which share meta/images keys)
            # from being mistakenly accepted as the portal blob.
            if "html" in blob and any(k in blob for k in ("summary", "meta", "imageSets", "images")):
                portal_blob = blob

            # Search results blob (old format only).
            if "searchResults" in blob:
                items = blob["searchResults"].get("data") or []
                if items and isinstance(items[0], dict):
                    matched = None
                    for item in items:
                        item_id = str(item.get("listingID", "") or item.get("_id", ""))
                        if active_id and active_id in item_id:
                            matched = item
                            break
                    search_item = matched or items[0]

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

    # ---- Extract fields ----------------------------------------------------
    # summary (inside portal_blob) is the primary source for both URL formats.
    # search_item (from searchResults) is secondary — only present in old format.
    summary = portal_blob.get("summary") or {}
    meta    = portal_blob.get("meta")    or {}

    data.title = _pick(portal_blob.get("title"), search_item.get("title"))

    data.street = _pick(summary.get("streetAddress"), search_item.get("streetAddress"))
    data.city   = _pick(summary.get("city"), search_item.get("city"),
                        summary.get("municipality"), search_item.get("municipality"))
    if not data.street and data.title:
        data.street, data.city = _split_address(data.title)

    raw_sn = _pick(summary.get("streetName"), search_item.get("streetName"))
    data.street_name = raw_sn if raw_sn else re.sub(r"^\d+\s*", "", data.street).strip()

    price_num = _pick(summary.get("listPrice"), summary.get("price"),
                      search_item.get("listPrice"), search_item.get("price"))
    price_fmt = _pick(summary.get("listPriceFormatted"), search_item.get("listPriceFormatted"),
                      meta.get("price"))
    data.price, data.raw_price = _clean_price(price_fmt or price_num)

    sale_or_rent = _pick(summary.get("saleOrRent"), search_item.get("saleOrRent"),
                         meta.get("saleOrRent")) or "SALE"
    data.listing_type = "rent" if any(w in sale_or_rent.lower() for w in ("rent","lease")) else "sale"

    data.description = _realm_description_from_html(portal_blob.get("html", ""))

    beds = _pick(summary.get("bedrooms"), search_item.get("bedrooms"))
    try:
        beds_extra = int(summary.get("bedroomsPossible") or search_item.get("bedroomsPossible") or 0)
        total_beds = (int(beds) + beds_extra) if beds else 0
    except (ValueError, TypeError):
        total_beds = int(beds) if beds and str(beds).isdigit() else 0
    data.bedrooms = str(total_beds) if total_beds > 0 else ""

    baths_raw = _pick(summary.get("bathrooms"), search_item.get("bathrooms"),
                      summary.get("bathroomsTotal"), search_item.get("bathroomsTotal"))
    try:
        baths = int(float(baths_raw)) if baths_raw else 0
    except (ValueError, TypeError):
        baths = 0
    data.bathrooms = str(baths) if baths > 0 else ""

    type_name = _pick(summary.get("typeName"), search_item.get("typeName"),
                      summary.get("propertyType"), search_item.get("propertyType"))
    data.property_type = _realm_type_to_fb(type_name)

    sq_raw = _pick(summary.get("squareFeet"), search_item.get("squareFeet"),
                   summary.get("sqft"), search_item.get("sqft"))
    data.square_feet = re.sub(r"[^\d]", "", str(sq_raw).split("-")[0]) if sq_raw else ""

    # ---- Photos ------------------------------------------------------------
    # IMPORTANT: every photo is published under *two* different CDN URLs:
    #   - portal_blob["images"] / imageSets[*]["url"]: a /i/... imgproxy path
    #     served off the portal host (session-tied, transform baked in)
    #   - imageSets[*]["sizes"]: public stratuscollab CDN URLs whose last
    #     path segment base64-decodes to the full-res TRREB/ampre original
    # Collecting from more than one of these double-counts each photo, so a
    # 27-photo listing becomes 54 URLs and Facebook's 50-photo cap fills up
    # with duplicates (or drops real photos). We therefore take exactly ONE
    # canonical, publicly-downloadable, full-res URL per photo.
    #
    # imageSets is the richest source and is present in BOTH URL formats, so
    # it is the primary. search_item/images are fallbacks only when a listing
    # has no imageSets.
    def _imageset_url(item):
        """Best full-res, publicly downloadable URL for one imageSets entry."""
        if not isinstance(item, dict):
            return item if isinstance(item, str) else ""
        sizes = item.get("sizes") or {}
        if sizes:
            # Any size decodes to the same 1920px original; pick the largest
            # so the resize-bump fallback (if decoding fails) is highest res.
            try:
                biggest = sizes[max(sizes, key=lambda k: int(k))]
            except (ValueError, TypeError):
                biggest = next(iter(sizes.values()), "")
            if biggest:
                return _fullres_photo_url(biggest)
        # No sizes: fall back to the proxy URL (resolved/decoded below).
        return _pick(item.get("downloadUrl"), item.get("url"))

    collected = []
    image_sets = portal_blob.get("imageSets") or []
    if image_sets:
        collected = [_imageset_url(it) for it in image_sets]
    else:
        # Fallback for listings without imageSets: plain image lists.
        def _plain_url(p):
            if isinstance(p, str):
                return p
            if isinstance(p, dict):
                return _pick(p.get("url"), p.get("src"), p.get("downloadUrl"))
            return ""
        for source in (search_item.get("images"), portal_blob.get("images")):
            for p in (source or []):
                u = _plain_url(p)
                if u:
                    collected.append(_fullres_photo_url(u))

    # Resolve relative /i/... proxy paths against the portal host.
    resolved = []
    for u in collected:
        u = (u or "").strip()
        if u.startswith("/"):
            u = realm_base + u
        if u.startswith("http"):
            resolved.append(u)

    data.photos = _dedupe_photos(resolved)

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


def _photo_identity(u: str) -> str:
    """A stable key for the underlying photo, ignoring CDN/transform wrapping.

    The same image is served under several URLs (full-res ampre, stratuscollab
    resize proxy, portal /i/ proxy). For ampre originals the first path segment
    is a content hash that is unique per image -- dedupe on that so the same
    photo coming from two sources collapses to one. Otherwise fall back to the
    URL itself.
    """
    m = re.search(r"ampre\.ca/([A-Za-z0-9_-]{16,})", u)
    if m:
        return m.group(1)
    return u


def _dedupe_photos(urls: list) -> list:
    """Drop duplicates and obvious non-photos (icons, blanks)."""
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip()
        if not u or u.lower().endswith(".svg"):
            continue
        key = _photo_identity(u)
        if key in seen:
            continue
        seen.add(key)
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
