"""
poster.py
---------
Main script. Scrapes a listing, adds your branding, then PRE-FILLS a Facebook
Marketplace listing and HANDS THE BROWSER TO YOU to review and click Publish.

It never logs in for you and never clicks Publish -- that's what keeps your
account off Facebook's automation radar. On the first run a browser opens; log
into Facebook by hand once. Your session is saved in the profile folder
(config.USER_DATA_DIR) and reused on every later run, so you won't log in again.

Usage:
    python poster.py "https://www.realtor.ca/real-estate/..."
    python poster.py                 # prompts for the URL
    python poster.py --manual        # skip scraping, type the details yourself
"""

import argparse
import shutil
import sys
from datetime import datetime

import config
import scraper

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
except Exception:
    print("Playwright isn't installed. Run:")
    print("    pip install -r requirements.txt && playwright install chromium")
    sys.exit(1)


# ============================================================================
# Facebook Marketplace selectors
# ----------------------------------------------------------------------------
# Facebook obfuscates its HTML and changes it often, so we target fields by
# their visible LABEL / placeholder text instead of brittle CSS classes. If a
# field stops filling, open Marketplace, read the box's label, and add it to
# the matching list below. Order = what we try first.
# ============================================================================
LABELS = {
    "title":       ["Title"],
    "price":       ["Price", "Price per month", "Monthly rent", "Rent"],
    "description": ["Property description", "Description"],
    "location":    ["Location", "Address", "City"],
}

# Marketplace category picker -- we navigate here then click the right tile.
MARKETPLACE_CREATE_URL = "https://www.facebook.com/marketplace/create"


# ---------------------------------------------------------------------------
# Input + logging
# ---------------------------------------------------------------------------
def get_args() -> tuple:
    """Read the listing URL (and --manual flag) from the command line."""
    parser = argparse.ArgumentParser(
        description="Pre-fill a Facebook Marketplace listing from a URL."
    )
    parser.add_argument("url", nargs="?", help="Listing URL to scrape")
    parser.add_argument("--manual", action="store_true",
                        help="Skip scraping; type the details yourself")
    args = parser.parse_args()
    url = args.url or input("Paste the listing URL: ").strip()
    return url, args.manual


def _has_terminal() -> bool:
    """True only when a real interactive terminal is attached.

    The Tkinter GUI and the .app bundle run with no stdin, so calling input()
    there raises EOFError. Use this to decide whether prompting is even possible.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def log_result(url: str, result: str) -> None:
    """Append the outcome to log.txt with a timestamp."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{stamp} | {result.upper():7} | {url}\n")


# ---------------------------------------------------------------------------
# Form-fill helpers (all best-effort; they warn instead of crashing)
# ---------------------------------------------------------------------------
def try_fill(page, label_candidates, value) -> bool:
    """Fill a field identified by its visible label text.

    Tries, in order: get_by_label -> get_by_placeholder -> [aria-label=...].
    Returns True on the first strategy that works.
    """
    if not value:
        return False
    for label in label_candidates:
        for locator in (
            page.get_by_label(label, exact=False),
            page.get_by_placeholder(label, exact=False),
            page.locator(f"[aria-label='{label}']"),
        ):
            try:
                el = locator.first
                el.wait_for(state="visible", timeout=2500)
                el.click()
                try:
                    el.fill(str(value))
                except Exception:
                    # contenteditable / custom widgets: type via the keyboard
                    page.keyboard.type(str(value), delay=10)
                return True
            except Exception:
                continue
    print(f"  ! Couldn't find a field for {label_candidates} -- fill it by hand.")
    return False


