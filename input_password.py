"""Fill the configured password on the 90tsg login page."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config


PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[id*="pass" i]',
    'input[placeholder*="密码"]',
    'input[type="password"]',
]


def fill_password(page: Page) -> None:
    for selector in PASSWORD_SELECTORS:
        locator = page.locator(selector).first
        if locator.count():
            locator.fill(config.password)
            return
    raise RuntimeError("Password input was not found.")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        fill_password(page)
        input("Password is filled. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
