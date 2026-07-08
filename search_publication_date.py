"""Search Web of Science by the next unfinished publication date."""

from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

from wos_download import next_task, read_date_tasks


def fill_publication_date_query(page: Page, date_value: str) -> None:
    page.get_by_text("QUERY BUILDER", exact=True).click()
    preview = page.locator("textarea").first
    preview.wait_for(state="visible")
    preview.fill(f"DOP=({date_value})")
    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle")


def main() -> None:
    tasks = read_date_tasks()
    task = next_task(tasks)
    if task is None:
        raise RuntimeError("No unfinished date in date.csv.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        input("Open Web of Science Advanced Search, then press Enter...")
        fill_publication_date_query(page, task.date)
        input("Search is complete. Press Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    main()
