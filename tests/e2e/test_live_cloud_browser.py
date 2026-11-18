"""Live smoke tests for the hosted browsers. Each one rents a session on your
account, so it also needs that provider's API key."""

from __future__ import annotations

import asyncio
import os

import pytest

from ats_scrapers.cloud_browser import (
    BrowserbaseBrowser,
    BrowserUseBrowser,
    CDPCloudBrowser,
    KernelBrowser,
    NotteBrowser,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("ATS_SCRAPERS_LIVE_E2E"),
        reason="set ATS_SCRAPERS_LIVE_E2E=1 to rent real browser sessions",
    ),
]


@pytest.mark.parametrize(
    ("cls", "env_var"),
    [
        (BrowserbaseBrowser, "BROWSERBASE_API_KEY"),
        (BrowserUseBrowser, "BROWSER_USE_API_KEY"),
        (KernelBrowser, "KERNEL_API_KEY"),
        (NotteBrowser, "NOTTE_API_KEY"),
    ],
)
async def test_live_render(cls: type[CDPCloudBrowser], env_var: str) -> None:
    if not os.getenv(env_var):
        pytest.skip(f"set {env_var} to rent a {cls.provider} session")

    async with asyncio.timeout(180):
        async with cls() as browser:
            page = await browser.render("https://example.com/")

    assert "Example Domain" in page.html
    assert page.url.startswith("https://example.com")
