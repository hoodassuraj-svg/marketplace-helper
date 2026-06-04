"""
config.py
---------
Your settings live here so you only set them once. Everything in the BRANDING
section is safe to edit by hand anytime -- those values get added to every
listing description before the script fills the Marketplace form.
"""

import os

from dotenv import load_dotenv

# Load optional overrides from a .env file if one exists (see .env.example).
load_dotenv()


# ============================================================================
# BRANDING  --  edit these freely
# ============================================================================

# Your business / brokerage name. Leave as "" to hide it.
BUSINESS_NAME = ""

# The call-to-action / contact line added to every listing.
CALL_TO_ACTION = "Call Rashmi Hooda at 647-766-5040"

# Where to put the branding in the description: "bottom" (default) or "top".
BRANDING_POSITION = "bottom"

# Drawn between the listing text and your branding block.
BRANDING_SEPARATOR = "\n\n--------------------\n"


def build_branding_block() -> str:
    """Assemble the branding text. Empty values are skipped."""
    lines = []
    if BUSINESS_NAME.strip():
        lines.append(BUSINESS_NAME.strip())
    if CALL_TO_ACTION.strip():
        lines.append(CALL_TO_ACTION.strip())
    return "\n".join(lines)


def apply_branding(description: str) -> str:
    """Return the description with the branding block added top or bottom."""
    branding = build_branding_block()
    if not branding:
        return (description or "").strip()
    description = (description or "").strip()
    if BRANDING_POSITION.strip().lower() == "top":
        return branding + BRANDING_SEPARATOR + description
    return description + BRANDING_SEPARATOR + branding  # default: bottom


# ============================================================================
# BROWSER / RUNTIME  --  sensible defaults; override via .env if you like
# ============================================================================

# Folder where Playwright stores your Facebook login session, so you only log
# in once. Treat it as private -- it effectively holds your logged-in cookies.
USER_DATA_DIR = os.getenv("FB_PROFILE_DIR", os.path.abspath("./.fb_profile"))

# Run the browser visibly (False) so you can watch it fill the form and click
# Publish yourself. Headless (True) is NOT recommended for the posting step.
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() == "true"

# Temp folder for downloaded photos (auto-created, auto-deleted each run).
TEMP_PHOTO_DIR = os.path.abspath("./_temp_photos")

# Where run results are appended.
LOG_FILE = os.path.abspath("./log.txt")

# Max photos to upload. Facebook's home listing cap is 50.
MAX_PHOTOS = 50
