"""Offline checks for the bits that aren't just pass-through. Run: pytest"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["OXYLABS_WEB_API_KEY"] = "test-key"
