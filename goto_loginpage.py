"""Open the configured 90tsg login page."""

from __future__ import annotations

from playwright.sync_api import sync_playwright

import config


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        input("Login page is open. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
