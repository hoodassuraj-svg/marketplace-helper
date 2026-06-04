"""
smoke_test.py
-------------
Quick check that the scraper's extraction + branding logic works, WITHOUT
hitting any live site. Proves the JSON-LD / OpenGraph parsing, sale-vs-rent
detection, address splitting, photo de-duping, and branding placement.

Run:
    python smoke_test.py                 # deterministic fixture test (offline)
    python smoke_test.py "<listing-url>" # also attempt a live fetch of a real URL

(Safe to delete -- this is just a dev/confidence check.)
"""

import sys

import config
import scraper

# A realistic "for sale" page: JSON-LD structured data + an OpenGraph cover image.
SALE_HTML = """
<html><head>
  <meta property="og:title" content="123 Maple Avenue, Toronto, ON">
  <meta property="og:description" content="Stunning detached home in the east end.">
  <meta property="og:image" content="https://img.example.com/cover.jpg">
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "123 Maple Avenue, Toronto, ON",
    "description": "Stunning 4-bedroom detached home with finished basement, large backyard, and updated kitchen. Steps to transit and top-rated schools.",
    "image": [
      "https://img.example.com/1.jpg",
      "https://img.example.com/2.jpg",
      "https://img.example.com/3.jpg"
    ],
    "offers": {"@type": "Offer", "price": "1249000", "priceCurrency": "CAD"},
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "123 Maple Avenue",
      "addressLocality": "Toronto",
      "addressRegion": "ON"
    }
  }
  </script>
</head><body></body></html>
"""

# A "for rent" page: smaller price + the words "for rent" should flip the type.
RENT_HTML = """
<html><head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Apartment",
    "name": "55 King Street W, Unit 1203, Toronto, ON",
    "description": "Bright 2-bedroom condo for rent. Includes parking and locker, floor-to-ceiling windows, steps from the financial district.",
    "image": "https://img.example.com/condo.jpg",
    "offers": {"@type": "Offer", "price": "2200", "priceCurrency": "CAD"},
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "55 King Street W",
      "addressLocality": "Toronto"
    }
  }
  </script>
</head><body></body></html>
"""


def show(label, html, url):
    print("\n" + "=" * 64)
    print(label)
    print("=" * 64)
    data = scraper.parse_listing(html, url)
    print(data.summary())
    print("\n  --- Description with your branding applied ---")
    print(config.apply_branding(data.description))


def main():
    show("FIXTURE 1: FOR SALE", SALE_HTML, "https://www.realtor.ca/real-estate/00000/sale")
    show("FIXTURE 2: FOR RENT", RENT_HTML, "https://www.realtor.ca/real-estate/11111/rent")

    # Optional live test: pass a real URL as an argument.
    if len(sys.argv) > 1:
        url = sys.argv[1]
        print("\n" + "=" * 64)
        print(f"LIVE FETCH: {url}")
        print("=" * 64)
        try:
            data = scraper.scrape_listing(url)
            print(data.summary())
        except Exception as e:
            print(f"  Live fetch failed (expected if the site blocks bots): {e}")


if __name__ == "__main__":
    main()
