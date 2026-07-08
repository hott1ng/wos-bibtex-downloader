# Web of Science BibTeX Downloader

This project automates an authorized Web of Science/SCI BibTeX export workflow through 90tsg with Python Playwright.

It can:

- Log in to 90tsg with local credentials.
- Solve the numeric login captcha with `ddddocr`.
- Open the Web of Science/SCI resource entry and handle click captchas with a configured captcha provider.
- Search Web of Science by publication date.
- Export BibTeX records in batches of 500.
- Resume progress from `date.csv`.
- Continue to the next date after the current date is fully downloaded.
- Switch to the next configured account when a batch export fails after 3 retries.

## Safety

Do not commit private credentials or runtime output.

The repository ignores these local files by default:

- `config.py`: 90tsg account credentials.
- `sms.md`: captcha provider API keys.
- `date.csv`: local download progress.
- `.wos_state.json`: cached entry state.
- `downloads/`: exported BibTeX files.
- `debug/`: screenshots and page text collected on failures.

Use the example files as templates.

## Requirements

- Python 3.10+
- Playwright browser dependencies

Install Python packages:

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Create `config.py` from `config.example.py`:

```python
url = "http://www.90tsg.com"
username = "your_username"
password = "your_password"
```

For multiple accounts:

```python
url = "http://www.90tsg.com"

accounts = [
    {"username": "account_1", "password": "password_1", "label": "account-1"},
    {"username": "account_2", "password": "password_2", "label": "account-2"},
]
```

Create `sms.md` from `sms.example.md` and add the captcha provider API key.

Create `date.csv` from `date.example.csv`:

```csv
date,status,progress
2025-01-01,pending,0/0
2025-01-02,pending,0/0
```

## Usage

Run with a visible browser:

```bash
python wos_download.py --headed --verbose
```

Run a limited verification, for example 2 export batches:

```bash
python wos_download.py --headed --max-batches 2 --verbose
```

If click captcha solving should be handled manually:

```bash
python wos_download.py --headed --manual-captcha --verbose
```

## Progress Rules

`date.csv` uses:

- `pending`: not started.
- `downloading`: partially downloaded.
- `done`: all records for that date are downloaded.
- `failed`: failed during a previous run; the next run will retry from saved progress.

Progress format is `<downloaded>/<total>`, for example:

```csv
2025-01-01,downloading,2000/632601
```

Each successful batch updates `date.csv` immediately.

## Main Scripts

- `wos_download.py`: integrated downloader.
- `goto_loginpage.py`, `input_username.py`, `input_password.py`, `solve_numeric_captcha.py`, `click_login_button.py`: recorded login steps.
- `click_english_database.py`, `click_wos_sci.py`, `select_wos_entry.py`, `search_publication_date.py`: recorded navigation/search helpers.

## Notes

Use this automation only where you are authorized to access and export records. Respect provider terms, account limits, and institutional access rules.
