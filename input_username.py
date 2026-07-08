"""Fill the configured username on the 90tsg login page."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config


USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[name="userName"]',
    'input[name="account"]',
    'input[id*="user" i]',
    'input[placeholder*="账号"]',
    'input[placeholder*="用户名"]',
    'input[type="text"]',
]


def fill_username(page: Page) -> None:
    for selector in USERNAME_SELECTORS:
        locator = page.locator(selector).first
        if locator.count():
            locator.fill(config.username)
            return
    raise RuntimeError("Username input was not found.")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        fill_username(page)
        input("Username is filled. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
