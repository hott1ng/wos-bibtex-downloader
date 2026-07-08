"""Select a working Web of Science entry and persist it for reuse."""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from wos_download import (
    choose_resource,
    load_config,
    load_state,
    login,
    read_sms_text,
    select_working_entry,
)


def main() -> None:
    config = load_config()
    sms_text = read_sms_text()
    state = load_state()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        login(page, config)
        choose_resource(page)
        wos_page = select_working_entry(
            context=context,
            page=page,
            state=state,
            sms_text=sms_text,
            manual_captcha=False,
        )
        print(f"Selected entry: {state.get('last_working_entry')}")
        print(f"WOS page URL: {wos_page.url}")
        input("WOS entry is open. Press Enter to close the browser...")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
