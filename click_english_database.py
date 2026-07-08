"""Click the English database tab on the 90tsg resource page."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config


ENGLISH_DATABASE_XPATH = "/html/body/div[4]/div[2]/div/ul/li[2]/a"


def click_english_database(page: Page) -> None:
    locator = page.locator(f"xpath={ENGLISH_DATABASE_XPATH}")
    locator.wait_for(state="visible")
    locator.click()
    page.wait_for_load_state("domcontentloaded")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        click_english_database(page)
        input("English database tab was clicked. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
