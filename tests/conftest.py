"""
Stub out heavy optional dependencies so the pure-logic functions in
scraper/scrape.py can be imported without the full scraper stack installed.
curl_cffi and playwright are only needed for live network fetches — they are
never exercised by unit tests.
"""

import sys
from unittest.mock import MagicMock

for _mod in ("curl_cffi", "curl_cffi.requests"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
