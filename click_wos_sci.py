"""Click the Web of Science/SCI resource on the English database page."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config


WOS_SCI_XPATH = "/html/body/div[4]/div[3]/div/div/div[2]/a[6]"


def click_wos_sci(page: Page) -> None:
    locator = page.locator(f"xpath={WOS_SCI_XPATH}")
    locator.wait_for(state="visible")
    locator.click()
    page.wait_for_load_state("domcontentloaded")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        click_wos_sci(page)
        input("Web of Science/SCI was clicked. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