def fill_location(page, data) -> None:
    """Type the city (or street) into the location field and pick the first suggestion.

    Facebook Marketplace's location input has no stable label — we probe each
    visible empty text input. We try the city name first (most reliable for
    Marketplace autocomplete), then street name, then full street as fallbacks.
    """
    # Build a priority list of queries to try — city first (most reliable).
    queries = []
    if data.city:
        queries.append(data.city)
    if data.street_name and data.street_name.lower() != (data.city or "").lower():
        queries.append(data.street_name)
    if data.street and data.street.lower() != (data.street_name or "").lower():
        queries.append(data.street)
    if not queries:
        return

    def _try_query(query):
        """Try one query string; return True if an autocomplete option was clicked."""
        all_inputs = page.locator("input[type='text'], input:not([type])").all()
        for inp in all_inputs:
            try:
                if not inp.is_visible():
                    continue
                if (inp.get_attribute("aria-label") or "").lower() == "search facebook":
                    continue
                if inp.input_value():
                    continue
                inp.click()
                page.wait_for_timeout(400)
                inp.press_sequentially(query, delay=80)
                page.wait_for_timeout(2500)
                options = page.get_by_role("option").all()
                if options:
                    options[0].click()
                    print(f"  + Location set: {options[0].text_content()[:60]}")
                    return True
                inp.fill("")
            except Exception:
                continue
        return False

    for q in queries:
        if _try_query(q):
            return

    # Last check: Facebook may have auto-filled the city already.
    city = (data.city or "").lower()
    if city:
        for inp in page.locator("input[type='text'], input:not([type])").all():
            try:
                if city in (inp.input_value() or "").lower():
                    return
            except Exception:
                continue

    print("  ⚠ Location: type the street name in the browser.")


def upload_photos(page, photo_paths) -> None:
    """Attach downloaded photos via the (hidden) file input."""
    if not photo_paths:
        return
    try:
        page.locator("input[type='file']").first.set_input_files(photo_paths)
        page.wait_for_timeout(2500)  # let thumbnails upload
        print(f"  ✅ {len(photo_paths)} photos uploaded.")
    except Exception as e:
        print(f"  ! Photo upload failed ({e}) -- drag them in by hand.")


def select_dropdown(page, label_candidates, value) -> bool:
    """Pick an option from a Facebook dropdown by its visible label text.

    Facebook renders dropdowns as combobox / listbox pairs. Strategy:
      1. Find the trigger element by label/placeholder/aria-label.
      2. Click it to open the option list.
      3. Find the option whose text matches `value` (case-insensitive, partial).
      4. Click the matching option.

    Returns True on success.
    """
    if not value:
        return False
    for label in label_candidates:
        for locator in (
            page.get_by_label(label, exact=False),
            page.get_by_placeholder(label, exact=False),
            page.locator(f"[aria-label='{label}']"),
        ):
            try:
                el = locator.first
                el.wait_for(state="visible", timeout=2500)
                el.click()
                page.wait_for_timeout(800)  # let the option list appear
                # Try to click an option whose text matches the value.
                option = page.get_by_role("option", name=value)
                if option.count() == 0:
                    # Partial / case-insensitive fallback.
                    option = page.locator(
                        f"[role='option']:has-text('{value}')"
                    )
                if option.count() > 0:
                    option.first.click(timeout=3000)
                    return True
                # No matching option found -- close the dropdown and try next label.
                page.keyboard.press("Escape")
            except Exception:
                continue
    print(f"  ! Couldn't set dropdown '{label_candidates}' to '{value}' -- set it by hand.")
    return False


# Default values for advanced fields -- these match Facebook's exact option text.
ADVANCED_DEFAULTS = {
    "laundry":  "Laundry available",
    "parking":  "Parking available",
    "ac":       "AC available",
    "heating":  "Central heating",
}


def fill_number_input(page, label_candidates, value) -> bool:
    """Fill a number input (bedrooms, bathrooms, sqft) by clearing then typing.

    These are plain <input type='number'> or text fields -- NOT dropdowns.
    """
    if not value:
        return False
    for label in label_candidates:
        for locator in (
            page.get_by_label(label, exact=False),
            page.get_by_placeholder(label, exact=False),
            page.locator(f"[aria-label='{label}']"),
        ):
            try:
                el = locator.first
                el.wait_for(state="visible", timeout=2500)
                el.click()
                el.fill(str(value))
                return True
            except Exception:
                continue
    print(f"  ! Couldn't fill number field {label_candidates} -- fill it by hand.")
    return False


