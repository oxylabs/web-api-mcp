"""Offline checks for the bits that aren't just pass-through. Run: pytest"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["OXYLABS_WEB_API_KEY"] = "test-key"

from oxylabs_web_api_mcp import config  # noqa: E402 — after the path and key above

# Calls that collect queued requests answer at once; the tests that need the wait set it.
config.MAX_WAIT_FOR_QUEUED_SECONDS = 0
