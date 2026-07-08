"""Recognize and fill the 90tsg login captcha with ddddocr."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

import config
import ddddocr


CAPTCHA_IMAGE_SELECTOR = 'img[src*="ShowKey"]'
CAPTCHA_INPUT_SELECTORS = [
    'input[name*="captcha" i]',
    'input[name*="verify" i]',
    'input[name*="code" i]',
    'input[id*="captcha" i]',
    'input[id*="verify" i]',
    'input[id*="code" i]',
    'input[placeholder*="验证码"]',
]


def fill_captcha(page: Page) -> str:
    captcha_image = page.locator(CAPTCHA_IMAGE_SELECTOR).first
    captcha_image.wait_for(state="visible")
    code = ddddocr.DdddOcr(show_ad=False).classification(captcha_image.screenshot()).strip()
    if not code:
        raise RuntimeError("Captcha OCR returned an empty result.")

    for selector in CAPTCHA_INPUT_SELECTORS:
        locator = page.locator(selector).first
        if locator.count():
            locator.fill(code)
            return code
    raise RuntimeError("Captcha input was not found.")


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url, wait_until="domcontentloaded")
        code = fill_captcha(page)
        print(f"Filled captcha with OCR result length: {len(code)}")
        input("Captcha is filled. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