def fill_marketplace_form(page, data, description) -> None:
    """Pre-fill every field on the Home for Sale/Rent create-listing page.

    Fill order matters: sale/rent type is set BEFORE price so Facebook formats
    the price correctly (not as a monthly rental amount).
    """

    # 1. Title -- fill first so it doesn't get clobbered by later field clicks.
    if data.title:
        try_fill(page, LABELS["title"], data.title)

    # 2. Property type dropdown (Townhouse, House, Condo, etc.)
    if data.property_type:
        select_dropdown(page, ["Property type", "Home type", "Type"], data.property_type)
        page.wait_for_timeout(500)

    # 2. Bedrooms + bathrooms -- plain number inputs, not dropdowns.
    if data.bedrooms:
        fill_number_input(page, ["Number of bedrooms", "Bedrooms", "Beds"], data.bedrooms)
    if data.bathrooms:
        fill_number_input(page, ["Number of bathrooms", "Bathrooms", "Baths"], data.bathrooms)

    # 3. Price (after type is confirmed so FB knows sale vs. rent format).
    try_fill(page, LABELS["price"], data.price)

    # 4. Location -- street name only, no house number.
    fill_location(page, data)

    # 5. Description with branding.
    try_fill(page, LABELS["description"], description)

    # 6. Advanced details -- scroll down to make them visible first.
    page.evaluate("window.scrollBy(0, 600)")
    page.wait_for_timeout(800)

    select_dropdown(
        page, ["Laundry type", "Laundry"], ADVANCED_DEFAULTS["laundry"]
    )
    select_dropdown(
        page, ["Parking type", "Parking"], ADVANCED_DEFAULTS["parking"]
    )
    select_dropdown(
        page, ["Air conditioning type", "Air conditioning", "AC type"], ADVANCED_DEFAULTS["ac"]
    )
    select_dropdown(
        page, ["Heating type", "Heating"], ADVANCED_DEFAULTS["heating"]
    )

    # 7. Photos -- upload last so thumbnails don't push fields off screen mid-fill.
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    upload_photos(page, data.photo_paths)

    # Photos done -- caller must call save_draft(page) after this returns.


# ---------------------------------------------------------------------------
# Save draft
# ---------------------------------------------------------------------------
def save_draft(page) -> bool:
    """Click the Save draft button and wait for confirmation.

    Returns True on success. Called BEFORE closing the browser context
    so the click has time to complete.
    """
    page.wait_for_timeout(4000)   # let photo uploads fully settle

    # Facebook has used several different labels for this button over time.
    button_names = ("Save draft", "Save Draft", "Save as draft",
                    "Save as Draft", "Save listing", "Save")

    for name in button_names:
        for locator in (
            page.get_by_role("button", name=name, exact=False),
            page.get_by_text(name, exact=False),
        ):
            try:
                if locator.count() == 0:
                    continue
                btn = locator.first
                # Scroll it into view in case it's below the fold.
                btn.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(500)
                btn.click(timeout=6000)
                page.wait_for_timeout(4000)   # wait for FB to confirm the save
                print("  ✅ Draft saved — go to Marketplace → Selling → Drafts to publish.")
                return True
            except Exception:
                continue

    print("  ! Couldn't click Save draft automatically — click it manually in the browser.")
    return False


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def get_listing(url: str, manual: bool):
    """Return a usable ListingData, scraping first and falling back to manual."""
    if manual:
        return scraper.manual_listing(url)

    data = None
    try:
        data = scraper.scrape_listing(url)
        if data and data.is_usable():
            beds_baths = f"{data.bedrooms or '?'} bed / {data.bathrooms or '?'} bath"
            listing_type = "For rent" if data.listing_type == "rent" else "For sale"
            price_str = (f"  ·  ${int(data.price):,}" if data.price and str(data.price).isdigit()
                         else (f"  ·  {data.raw_price}" if data.raw_price else ""))
            location = ", ".join(filter(None, [data.street, data.city]))
            print(f"✅ {beds_baths}  ·  {listing_type}{price_str}")
            if location:
                print(f"   {location}")
            print(f"   {len(data.photos)} photos found")
    except Exception as e:
        print(f"  ⚠ Scrape failed: {e}")

    if not data or not data.is_usable():
        # If we still got *something* worth pre-filling, use it rather than fail.
        if data and (data.title or data.street or data.description or data.photos):
            print("  ⚠ Some details were missing — filling in what we found.")
            return data

        # Only prompt for manual entry when there's a real terminal attached.
        # The GUI / .app bundle has no stdin, so input() would raise EOFError.
        if _has_terminal():
            print("  Scrape came up short (the site may have blocked it).")
            if input("  Enter details manually? [Y/n] ").strip().lower() in ("", "y", "yes"):
                return scraper.manual_listing(url)
            return None

        print("  ❌ Couldn't read this listing (the site may have blocked it).")
        return None
    return data


