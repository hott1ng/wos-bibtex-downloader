---
name: wos-bibtex-downloader
description: Automates authorized Web of Science/SCI BibTeX downloads with Playwright. Use when creating, debugging, or running workflows that log into 90tsg, select a Web of Science entry, solve configured captchas, query publication dates, export BibTeX records, and resume date.csv progress.
---

# Web of Science BibTeX Downloader

## Required Files

- `config.py`: defines `url`, `username`, and `password`.
- `sms.md`: stores captcha-service notes and API keys. Read it only at runtime and never print secrets.
- `date.csv`: uses columns `date,status,progress`.
  - Example: `2021-01-01,pending,0/0`
  - `status` values: `pending`, `downloading`, `done`, `failed`
  - `progress` format: `<downloaded>/<total>`, for example `500/889`

## Workflow

1. Load `config.py`, `sms.md`, `date.csv`, and `.wos_state.json` if it exists.
2. Open `config.url` with Python Playwright.
3. Log in with `config.username` and `config.password`.
4. Solve the numeric captcha with `ddddocr`, then click the login button.
5. After navigation, click:
   - English database tab: `/html/body/div[4]/div[2]/div/ul/li[2]/a`
   - Web of Science/SCI: `/html/body/div[4]/div[3]/div/div/div[2]/a[6]`
6. Try the cached WOS entry first. If it fails, traverse entries in this order:
   `wos2(定制)`, `临时`, `wos定制`, `sh`, `wos1`, `wos4`.
7. When a click captcha appears, solve it with the configured captcha provider from `sms.md`.
8. On the WOS page, open Advanced Search:
   `/html/body/app-wos/main/div/app-header/div[1]/header/div[2]/div[2]/div/nav/div[2]/div/div/a[2]/span[2]/span`
9. Open Query Builder, select `Publication Date`, enter the next unfinished date from `date.csv`, add it to the query, and run Search.
10. Read the result count and export all records in BibTeX batches of 500.
11. For each batch:
    - Click Export.
    - Choose BibTeX.
    - Set `Records from` and `to`.
    - Set `Record Content` to `Full Record and Cited References`.
    - Wait until the download is complete.
    - Update `date.csv` progress immediately.
12. Mark the date `done` only after the final batch download succeeds.

## Recovery Rules

- Persist the last working entry in `.wos_state.json`.
- If the cached entry fails for the current account, continue traversing entries and replace the cache when a new entry succeeds.
- If a download batch fails, keep the latest successful `progress` and set `status` to `failed`.
- On the next run, retry `failed` and `downloading` dates from the next missing batch.
- Prefer stable selectors by role/text when available; keep the supplied XPath locators as fallbacks.
- Never log passwords, captcha keys, raw cookies, or downloaded record content.
