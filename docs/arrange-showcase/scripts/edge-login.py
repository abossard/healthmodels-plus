"""Sign in to the Azure portal once in Edge and save the session for the Playwright MCP servers.

Opens Edge with a profile kept in .playwright/edge-profile, waits until the portal has loaded
after you signed in, and writes .playwright/auth.json, which every Playwright MCP server of the
showcase loads with --isolated --storage-state.

    uv run --no-project --with playwright==1.55.0 python docs/arrange-showcase/scripts/edge-login.py
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
PROFILE, STATE = ROOT / '.playwright' / 'edge-profile', ROOT / '.playwright' / 'auth.json'
PORTAL = 'https://portal.azure.com/'

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(PROFILE, channel='msedge', headless=False, no_viewport=True)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(PORTAL)
    deadline, steady = time.time() + 600, 0
    while steady < 5:  # the portal first loads, then redirects to the sign-in page; wait until it stays
        if time.time() > deadline:
            sys.exit('No portal sign-in within 10 minutes')
        page.wait_for_timeout(2000)
        steady = steady + 1 if page.url.startswith(PORTAL + '#') else 0
    page.wait_for_timeout(15000)  # let the portal fetch its tokens before the session is saved
    context.storage_state(path=STATE, indexed_db=True)
    print(f'Saved {STATE}')
    context.close()