def cleanup() -> None:
    """Delete the temporary photo folder."""
    shutil.rmtree(config.TEMP_PHOTO_DIR, ignore_errors=True)


def main() -> None:
    url, manual = get_args()
    if not url:
        print("No URL given. Exiting.")
        return

    # ---- 1) Get the listing data (scrape, with manual fallback) ------------
    data = get_listing(url, manual)
    if data is None:
        log_result(url, "fail")
        return

    # ---- 2) Apply your branding to the description -------------------------
    description = config.apply_branding(data.description)
    print("\nDescription that will be posted:\n" + "-" * 40)
    print(description)
    print("-" * 40)

    # ---- 3) Download photos to a temp folder -------------------------------
    print("\nDownloading photos...")
    data.photo_paths = scraper.download_photos(
        data.photos, config.TEMP_PHOTO_DIR, limit=config.MAX_PHOTOS
    )
    print(f"  {len(data.photo_paths)} photo(s) ready.")

    # ---- 5) Review gate ----------------------------------------------------
    print("\nReady to open Facebook and pre-fill the listing.")
    if input("Continue? [Enter to go, or 'q' to quit] ").strip().lower() == "q":
        cleanup()
        return

    # ---- 6) Open Facebook (persistent session) and fill the form -----------
    result = "fail"
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                config.USER_DATA_DIR,
                headless=config.HEADLESS,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()

            # First-run login check.
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if "login" in page.url or page.locator("input[name='email']").count() > 0:
                print("\n>>> Please LOG INTO FACEBOOK in the open browser window.")
                input(">>> Once you're on your Facebook home feed, press Enter here...")

            # Navigate to the category picker and click "Home for sale or rent".
            # This is more reliable than a direct URL (Facebook ignores them).
            print("\nOpening Marketplace category picker...")
            page.goto(MARKETPLACE_CREATE_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # Click the "Home for sale or rent" tile (text seen in the UI).
            home_tile = page.get_by_text("Home for sale or rent", exact=False)
            if home_tile.count() == 0:
                # Fallback label variants Facebook has used.
                for label in ("Home for Sale or Rent", "Property for sale", "Real estate"):
                    home_tile = page.get_by_text(label, exact=False)
                    if home_tile.count() > 0:
                        break
            if home_tile.count() > 0:
                home_tile.first.click()
                page.wait_for_timeout(2500)
                print("  Clicked 'Home for sale or rent' tile.")
            else:
                print("  ! Couldn't find the Home tile -- you may need to click it manually.")
                input("  Click 'Home for sale or rent' in the browser, then press Enter...")

            # Set For Sale / For Rent BEFORE anything else so Facebook formats
            # the price field correctly (sale = total, rent = per month).
            sale_or_rent_label = "For Rent" if data.listing_type == "rent" else "For Sale"
            page.wait_for_timeout(1000)
            success = select_dropdown(
                page,
                ["Home for Sale or Rent", "Sale or Rent", "Listing type"],
                sale_or_rent_label,
            )
            if not success:
                # Try clicking any visible combobox and picking from the list.
                try:
                    page.locator("[role='combobox']").first.click()
                    page.wait_for_timeout(800)
                    page.get_by_role("option", name=sale_or_rent_label).first.click()
                except Exception:
                    print(f"  ! Set '{sale_or_rent_label}' manually in the browser.")
            page.wait_for_timeout(1500)  # let form re-render after type change

            fill_marketplace_form(page, data, description)

            # ---- 7) Hand control back to you to review and Publish ---------
            print("\n" + "=" * 60)
            print("  Form pre-filled. NOW IT'S YOUR TURN:")
            print("  1. Check every field (especially CATEGORY + LOCATION).")
            print("  2. Add anything the script missed.")
            print("  3. Click PUBLISH yourself.")
            print("=" * 60)
            answer = input("After publishing type 'done' (or 'fail' to skip): ").strip().lower()
            result = "success" if answer == "done" else "fail"

            context.close()
    except PWTimeout as e:
        print(f"  ! Timed out: {e}")
    except Exception as e:
        print(f"  ! Error: {e}")
    finally:
        log_result(url, result)
        cleanup()
        print(f"\nLogged result: {result}. Temp photos cleaned up. Done.")


if __name__ == "__main__":
    main()
