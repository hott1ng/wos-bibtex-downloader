"""Click the 90tsg login button and wait for navigation."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config


LOGIN_BUTTON_TEXTS = ["立即登录", "登录", "Login", "Sign in"]


def click_login_button(page: Page) -> None:
    for text in LOGIN_BUTTON_TEXTS:
        locator = page.get_by_text(text, exact=False).first
        if locator.count():
            locator.click()
            page.wait_for_load_state("domcontentloaded")
            return

    submit = page.locator('button[type="submit"], input[type="submit"]').first
    if not submit.count():
        raise RuntimeError("Login button was not found.")
    submit.click()
    page.wait_for_load_state("domcontentloaded")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        click_login_button(page)
        input("Login button was clicked. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
