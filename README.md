# Marketplace Listing Helper

Scrapes a real-estate listing, adds your branding, and **pre-fills** a Facebook
Marketplace listing for you to review and Publish.

It **never logs in for you and never clicks Publish** — you do both. That keeps
your account off Facebook's automation radar (scripted logins + bot posting are
the #1 ban trigger) while still saving you the slow part: reformatting the
description, downloading every photo, and filling every field.

> Only post listings you own or are authorized to advertise. The tool fills a
> form; you are responsible for what you publish.

---

## Setup (once)

```bash
cd ~/Projects/marketplace-helper
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
playwright install chromium                          # downloads the browser
```

(Optional) copy the env template if you want to change defaults:

```bash
cp .env.example .env
```

## Set your branding

Edit **`config.py`**:

| Setting             | What it does                                        |
|---------------------|-----------------------------------------------------|
| `CALL_TO_ACTION`    | Already set to `Call Rashmi Hooda at 647-766-5040`  |
| `BUSINESS_NAME`     | Optional name shown above the call-to-action        |
| `BRANDING_POSITION` | `"bottom"` (default) or `"top"`                     |

## Run it

```bash
python poster.py "https://www.realtor.ca/real-estate/..."
```

or just `python poster.py` and paste the URL when asked.
Use `python poster.py --manual` to skip scraping and type the details yourself.

### First run
A browser window opens. **Log into Facebook by hand once.** Your session is
saved in `./.fb_profile` and reused next time, so you won't log in again.

### Every run
1. The script scrapes the listing (or you paste details if it's blocked).
2. It downloads the photos and builds your description + branding.
3. It opens the Marketplace create form and fills everything it can.
4. **It stops.** You review every field — especially **category** and
   **location** — then click **Publish** yourself.

---

## How it's organized

| File              | Role                                                        |
|-------------------|-------------------------------------------------------------|
| `poster.py`       | Main script: orchestrates scrape → fill → you publish       |
| `scraper.py`      | Extracts listing data; realtor.ca built in, easy to extend  |
| `config.py`       | Your branding + runtime settings                            |
| `requirements.txt`| Dependencies                                                |
| `.env.example`    | Optional settings template (no passwords stored)            |
| `log.txt`         | Auto-generated: `timestamp | result | url` per run          |

## Adding another listing site

In `scraper.py`, copy `_from_realtor_ca`, swap the CSS selectors, and register
it in `SITE_EXTRACTORS` keyed by the site's domain. The JSON-LD and OpenGraph
layers already work on many sites with **no** extra code.

## When things break (they will, occasionally)

- **realtor.ca blocks the scrape** → the script drops to manual entry. Paste the
  details once; photo download + form-fill still save you the time.
- **A field won't fill** → Facebook changed its HTML. Open the form, read the
  box's label, and add it to the matching list in `LABELS` at the top of
  `poster.py`.
- **Location won't match** → Marketplace's location box is usually *city-level*,
  so a full street address may not resolve. See `fill_location()` to prefer city.
