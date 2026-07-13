"""Automate authorized Web of Science/SCI BibTeX downloads.

The script follows the project skill in
`.cursor/skills/wos-bibtex-downloader/SKILL.md`.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import re
import struct
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import ddddocr
import requests
from playwright.sync_api import (
    BrowserContext,
    Download,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from bingtop_captcha import BingtopCaptchaError, extract_bingtop_credentials, solve_click_coordinates


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.py"
SMS_PATH = BASE_DIR / "sms.md"
DATE_CSV_PATH = BASE_DIR / "date.csv"
STATE_PATH = BASE_DIR / ".wos_state.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DEBUG_DIR = BASE_DIR / "debug"
ACCOUNT_DAILY_LOG_PATH = BASE_DIR / "account_daily_log.csv"

ENGLISH_DATABASE_XPATH = "/html/body/div[4]/div[2]/div/ul/li[2]/a"
ADVANCED_SEARCH_XPATH = (
    "/html/body/app-wos/main/div/app-header/div[1]/header/div[2]/div[2]"
    "/div/nav/div[2]/div/div/a[2]/span[2]/span"
)

BINGTOP_ENTRY_CAPTCHA_TYPES = {
    "Web of Science【njust推荐】": 13152,
}
ENTRY_NAMES = list(BINGTOP_ENTRY_CAPTCHA_TYPES)
BATCH_SIZE = 100
DEFAULT_TIMEOUT_MS = 30_000
RETRY_DELAY_S = 2
ACTION_DELAY_MS = 2_000
BROWSER_WINDOW_WIDTH = 1_400
BROWSER_WINDOW_HEIGHT = 1_000
CLICK_CAPTCHA_MAX_ATTEMPTS = 3
CLICK_CAPTCHA_SUCCESS_WAIT_MS = 15_000
RECAPTCHA_INITIAL_DOWNLOAD_WAIT_MS = 35_000
BINGTOP_RECAPTCHA_CAPTCHA_TYPE = 2303

USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[name="userName"]',
    'input[name="account"]',
    'input[id*="user" i]',
    'input[placeholder*="账号"]',
    'input[placeholder*="用户名"]',
    'input[type="text"]',
]
PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[id*="pass" i]',
    'input[placeholder*="密码"]',
    'input[type="password"]',
]
NUMERIC_CAPTCHA_INPUT_SELECTORS = [
    'input[name*="captcha" i]',
    'input[name*="verify" i]',
    'input[name*="code" i]',
    'input[name*="key" i]',
    'input[id*="captcha" i]',
    'input[id*="verify" i]',
    'input[id*="code" i]',
    'input[id*="key" i]',
    'input[placeholder*="验证码"]',
]
NUMERIC_CAPTCHA_IMAGE_SELECTORS = [
    'img[src*="ShowKey" i]',
    'img[src*="CheckCode" i]',
    'img[src*="captcha" i]',
    'img[src*="verify" i]',
    'img[src*="code" i]',
    'img[src*="key" i]',
    'img[id*="captcha" i]',
    'img[id*="verify" i]',
    'img[id*="code" i]',
    'img[id*="key" i]',
]
CLICK_CAPTCHA_SELECTORS = [
    ".clicaptcha-img",
    "#image",
    'canvas',
    'img[src*="captcha" i]',
    'img[src*="verify" i]',
    'div[class*="captcha" i] img',
]


@dataclass
class Config:
    url: str
    username: str
    password: str
    label: str = ""


@dataclass
class BrowserSession:
    browser: Any
    context: BrowserContext
    page: Page
    ads_profile_id: str = ""
    ads_delete_on_close: bool = False


@dataclass
class DateTask:
    date: str
    status: str
    downloaded: int
    total: int

    @property
    def progress(self) -> str:
        return f"{self.downloaded}/{self.total}"

    @property
    def is_finished(self) -> bool:
        return self.status == "done"


class WorkflowError(RuntimeError):
    """Raised when the site cannot be driven to the expected state."""


class AccountDownloadLimit(WorkflowError):
    """Raised when export retries suggest the current account is rate-limited."""


class AccountUnavailable(WorkflowError):
    """Raised when the current account cannot be used and the next one should be tried."""


class ExportBatchSkipped(WorkflowError):
    """Raised when the current export batch should be skipped and progress advanced."""


def retry_step(label: str, attempts: int, func):
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except ExportBatchSkipped:
            raise
        except Exception as exc:
            last_error = exc
            logging.warning("%s failed on attempt %d/%d: %s", label, attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(RETRY_DELAY_S)
    raise WorkflowError(f"{label} failed after {attempts} attempts") from last_error


def safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "debug"


def save_debug_artifacts(page: Page, label: str, error: Optional[BaseException] = None) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = DEBUG_DIR / f"{stamp}_{safe_name(label)}"
    try:
        try:
            screenshot = page.locator("body").screenshot(timeout=10_000)
            prefix.with_suffix(".png").write_bytes(screenshot)
        except Exception:
            page.screenshot(path=prefix.with_suffix(".png"), full_page=True, timeout=10_000)
    except Exception as exc:
        logging.warning("Failed to save debug screenshot for %s: %s", label, exc)
    try:
        text = page.locator("body").inner_text(timeout=5_000)
        details = [
            f"url: {page.url}",
            f"title: {page.title()}",
            f"error: {error!r}" if error else "",
            "",
            text[:10_000],
        ]
        prefix.with_suffix(".txt").write_text("\n".join(details), encoding="utf-8")
    except Exception as exc:
        logging.warning("Failed to save debug text for %s: %s", label, exc)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Requests may include captcha API keys in query strings when debug logging is enabled.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def build_config(module: Any, raw_account: Any = None, index: int = 0) -> Config:
    url = str(getattr(module, "url", "")).strip()
    if not url:
        raise WorkflowError("config.py is missing: url")

    if raw_account is None:
        missing = [name for name in ("username", "password") if not hasattr(module, name)]
        if missing:
            raise WorkflowError(f"config.py is missing: {', '.join(missing)}")
        username = str(module.username).strip()
        password = str(module.password)
        label = username
    elif isinstance(raw_account, dict):
        username = str(raw_account.get("username", "")).strip()
        password = str(raw_account.get("password", ""))
        label = str(raw_account.get("label") or username or f"account-{index + 1}")
    elif isinstance(raw_account, (list, tuple)) and len(raw_account) >= 2:
        username = str(raw_account[0]).strip()
        password = str(raw_account[1])
        label = str(raw_account[2]) if len(raw_account) >= 3 else username
    else:
        raise WorkflowError(f"Unsupported account config at index {index}.")

    if not username or not password:
        raise WorkflowError(f"Account config at index {index} is missing username or password.")
    return Config(url=url, username=username, password=password, label=label)


def load_config_module(path: Path = CONFIG_PATH) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    spec = importlib.util.spec_from_file_location("wos_config", path)
    if spec is None or spec.loader is None:
        raise WorkflowError(f"Cannot load config file: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_configs(path: Path = CONFIG_PATH) -> list[Config]:
    module = load_config_module(path)
    raw_accounts = getattr(module, "accounts", None)
    if raw_accounts:
        configs = [build_config(module, raw_account, index) for index, raw_account in enumerate(raw_accounts)]
        if configs:
            return configs
    return [build_config(module)]


def load_config(path: Path = CONFIG_PATH) -> Config:
    return load_configs(path)[0]


def load_ads_api_key(path: Path = CONFIG_PATH) -> str:
    module = load_config_module(path)
    api_key = str(getattr(module, "ads_api_key", "")).strip()
    if not api_key:
        raise WorkflowError("config.py is missing: ads_api_key")
    return api_key


def adspower_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def clear_adspower_profile_cache(profile_id: str, api_key: str, port: int) -> None:
    response = requests.post(
        f"http://127.0.0.1:{port}/api/v2/browser-profile/delete-cache",
        headers=adspower_headers(api_key),
        json={
            "profile_id": [profile_id],
            "type": ["cookie", "local_storage", "indexeddb", "extension_cache", "history"],
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise WorkflowError(f"AdsPower failed to clear profile cache for {profile_id}: {payload.get('msg')}")
    logging.info("Cleared AdsPower cookies and local storage for profile %s.", profile_id)


def create_adspower_profile(api_key: str, port: int) -> str:
    profile_name = f"wos-temp-{time.strftime('%Y%m%d-%H%M%S')}"
    response = requests.post(
        f"http://127.0.0.1:{port}/api/v1/user/create",
        headers=adspower_headers(api_key),
        json={
            "group_id": "0",
            "name": profile_name,
            "username": profile_name,
            "platform": "90tsg.com",
            "domain_name": "90tsg.com",
            "user_proxy_config": {"proxy_soft": "no_proxy"},
            "fingerprint_config": {
                "screen_resolution": f"{BROWSER_WINDOW_WIDTH}_{BROWSER_WINDOW_HEIGHT}",
                "language_switch": "0",
                "language": ["en-US", "en"],
                "page_language_switch": "0",
                "page_language": "en-US",
                "webrtc": "disabled",
                "random_ua": {
                    "ua_system_version": ["Windows 10", "Windows 11"],
                },
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise WorkflowError(f"AdsPower failed to create temporary profile: {payload.get('msg')}")

    profile_id = payload.get("data", {}).get("id")
    if not profile_id:
        raise WorkflowError("AdsPower did not return a profile id for the temporary profile.")
    logging.info("Created fresh AdsPower profile %s (%s).", profile_id, profile_name)
    return str(profile_id)


def delete_adspower_profile(profile_id: str, api_key: str, port: int) -> None:
    stop_adspower_profile(profile_id, api_key, port, warn_on_failure=False)
    time.sleep(2)
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                f"http://127.0.0.1:{port}/api/v1/user/delete",
                headers=adspower_headers(api_key),
                json={"user_ids": [profile_id]},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 0:
                logging.info("Deleted temporary AdsPower profile %s.", profile_id)
                return
            last_error = WorkflowError(str(payload.get("msg")))
        except Exception as exc:
            last_error = exc

        if attempt < 3:
            time.sleep(3)

    logging.warning("Failed to delete temporary AdsPower profile %s: %s", profile_id, last_error)


def start_adspower_profile(
    profile_id: str,
    api_key: str,
    port: int,
    headless: bool,
    reset_before_start: bool = True,
) -> str:
    if reset_before_start:
        logging.info("Resetting AdsPower profile %s before run.", profile_id)
        stop_adspower_profile(profile_id, api_key, port, warn_on_failure=False)
        clear_adspower_profile_cache(profile_id, api_key, port)

    response = requests.get(
        f"http://127.0.0.1:{port}/api/v1/browser/start",
        headers=adspower_headers(api_key),
        params={
            "user_id": profile_id,
            "ip_tab": 0,
            "last_opened_tabs": 0,
            "device_scale": 1,
            "delete_cache": 1,
            "launch_args": json.dumps([
                f"--window-size={BROWSER_WINDOW_WIDTH},{BROWSER_WINDOW_HEIGHT}",
                "--window-position=0,0",
                "--force-device-scale-factor=1",
            ]),
            "headless": 1 if headless else 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise WorkflowError(f"AdsPower failed to start profile {profile_id}: {payload.get('msg')}")

    ws_endpoint = payload.get("data", {}).get("ws", {}).get("puppeteer")
    if not ws_endpoint:
        raise WorkflowError(f"AdsPower did not return a Playwright CDP endpoint for profile {profile_id}.")
    logging.info("Started AdsPower profile %s via local API port %s.", profile_id, port)
    return str(ws_endpoint)


def stop_adspower_profile(profile_id: str, api_key: str, port: int, warn_on_failure: bool = True) -> None:
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/api/v1/browser/stop",
            headers=adspower_headers(api_key),
            params={"user_id": profile_id},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            message = "AdsPower failed to stop profile %s: %s"
            if warn_on_failure:
                logging.warning(message, profile_id, payload.get("msg"))
            else:
                logging.info(message, profile_id, payload.get("msg"))
    except Exception as exc:
        if warn_on_failure:
            logging.warning("Failed to stop AdsPower profile %s: %s", profile_id, exc)
        else:
            logging.info("AdsPower profile %s was not running before reset: %s", profile_id, exc)


def resize_browser_window(browser: Any, page: Page) -> None:
    try:
        cdp_session = page.context.new_cdp_session(page)
        window = cdp_session.send("Browser.getWindowForTarget")
        cdp_session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {
                    "left": 0,
                    "top": 0,
                    "width": BROWSER_WINDOW_WIDTH,
                    "height": BROWSER_WINDOW_HEIGHT,
                    "windowState": "normal",
                },
            },
        )
        page.set_viewport_size({"width": BROWSER_WINDOW_WIDTH, "height": BROWSER_WINDOW_HEIGHT - 120})
        logging.info("Resized browser window to %dx%d.", BROWSER_WINDOW_WIDTH, BROWSER_WINDOW_HEIGHT)
    except Exception as exc:
        logging.warning("Failed to resize browser window: %s", exc)


def prepare_initial_page_state(context: BrowserContext) -> Page:
    pages = list(context.pages)
    page = pages[0] if pages else context.new_page()

    for extra_page in pages[1:]:
        try:
            extra_page.close()
        except Exception as exc:
            logging.debug("Failed to close extra startup page: %s", exc)

    try:
        page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
    except Exception as exc:
        raise WorkflowError("Browser did not reach initial about:blank state before run.") from exc

    if page.url != "about:blank":
        raise WorkflowError(f"Browser initial state check failed; current URL is {page.url!r}.")
    logging.info("Browser initial state verified: one blank page.")
    return page


def open_browser_session(playwright: Any, args: argparse.Namespace) -> BrowserSession:
    if args.ads_profile_id or args.ads_fresh_profile:
        api_key = load_ads_api_key()
        profile_id = args.ads_profile_id
        delete_on_close = False
        if args.ads_fresh_profile:
            profile_id = create_adspower_profile(api_key, args.ads_port)
            delete_on_close = True

        ws_endpoint = start_adspower_profile(
            profile_id,
            api_key,
            args.ads_port,
            args.headless,
            reset_before_start=not args.ads_no_reset and not args.ads_fresh_profile,
        )
        browser = playwright.chromium.connect_over_cdp(ws_endpoint)
        if not browser.contexts:
            raise WorkflowError(f"AdsPower profile {profile_id} opened without a browser context.")
        context = browser.contexts[0]
        page = prepare_initial_page_state(context)
        resize_browser_window(browser, page)
        return BrowserSession(
            browser=browser,
            context=context,
            page=page,
            ads_profile_id=profile_id,
            ads_delete_on_close=delete_on_close,
        )

    browser = playwright.chromium.launch(headless=args.headless)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    return BrowserSession(browser=browser, context=context, page=page)


def close_browser_session(session: BrowserSession, args: argparse.Namespace) -> None:
    if session.ads_profile_id:
        try:
            session.browser.close()
        finally:
            if session.ads_delete_on_close and not args.ads_keep_open:
                delete_adspower_profile(session.ads_profile_id, load_ads_api_key(), args.ads_port)
            elif not args.ads_keep_open:
                stop_adspower_profile(session.ads_profile_id, load_ads_api_key(), args.ads_port)
        return

    session.context.close()
    session.browser.close()


def read_sms_text(path: Path = SMS_PATH) -> str:
    if not path.exists():
        logging.warning("sms.md not found; click captcha solving may require manual mode.")
        return ""
    return path.read_text(encoding="utf-8")


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("State file is invalid JSON; ignoring it.")
        return {}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_progress(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", raw or "")
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def read_date_tasks(path: Path = DATE_CSV_PATH) -> list[DateTask]:
    if not path.exists():
        path.write_text("date,status,progress\n", encoding="utf-8")
        raise WorkflowError(
            f"Created empty {path.name}. Add rows like 2021-01-01,pending,0/0 and rerun."
        )

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "status", "progress"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise WorkflowError("date.csv must contain columns: date,status,progress")

        tasks: list[DateTask] = []
        for row in reader:
            date = (row.get("date") or "").strip()
            if not date:
                continue
            downloaded, total = parse_progress(row.get("progress", "0/0"))
            status = (row.get("status") or "pending").strip() or "pending"
            tasks.append(DateTask(date=date, status=status, downloaded=downloaded, total=total))
        return tasks


def write_date_tasks(tasks: Iterable[DateTask], path: Path = DATE_CSV_PATH) -> None:
    tmp_path = path.with_suffix(".csv.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "status", "progress"])
        writer.writeheader()
        for task in tasks:
            writer.writerow({"date": task.date, "status": task.status, "progress": task.progress})
    tmp_path.replace(path)


def update_account_daily_log(
    account: str,
    result: str,
    batches: int,
    records: int,
    message: str = "",
    path: Path = ACCOUNT_DAILY_LOG_PATH,
    count_run: bool = True,
) -> None:
    fields = [
        "date",
        "account",
        "run_count",
        "successful_batches",
        "downloaded_records",
        "login_failures",
        "account_limits",
        "workflow_errors",
        "last_result",
        "last_message",
        "updated_at",
    ]
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    target: Optional[dict[str, str]] = None
    for row in rows:
        if row.get("date") == today and row.get("account") == account:
            target = row
            break
    if target is None:
        target = {field: "0" for field in fields}
        target["date"] = today
        target["account"] = account
        rows.append(target)

    def add_int(field: str, amount: int) -> None:
        current = int(target.get(field) or 0)
        target[field] = str(current + max(amount, 0))

    if count_run:
        add_int("run_count", 1)
    add_int("successful_batches", batches)
    add_int("downloaded_records", records)
    if result == "login_failed":
        add_int("login_failures", 1)
    elif result == "account_limit":
        add_int("account_limits", 1)
    elif result == "workflow_error":
        add_int("workflow_errors", 1)

    target["last_result"] = result
    target["last_message"] = message[:300]
    target["updated_at"] = now

    tmp_path = path.with_suffix(".csv.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(path)


def next_task(tasks: list[DateTask]) -> Optional[DateTask]:
    for task in tasks:
        if not task.is_finished:
            return task
    return None


def first_visible(page: Page, selectors: Iterable[str], timeout_ms: int = 5_000) -> Locator:
    last_error: Optional[Exception] = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise WorkflowError(f"No visible element found for selectors: {list(selectors)}") from last_error


def wait_after_action(page: Page, label: str = "action") -> None:
    page.wait_for_timeout(ACTION_DELAY_MS)
    logging.debug("Waited %.1fs after %s.", ACTION_DELAY_MS / 1000, label)


def click_xpath(page: Page, xpath: str, label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    locator = page.locator(f"xpath={xpath}")
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.click()
    logging.info("Clicked %s", label)
    wait_after_action(page, label)


def click_by_text(page: Page, texts: Iterable[str], label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    last_error: Optional[Exception] = None
    for text in texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click()
            logging.info("Clicked %s by text: %s", label, text)
            wait_after_action(page, label)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise WorkflowError(f"Cannot click {label}") from last_error


def click_accept_all_if_present(page: Page, timeout_ms: int = 8_000) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "[id*='onetrust'][id*='accept' i]",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept all cookies')",
        "button:has-text('Accept All Cookies')",
        "text=/^Accept all$/i",
        "text=/^Accept all cookies$/i",
        "button[aria-label*='Accept all' i]",
        "button[title*='Accept all' i]",
    ]
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=500):
                    locator.click(timeout=2_000, force=True)
                    logging.info("Clicked Accept All cookie button.")
                    wait_after_action(page, "Accept All cookie button")
                    return
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const target = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')]
                      .find((el) => /^(Accept all|Accept all cookies)$/i.test(normalize(
                        el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title')
                      )));
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
            if clicked:
                logging.info("Clicked Accept All cookie button by DOM fallback.")
                wait_after_action(page, "Accept All cookie button")
                return
        except Exception:
            pass
        page.wait_for_timeout(500)
    logging.info("Accept All cookie button was not present; continuing.")


def dismiss_wos_overlays(page: Page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "text=/^Accept all$/i",
        "button[aria-label*='Accept all' i]",
        "button[aria-label*='Close this tour' i]",
        "button:has-text('×')",
        "button:has-text('Close')",
    ]
    for _ in range(3):
        clicked = False
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=800):
                    locator.click(timeout=2_000, force=True)
                    wait_after_action(page, "dismiss overlay")
                    clicked = True
            except Exception:
                continue
        if not clicked:
            break
    try:
        page.evaluate(
            """() => {
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const targets = [...document.querySelectorAll('button, a, [role="button"]')]
                  .filter((el) => /^Accept all$/i.test(normalize(el.innerText || el.textContent)));
                for (const target of targets) target.click();
            }"""
        )
        wait_after_action(page, "dismiss accept overlay")
    except Exception:
        pass


def solve_numeric_captcha(page: Page, ocr: ddddocr.DdddOcr) -> Optional[str]:
    try:
        image = first_visible(page, NUMERIC_CAPTCHA_IMAGE_SELECTORS, timeout_ms=3_000)
        image_bytes = image.screenshot()
        code = ocr.classification(image_bytes).strip()
        if not code:
            logging.warning("Numeric captcha OCR returned empty result.")
            return None

        input_box = first_visible(page, NUMERIC_CAPTCHA_INPUT_SELECTORS, timeout_ms=3_000)
        input_box.fill(code)
        logging.info("Filled numeric captcha with OCR result length %d", len(code))
        wait_after_action(page, "numeric captcha fill")
        return code
    except WorkflowError:
        logging.info("Numeric captcha elements were not detected.")
        return None


def is_logged_in(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        return False
    if "亲爱的" in text and "退出" in text:
        return True
    zhixian_resource_page = "zhixianlib.com/e/action/ListInfo" in page.url
    return zhixian_resource_page and "英文数据库" in text and "Web of Science【njust推荐】" in text


def confirm_zhixian_login_by_resource_page(page: Page) -> bool:
    if "zhixianlib.com" not in page.url:
        return False
    try:
        page.goto(
            "https://www.zhixianlib.com/e/action/ListInfo/?classid=63",
            wait_until="domcontentloaded",
            timeout=DEFAULT_TIMEOUT_MS,
        )
        text = page.locator("body").inner_text(timeout=5_000)
        return "英文数据库" in text and "Web of Science【njust推荐】" in text
    except Exception:
        return False


def is_member_login_page(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        return False
    return "会员登录" in text and "用户名称" in text and "您的密码" in text


def login_current_page(page: Page, config: Config, label: str = "current page login") -> None:
    ocr = ddddocr.DdddOcr(show_ad=False)

    def submit_member_login_form() -> None:
        submitted = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const normalize = (value) => (value || '').replace(/\\s+/g, '').trim();
                const forms = [...document.querySelectorAll('form')];
                const loginForm = forms.find((form) => {
                    const text = form.innerText || form.textContent || '';
                    return /用户名称|您的密码|验证码/.test(text) || form.querySelector('input[type="password"]');
                });
                if (!loginForm) return false;
                const candidates = [...loginForm.querySelectorAll(
                    'input[type="submit"], input[type="button"], input[type="image"], button, [role="button"]'
                )].filter(visible);
                const button = candidates.find((el) => /登录|Login|Sign in|立即登录/i.test(normalize(
                    el.value || el.innerText || el.textContent || el.getAttribute('alt') || el.getAttribute('title')
                ))) || candidates[candidates.length - 1];
                if (button) {
                    button.click();
                    return true;
                }
                if (typeof loginForm.requestSubmit === 'function') {
                    loginForm.requestSubmit();
                    return true;
                }
                loginForm.submit();
                return true;
            }"""
        )
        if not submitted:
            raise WorkflowError(f"Cannot submit {label} form")
        wait_after_action(page, f"{label} form submit")

    def attempt_login() -> None:
        if not is_member_login_page(page):
            return
        first_visible(page, USERNAME_SELECTORS).fill(config.username)
        wait_after_action(page, f"{label} username fill")
        first_visible(page, PASSWORD_SELECTORS).fill(config.password)
        wait_after_action(page, f"{label} password fill")
        solve_numeric_captcha(page, ocr)
        try:
            submit = page.locator(
                "form input[type='submit'][value*='登录'], form input[type='button'][value*='登录'], "
                "form button[type='submit']:has-text('登录'), input[type='submit'][value*='登录'], "
                "input[type='button'][value*='登录'], button[type='submit']:has-text('登录')"
            ).first
            submit.click(timeout=8_000)
            wait_after_action(page, f"{label} submit button")
        except Exception:
            try:
                submit_member_login_form()
            except WorkflowError:
                click_by_text(page, ["立即登录", "Login", "Sign in"], f"{label} button", timeout_ms=8_000)

        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        for _ in range(8):
            if not is_member_login_page(page):
                return
            page.wait_for_timeout(1_000)
        captcha = page.locator('img[src*="ShowKey" i], img[src*="CheckCode" i], img[src*="captcha" i]').first
        if captcha.count():
            captcha.click()
            wait_after_action(page, f"{label} numeric captcha refresh")
        raise WorkflowError(f"{label} did not leave member login page.")

    retry_step(label, 4, attempt_login)


def login(page: Page, config: Config) -> None:
    logging.info("Opening login URL: %s", config.url)
    page.goto(config.url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        logging.info("Login page did not reach networkidle; continuing after DOM content loaded.")
    if is_logged_in(page):
        logging.info("Already logged in; current URL: %s", page.url)
        click_accept_all_if_present(page, timeout_ms=12_000)
        return

    ocr = ddddocr.DdddOcr(show_ad=False)

    def attempt_login() -> None:
        if is_logged_in(page):
            return
        first_visible(page, USERNAME_SELECTORS).fill(config.username)
        wait_after_action(page, "username fill")
        first_visible(page, PASSWORD_SELECTORS).fill(config.password)
        wait_after_action(page, "password fill")
        solve_numeric_captcha(page, ocr)

        try:
            click_by_text(page, ["立即登录", "登录", "Login", "Sign in"], "login button", timeout_ms=8_000)
        except WorkflowError:
            page.locator('button[type="submit"], input[type="submit"]').first.click()
            wait_after_action(page, "login submit fallback")

        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        for _ in range(6):
            if is_logged_in(page):
                return
            page.wait_for_timeout(1_000)
        if confirm_zhixian_login_by_resource_page(page):
            logging.info("Login confirmed by zhixian English database page.")
            return
        if not is_logged_in(page):
            captcha = page.locator('img[src*="ShowKey" i]').first
            if captcha.count():
                captcha.click()
                wait_after_action(page, "numeric captcha refresh")
            raise WorkflowError("Login did not reach authenticated resource page.")

    retry_step("login", 4, attempt_login)
    logging.info("Login succeeded; current URL: %s", page.url)
    click_accept_all_if_present(page, timeout_ms=12_000)


def choose_resource(page: Page) -> None:
    try:
        text = page.locator("body").inner_text(timeout=5_000)
        if "zhixianlib.com/e/action/ListInfo" in page.url and "Web of Science【njust推荐】" in text:
            logging.info("English database page is already open.")
            return
    except PlaywrightTimeoutError:
        pass
    try:
        click_by_text(page, ["英文数据库", "English database"], "English database tab", timeout_ms=12_000)
    except WorkflowError:
        click_xpath(page, ENGLISH_DATABASE_XPATH, "English database tab")
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)


def ordered_entries(state: dict[str, Any]) -> list[str]:
    cached = state.get("last_working_entry")
    entries = []
    if isinstance(cached, str) and cached in ENTRY_NAMES:
        entries.append(cached)
    entries.extend(name for name in ENTRY_NAMES if name not in entries)
    return entries


def find_entry_link(page: Page, entry_name: str) -> Locator:
    return page.get_by_text(entry_name, exact=False).first


def is_wos_page_text(text: str) -> bool:
    captcha_text = re.search(r"captcha|验证码|人机验证|请点击|请依次点击", text, re.I)
    wos_text = re.search(
        r"Advanced Search|Web of Science Core Collection|QUERY BUILDER|Document Search|Documents",
        text,
        re.I,
    )
    return bool(wos_text and not captcha_text)


def wait_for_wos_page(page: Page, timeout_ms: int = 45_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        text = page.locator("body").inner_text(timeout=5_000)
        if is_wos_page_text(text):
            return True
        if "captcha" in text.lower() or "验证码" in text:
            return False
        page.wait_for_timeout(1_000)
    return False


def png_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(image_bytes) < 24:
        raise WorkflowError("Captcha screenshot is not a valid PNG image.")
    width, height = struct.unpack(">II", image_bytes[16:24])
    return int(width), int(height)


def solve_click_captcha_if_present(
    page: Page,
    entry_name: str,
    sms_text: str,
    manual_captcha: bool,
    timeout_ms: int = 5_000,
) -> bool:
    body_text = page.locator("body").inner_text(timeout=timeout_ms)
    captcha_hint = any(
        hint in body_text
        for hint in ("验证码", "人机验证", "请点击", "请依次点击")
    ) or "captcha" in body_text.lower()
    candidates = page.locator(", ".join(CLICK_CAPTCHA_SELECTORS))
    if not captcha_hint and candidates.count() == 0:
        return False

    if manual_captcha:
        logging.info("Click captcha detected. Solve it manually in the browser, then press Enter.")
        input("Press Enter after captcha is solved...")
        return True

    captcha_type = BINGTOP_ENTRY_CAPTCHA_TYPES.get(entry_name)
    if captcha_type is None:
        raise WorkflowError(f"Click captcha detected, but entry {entry_name!r} has no Bingtop type configured.")

    credentials = extract_bingtop_credentials(sms_text)
    if credentials is None:
        raise WorkflowError("Click captcha detected, but Bingtop username/password were not found in sms.md.")

    captcha = candidates.first
    captcha.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    image_bytes = captcha.screenshot()
    instruction = extract_click_captcha_instruction(body_text)
    title = instruction if captcha_type == 13152 else ""
    try:
        points = solve_click_coordinates(credentials, image_bytes, captcha_type, title)
    except BingtopCaptchaError as exc:
        raise WorkflowError(f"Bingtop captcha solve failed for {entry_name}: {exc}") from exc
    expected_count = expected_click_count(instruction)
    if expected_count:
        points = points[:expected_count]

    box = captcha.bounding_box()
    if not box:
        raise WorkflowError("Cannot locate captcha image bounding box.")

    image_width, image_height = png_dimensions(image_bytes)
    scale_x = box["width"] / image_width
    scale_y = box["height"] / image_height
    logging.info(
        "Click captcha geometry for %s: screenshot=%dx%d css=%.1fx%.1f scale=%.3f,%.3f.",
        entry_name,
        image_width,
        image_height,
        box["width"],
        box["height"],
        scale_x,
        scale_y,
    )
    for point in points:
        captcha.click(position={"x": point.x * scale_x, "y": point.y * scale_y}, timeout=5_000)
        wait_after_action(page, "click captcha point")
    logging.info("Clicked %d Bingtop captcha coordinates for %s type %s.", len(points), entry_name, captcha_type)
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    wait_after_action(page, "click captcha submit")
    return True


def refresh_click_captcha_page(page: Page) -> None:
    refresh_selectors = [
        "text=/换一张|刷新|看不清|refresh/i",
        ".clicaptcha-refresh",
        "[class*='refresh' i]",
        "[title*='刷新' i]",
        "[title*='refresh' i]",
    ]
    for selector in refresh_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible(timeout=800):
                locator.click(timeout=2_000, force=True)
                wait_after_action(page, "click captcha refresh")
                return
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    except Exception:
        wait_after_action(page, "click captcha page reload fallback")


def solve_click_captcha_until_wos_page(
    page: Page,
    entry_name: str,
    sms_text: str,
    manual_captcha: bool,
) -> bool:
    last_error: Optional[Exception] = None
    for attempt in range(1, CLICK_CAPTCHA_MAX_ATTEMPTS + 1):
        if wait_for_wos_page(page, timeout_ms=3_000):
            return True

        logging.info(
            "Solving click captcha for %s, attempt %d/%d.",
            entry_name,
            attempt,
            CLICK_CAPTCHA_MAX_ATTEMPTS,
        )
        try:
            detected = solve_click_captcha_if_present(page, entry_name, sms_text, manual_captcha)
            if not detected:
                return wait_for_wos_page(page, timeout_ms=CLICK_CAPTCHA_SUCCESS_WAIT_MS)
            if wait_for_wos_page(page, timeout_ms=CLICK_CAPTCHA_SUCCESS_WAIT_MS):
                return True
            last_error = WorkflowError("Click captcha was submitted but WOS page did not load.")
            save_debug_artifacts(page, f"click_captcha_no_jump_{entry_name}_{attempt}", last_error)
        except Exception as exc:
            last_error = exc
            save_debug_artifacts(page, f"click_captcha_failed_{entry_name}_{attempt}", exc)
            logging.warning(
                "Click captcha attempt %d/%d failed for %s: %s",
                attempt,
                CLICK_CAPTCHA_MAX_ATTEMPTS,
                entry_name,
                exc,
            )

        if attempt < CLICK_CAPTCHA_MAX_ATTEMPTS:
            refresh_click_captcha_page(page)

    raise WorkflowError(
        f"Click captcha for {entry_name} failed after {CLICK_CAPTCHA_MAX_ATTEMPTS} attempts."
    ) from last_error


def extract_click_captcha_instruction(body_text: str) -> str:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    for line in lines:
        if "请" in line and ("点击" in line or "依次" in line):
            return line[:120]
    return ""


def expected_click_count(instruction: str) -> int:
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", instruction)
    if quoted:
        chars = re.findall(r"[\u4e00-\u9fff]", "".join(quoted))
        return len(chars) or len(quoted)
    match = re.search(r"[点点击选出]+\s*([\u4e00-\u9fff、,，\s]+)", instruction)
    if match:
        chars = re.findall(r"[\u4e00-\u9fff]", match.group(1))
        return len(chars)
    match = re.search(r"点击\s*([\u4e00-\u9fff])", instruction)
    return 1 if match else 0


def click_entry_and_get_page(
    context: BrowserContext,
    page: Page,
    entry_name: str,
    config: Config,
    sms_text: str,
    manual_captcha: bool,
) -> Page:
    link = find_entry_link(page, entry_name)
    link.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    old_pages = set(context.pages)

    logging.info("Trying WOS entry: %s", entry_name)
    try:
        with context.expect_page(timeout=5_000) as page_info:
            link.click()
        active_page = page_info.value
    except PlaywrightTimeoutError:
        link.click()
        active_page = page
    wait_after_action(page, f"WOS entry {entry_name}")
    page.wait_for_timeout(5_000)

    new_pages = [candidate for candidate in context.pages if candidate not in old_pages]
    active_page = new_pages[-1] if new_pages else active_page
    active_page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    for _ in range(12):
        if is_member_login_page(active_page):
            logging.info("WOS entry opened member login page; logging in again for %s.", entry_name)
            login_current_page(active_page, config, f"{entry_name} member login")
            active_page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            break
        try:
            body_text = active_page.locator("body").inner_text(timeout=2_000)
        except Exception:
            body_text = ""
        if is_wos_page_text(body_text) or "webofscience" in active_page.url.lower():
            break
        if active_page.locator(", ".join(CLICK_CAPTCHA_SELECTORS)).count():
            break
        active_page.wait_for_timeout(1_000)
    if solve_click_captcha_until_wos_page(active_page, entry_name, sms_text, manual_captcha):
        return active_page
    raise WorkflowError(f"Entry did not reach WOS page: {entry_name}")


def select_working_entry(
    context: BrowserContext,
    page: Page,
    state: dict[str, Any],
    config: Config,
    sms_text: str,
    manual_captcha: bool,
) -> Page:
    last_error: Optional[Exception] = None
    for entry_name in ordered_entries(state):
        try:
            wos_page = click_entry_and_get_page(context, page, entry_name, config, sms_text, manual_captcha)
            state["last_working_entry"] = entry_name
            save_state(state)
            logging.info("Selected WOS entry: %s", entry_name)
            click_accept_all_if_present(wos_page, timeout_ms=12_000)
            return wos_page
        except Exception as exc:
            last_error = exc
            logging.warning("Entry failed: %s (%s)", entry_name, exc)
            if page.is_closed():
                page = context.pages[0]
    raise WorkflowError("No WOS entry is currently usable.") from last_error


def open_advanced_search(page: Page) -> None:
    dismiss_wos_overlays(page)
    body_text = page.locator("body").inner_text(timeout=5_000)
    already_open = "advanced-search" in page.url or (
        "QUERY BUILDER" in body_text and "FIELDED SEARCH" in body_text
    )
    if already_open:
        logging.info("Advanced Search is already open; continuing.")
    else:
        try:
            click_xpath(page, ADVANCED_SEARCH_XPATH, "Advanced Search", timeout_ms=12_000)
        except WorkflowError:
            click_by_text(page, ["Advanced Search"], "Advanced Search")
        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    dismiss_wos_overlays(page)
    page.wait_for_selector("text=/Advanced search|FIELDED SEARCH|QUERY BUILDER/i", timeout=DEFAULT_TIMEOUT_MS)
    clicked = page.evaluate(
        """() => {
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const candidates = [
                ...document.querySelectorAll('[role="tab"], .mat-tab-label, .mat-mdc-tab, a, button')
            ];
            const target = candidates.find((el) => /^QUERY BUILDER$/i.test(normalize(el.innerText || el.textContent)));
            if (!target) return false;
            target.click();
            return true;
        }"""
    )
    if clicked:
        wait_after_action(page, "QUERY BUILDER tab")
    if not clicked:
        try:
            page.get_by_text(re.compile(r"^QUERY BUILDER$", re.I)).first.click(timeout=8_000)
            clicked = True
            wait_after_action(page, "QUERY BUILDER tab")
        except Exception as exc:
            save_debug_artifacts(page, "query_builder_tab_not_clicked", exc)
            clicked = page.evaluate(
                """() => {
                    const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const textNode = [...document.querySelectorAll('*')]
                      .find((el) => normalize(el.innerText || el.textContent) === 'QUERY BUILDER');
                    const target = textNode?.closest?.('[role="tab"], a, button') || textNode;
                    if (!target) return false;
                    target.click();
                    return true;
                }"""
            )
            if clicked:
                wait_after_action(page, "QUERY BUILDER tab")
    if not clicked:
        save_debug_artifacts(page, "query_builder_tab_missing")
        raise WorkflowError("QUERY BUILDER tab not found")
    try:
        page.wait_for_selector(
            "text=/Add terms to the query preview|Select search field|All Fields/i",
            timeout=DEFAULT_TIMEOUT_MS,
        )
    except Exception as exc:
        save_debug_artifacts(page, "query_builder_tab_not_clicked", exc)
        raise
    dismiss_wos_overlays(page)
    save_debug_artifacts(page, "after_open_advanced_search")


def solve_blocking_recaptcha_if_present(page: Page, sms_text: str, label: str) -> None:
    for attempt in range(1, 4):
        if not solve_recaptcha_if_present(page, [], f"{label}_{attempt}", sms_text):
            return
        page.wait_for_timeout(3_000)
        if not recaptcha_present(page, []):
            return
    if recaptcha_present(page, []):
        save_debug_artifacts(page, f"{label}_still_blocked_after_bingtop")
        raise WorkflowError(f"Captcha is still blocking the page after Bingtop attempts: {label}")


def click_search_field_dropdown(page: Page) -> None:
    candidates = [
        page.get_by_role("combobox", name=re.compile("Select search field", re.I)).first,
        page.get_by_role("combobox", name=re.compile("All Fields", re.I)).first,
        page.get_by_role("combobox").first,
    ]
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=5_000)
            locator.click(timeout=5_000, force=True)
            wait_after_action(page, "search field dropdown")
            return
        except Exception:
            continue

    clicked = page.evaluate(
        """() => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const candidates = [...document.querySelectorAll(
                '[role="combobox"], mat-select, button, .mat-mdc-select, .mat-select'
            )].filter(visible);
            const target = candidates.find((el) => /All Fields|Select search field/i.test(normalize(
                el.innerText || el.textContent || el.getAttribute('aria-label')
            ))) || candidates[0];
            if (!target) return false;
            target.click();
            return true;
        }"""
    )
    if not clicked:
        save_debug_artifacts(page, "search_field_dropdown_not_found")
        raise WorkflowError("Search field dropdown was not found.")
    wait_after_action(page, "search field dropdown")


def fill_publication_date_query(page: Page, date_value: str) -> None:
    logging.info("Selecting Publication Date search field.")
    click_search_field_dropdown(page)

    option = page.get_by_role("option", name=re.compile(r"^Publication Date$", re.I))
    for _ in range(20):
        try:
            option.click(timeout=1_000)
            wait_after_action(page, "Publication Date option")
            break
        except Exception:
            listbox = page.get_by_role("listbox").first
            if listbox.count():
                listbox.evaluate("(el) => { el.scrollTop += 260; }")
            else:
                page.mouse.wheel(0, 260)
            page.wait_for_timeout(150)
    else:
        save_debug_artifacts(page, "publication_date_option_not_found")
        raise WorkflowError("Publication Date option was not found in All Fields dropdown.")

    page.wait_for_selector("text=Publication Date", timeout=10_000)
    wait_after_action(page, "Publication Date field ready")
    logging.info("Filling Publication Date query for %s.", date_value)
    filled = page.evaluate(
        """(dateValue) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            const dateInputs = [...document.querySelectorAll('input')]
                .filter((el) => visible(el))
                .filter((el) => {
                const label = [
                    el.getAttribute('aria-label'),
                    el.getAttribute('placeholder'),
                    el.value,
                    el.closest('app-search-row')?.innerText,
                    el.closest('.input-adv-search-row')?.innerText,
                ].filter(Boolean).join(' ');
                    return /YYYY-MM-DD|Publication Date/i.test(label);
                })
                .slice(0, 2);
            if (dateInputs.length < 2) return dateInputs.map((el) => el.value);
            for (const target of dateInputs) {
                setter.call(target, dateValue);
                target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: dateValue }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
                target.blur();
            }
            return dateInputs.map((el) => el.value);
        }""",
        date_value,
    )
    wait_after_action(page, "Publication Date fill")
    if filled != [date_value, date_value]:
        save_debug_artifacts(page, "publication_date_range_not_written")
        raise WorkflowError(f"Publication Date range was not written. Current value: {filled!r}")

    add_button = page.get_by_role("button", name=re.compile(r"^Add to query$", re.I)).first
    add_button.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    add_button.click()
    wait_after_action(page, "Add to query")

    preview_value = page.locator('textarea[placeholder*="Enter or edit"], textarea').first.input_value(timeout=5_000)
    if "DOP=" not in preview_value or " to " in preview_value:
        save_debug_artifacts(page, "publication_date_not_added_to_query")
        raise WorkflowError(f"Publication Date was not added to query preview: {preview_value!r}")
    logging.info("Added publication date query through UI: %s", preview_value)


def query_preview_value(page: Page) -> str:
    try:
        value = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const textareas = [...document.querySelectorAll('textarea')].filter(visible);
                const values = textareas
                    .map((el) => el.value || el.textContent || '')
                    .map((value) => value.trim())
                    .filter(Boolean);
                return values.find((value) => /DOP=/.test(value)) || values[0] || '';
            }"""
        )
        return str(value)
    except Exception:
        return ""


def page_contains_query(page: Page, expected: str) -> bool:
    try:
        return expected in page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return False


def is_search_results_for_date(page: Page, date_value: str) -> bool:
    expected = f"DOP=({date_value}/{date_value})"
    try:
        if expected in page.title(timeout=3_000):
            return True
    except Exception:
        pass
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return False
    return expected in body_text and re.search(r"results from Web of Science Core Collection|Search results", body_text, re.I) is not None


def ensure_date_query(page: Page, date_value: str) -> None:
    preview_value = query_preview_value(page)
    expected = f"DOP=({date_value}/{date_value})"
    if is_search_results_for_date(page, date_value):
        logging.info("Already on search results page for %s.", expected)
        return
    if expected in preview_value or page_contains_query(page, expected):
        return

    logging.info("Query preview missing %s; rebuilding date query.", expected)
    fill_publication_date_query(page, date_value)


def click_search_button(page: Page) -> None:
    clicked = page.evaluate(
        """() => {
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const candidates = [
                ...document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]')
            ].filter((el) => {
                const text = normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label'));
                const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                return /^Search$/i.test(text) && !disabled;
            });
            const target = candidates[candidates.length - 1];
            if (!target) return false;
            target.click();
            return true;
        }"""
    )
    if not clicked:
        page.get_by_text(re.compile(r"^Search$", re.I)).last.click(timeout=8_000)
    wait_after_action(page, "Search button")


def wait_for_search_results_with_captcha(page: Page, label: str, date_value: str, sms_text: str) -> None:
    observed_urls: list[str] = []

    def record_captcha_request(request) -> None:
        url = request.url
        if is_recaptcha_url(url):
            observed_urls.append(url)

    page.on("request", record_captcha_request)
    try:
        for attempt in range(1, 4):
            try:
                if is_search_results_for_date(page, date_value):
                    return
                ensure_date_query(page, date_value)
                if is_search_results_for_date(page, date_value):
                    return
                click_search_button(page)
                page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                page.wait_for_selector("text=/results from Web of Science Core Collection/i", timeout=90_000)
                return
            except PlaywrightTimeoutError as exc:
                save_debug_artifacts(page, f"{label}_search_wait_timeout_{attempt}", exc)
                if is_search_results_for_date(page, date_value):
                    logging.info("Search results page loaded for %s after timeout fallback.", date_value)
                    return
                body_text = page.locator("body").inner_text(timeout=10_000)
                verification_error = "request couldn't be verified" in body_text.lower()
                if is_search_results_for_date(page, date_value):
                    logging.info("Search results page confirmed for %s from body text.", date_value)
                    return
                if not (verification_error or recaptcha_present(page, observed_urls)):
                    raise
                if not solve_recaptcha_if_present(page, observed_urls, f"{label}_search_attempt_{attempt}", sms_text):
                    raise
                if is_search_results_for_date(page, date_value):
                    logging.info("Search results page loaded for %s after captcha solve.", date_value)
                    return
                page.wait_for_timeout(RETRY_DELAY_S * 1_000)
        raise WorkflowError(f"Search did not show results after captcha handling: {label}")
    finally:
        try:
            page.remove_listener("request", record_captcha_request)
        except Exception:
            pass


def run_search_and_count(page: Page, date_value: str, sms_text: str) -> int:
    dismiss_wos_overlays(page)
    save_debug_artifacts(page, "before_search_click")
    wait_for_search_results_with_captcha(page, "date_query", date_value, sms_text)
    body_text = page.locator("body").inner_text(timeout=DEFAULT_TIMEOUT_MS)
    count = extract_result_count(body_text)
    logging.info("Search result count: %d", count)
    return count


def extract_result_count(text: str) -> int:
    patterns = [
        r"([\d,]+)\s+results?",
        r"Results?\s*:?\s*([\d,]+)",
        r"共\s*([\d,]+)\s*条",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1).replace(",", ""))
    raise WorkflowError("Cannot determine result count from page text.")


def search_date_and_count(page: Page, date_value: str, sms_text: str) -> int:
    open_advanced_search(page)
    solve_blocking_recaptcha_if_present(page, sms_text, "advanced_search_gate")
    dismiss_wos_overlays(page)
    fill_publication_date_query(page, date_value)
    return run_search_and_count(page, date_value, sms_text)


def is_recaptcha_url(url: str) -> bool:
    normalized = url.lower()
    return (
        "api.hcaptcha.com/getcaptcha" in normalized
        or ("recaptcha" in normalized and ("google.com" in normalized or "recaptcha.net" in normalized))
    )


def is_hcaptcha_url(url: str) -> bool:
    return "api.hcaptcha.com/getcaptcha" in url.lower() or "hcaptcha.com" in url.lower()


def recaptcha_present(page: Page, observed_urls: list[str]) -> bool:
    if observed_urls:
        return True
    try:
        return page.locator(
            (
                'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
                'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], '
                '.g-recaptcha, .h-captcha, [data-sitekey]'
            )
        ).count() > 0
    except Exception:
        return False


def save_recaptcha_observations(observed_urls: list[str], label: str) -> None:
    if not observed_urls:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = DEBUG_DIR / f"{stamp}_{safe_name(label)}_recaptcha_urls.txt"
    unique_urls = list(dict.fromkeys(observed_urls))
    path.write_text("\n".join(unique_urls), encoding="utf-8")


def visible_visual_captcha_locator(page: Page) -> Optional[Locator]:
    selectors = [
        'iframe[src*="hcaptcha.com"][title*="challenge" i]',
        'iframe[src*="newassets.hcaptcha.com"][title*="challenge" i]',
        'iframe[src*="recaptcha"][title*="challenge" i]',
        'iframe[src*="recaptcha/api2/bframe"]',
        'iframe[src*="hcaptcha.com"]',
        'iframe[src*="recaptcha"]',
    ]
    for selector in selectors:
        locators = page.locator(selector)
        try:
            count = locators.count()
        except Exception:
            continue
        for index in range(count):
            locator = locators.nth(index)
            try:
                if not locator.is_visible(timeout=1_000):
                    continue
                box = locator.bounding_box()
                if box and box["width"] >= 250 and box["height"] >= 180:
                    return locator
            except Exception:
                continue

    body = page.locator("body").first
    try:
        if body.is_visible(timeout=1_000):
            return body
    except Exception:
        return None
    return None


def click_captcha_verify_if_present(page: Page) -> bool:
    verify_text = re.compile(r"^(Verify|Submit|Done|验证|提交|完成|确定)$", re.I)
    for frame in page.frames:
        frame_url = frame.url.lower()
        if "hcaptcha" not in frame_url and "recaptcha" not in frame_url:
            continue
        locators = [
            frame.get_by_role("button", name=verify_text).first,
            frame.get_by_text(verify_text).first,
            frame.locator("button:has-text('Verify'), button:has-text('验证'), button:has-text('Submit')").first,
        ]
        for locator in locators:
            try:
                if locator.count() and locator.is_visible(timeout=1_000):
                    locator.click(timeout=5_000, force=True)
                    wait_after_action(page, "captcha verify button")
                    logging.info("Clicked captcha verify button.")
                    return True
            except Exception:
                continue
    return False


def solve_visual_captcha_with_bingtop(page: Page, sms_text: str, label: str) -> bool:
    credentials = extract_bingtop_credentials(sms_text)
    if credentials is None:
        raise WorkflowError("Captcha detected, but Bingtop username/password were not found in sms.md.")

    captcha = visible_visual_captcha_locator(page)
    if captcha is None:
        save_debug_artifacts(page, f"{label}_bingtop_visual_captcha_missing")
        raise WorkflowError("Captcha detected, but no visible visual challenge could be screenshotted.")

    image_bytes = captcha.screenshot(timeout=10_000)
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (DEBUG_DIR / f"{stamp}_{safe_name(label)}_bingtop_input.png").write_bytes(image_bytes)

    try:
        points = solve_click_coordinates(credentials, image_bytes, BINGTOP_RECAPTCHA_CAPTCHA_TYPE)
    except BingtopCaptchaError as exc:
        save_debug_artifacts(page, f"{label}_bingtop_solve_failed", exc)
        raise WorkflowError(f"Bingtop captcha solve failed: {exc}") from exc

    box = captcha.bounding_box()
    if not box:
        raise WorkflowError("Cannot locate visual captcha bounding box.")

    image_width, image_height = png_dimensions(image_bytes)
    scale_x = box["width"] / image_width
    scale_y = box["height"] / image_height
    logging.info(
        "Bingtop visual captcha geometry for %s: screenshot=%dx%d css=%.1fx%.1f scale=%.3f,%.3f.",
        label,
        image_width,
        image_height,
        box["width"],
        box["height"],
        scale_x,
        scale_y,
    )
    for point in points:
        page.mouse.click(box["x"] + point.x * scale_x, box["y"] + point.y * scale_y)
        wait_after_action(page, "Bingtop visual captcha point")

    click_captcha_verify_if_present(page)
    logging.info(
        "Clicked %d Bingtop visual captcha coordinates for %s type %s.",
        len(points),
        label,
        BINGTOP_RECAPTCHA_CAPTCHA_TYPE,
    )
    return True


def solve_recaptcha_if_present(page: Page, observed_urls: list[str], label: str, sms_text: str) -> bool:
    if not recaptcha_present(page, observed_urls):
        return False

    save_recaptcha_observations(observed_urls, label)
    is_hcaptcha = any(is_hcaptcha_url(url) for url in observed_urls)
    if not is_hcaptcha:
        try:
            is_hcaptcha = page.locator(
                'iframe[src*="hcaptcha"], textarea[name="h-captcha-response"], .h-captcha'
            ).count() > 0
        except Exception:
            is_hcaptcha = False
    captcha_name = "hCaptcha" if is_hcaptcha else "reCAPTCHA v2"
    logging.info(
        "%s detected for %s; solving with Bingtop screenshot captcha type %s.",
        captcha_name,
        label,
        BINGTOP_RECAPTCHA_CAPTCHA_TYPE,
    )
    return solve_visual_captcha_with_bingtop(page, sms_text, label)


def click_export_button_for_download(page: Page) -> None:
    page.get_by_role("button", name=re.compile(r"^Export$", re.I)).first.click()
    wait_after_action(page, "confirm export download")


def click_try_again_if_present(page: Page, timeout_ms: int = 2_000) -> bool:
    selectors = [
        page.get_by_role("button", name=re.compile(r"^Try again$", re.I)).first,
        page.get_by_text(re.compile(r"^Try again$", re.I)).first,
        page.locator("button:has-text('Try again')").first,
    ]
    for locator in selectors:
        try:
            if locator.count() and locator.is_visible(timeout=timeout_ms):
                locator.click(timeout=5_000, force=True)
                wait_after_action(page, "Try again")
                logging.info("Clicked Try again dialog button.")
                return True
        except Exception:
            continue
    return False


def close_export_dialog_if_open(page: Page) -> None:
    try:
        if page.get_by_text("Export Records to BibTeX File", exact=False).first.is_visible(timeout=1_000):
            page.get_by_role("button", name=re.compile(r"^(Cancel|Close)$", re.I)).first.click(timeout=3_000)
            wait_after_action(page, "close export dialog")
    except Exception:
        pass


def export_too_large_prompt_present(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return False
    normalized = " ".join(body_text.split()).lower()
    return (
        "this export took longer than expected" in normalized
        and "please select fewer records" in normalized
    )


def raise_if_export_too_large_prompt(page: Page, label: str) -> None:
    if not export_too_large_prompt_present(page):
        return
    save_debug_artifacts(page, f"{label}_export_too_large_prompt")
    click_try_again_if_present(page, timeout_ms=1_000)
    close_export_dialog_if_open(page)
    raise ExportBatchSkipped(
        "Export took longer than expected; skipping this batch and continuing."
    )


def wait_for_export_download_with_recaptcha(page: Page, label: str, sms_text: str) -> Download:
    observed_urls: list[str] = []

    def record_recaptcha_request(request) -> None:
        url = request.url
        if is_recaptcha_url(url):
            observed_urls.append(url)

    page.on("request", record_recaptcha_request)
    try:
        for attempt in range(1, 4):
            try:
                with page.expect_download(timeout=RECAPTCHA_INITIAL_DOWNLOAD_WAIT_MS) as download_info:
                    click_export_button_for_download(page)
                return download_info.value
            except PlaywrightTimeoutError as exc:
                save_debug_artifacts(page, f"{label}_download_wait_timeout_{attempt}", exc)
                raise_if_export_too_large_prompt(page, label)
                click_try_again_if_present(page)
                if not solve_recaptcha_if_present(page, observed_urls, f"{label}_attempt_{attempt}", sms_text):
                    raise
                try:
                    with page.expect_download(timeout=180_000) as download_info:
                        click_try_again_if_present(page)
                        click_export_button_for_download(page)
                    return download_info.value
                except PlaywrightTimeoutError as second_exc:
                    save_debug_artifacts(page, f"{label}_after_recaptcha_timeout_{attempt}", second_exc)
                    raise_if_export_too_large_prompt(page, label)
                    click_try_again_if_present(page)
                    if attempt >= 3:
                        raise
                    close_export_dialog_if_open(page)
                    page.wait_for_timeout(RETRY_DELAY_S * 1_000)
        raise WorkflowError(f"Export did not start a download after reCAPTCHA handling: {label}")
    finally:
        try:
            page.remove_listener("request", record_recaptcha_request)
        except Exception:
            pass


def click_export_menu(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"^Export$", re.I)).last.click(timeout=8_000)
        wait_after_action(page, "Export menu")
        return
    except Exception as exc:
        save_debug_artifacts(page, "export_menu_role_click_failed", exc)

    clicked = page.evaluate(
        """() => {
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const candidates = [...document.querySelectorAll('button, [role="button"], a')]
              .filter(visible)
              .filter((el) => /^Export$/i.test(normalize(el.innerText || el.textContent || el.getAttribute('aria-label'))));
            const target = candidates[candidates.length - 1];
            if (!target) return false;
            target.click();
            return true;
        }"""
    )
    if not clicked:
        save_debug_artifacts(page, "export_menu_not_found")
        raise WorkflowError("Export menu button was not found.")
    wait_after_action(page, "Export menu fallback")


def click_bibtex_menu_item(page: Page) -> None:
    try:
        page.get_by_role("menuitem", name=re.compile(r"^BibTeX$", re.I)).click(timeout=8_000)
        wait_after_action(page, "BibTeX menu item")
        return
    except Exception as exc:
        save_debug_artifacts(page, "bibtex_menuitem_role_click_failed", exc)

    for locator in [
        page.get_by_text(re.compile(r"^BibTeX$", re.I)).first,
        page.locator("button, a, [role='menuitem'], [role='option']").filter(has_text=re.compile(r"^BibTeX$", re.I)).first,
    ]:
        try:
            locator.click(timeout=5_000, force=True)
            wait_after_action(page, "BibTeX menu item fallback")
            return
        except Exception:
            continue

    clicked = page.evaluate(
        """() => {
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const target = [...document.querySelectorAll('button, a, [role="menuitem"], [role="option"], span')]
              .filter(visible)
              .find((el) => /^BibTeX$/i.test(normalize(el.innerText || el.textContent)));
            const clickable = target?.closest?.('button, a, [role="menuitem"], [role="option"]') || target;
            if (!clickable) return false;
            clickable.click();
            return true;
        }"""
    )
    if not clicked:
        save_debug_artifacts(page, "bibtex_menuitem_not_found")
        raise WorkflowError("BibTeX export menu item was not found.")
    wait_after_action(page, "BibTeX menu item DOM fallback")


def export_batch(
    page: Page,
    date_value: str,
    start: int,
    end: int,
    download_dir: Path,
    sms_text: str,
) -> Download:
    logging.info("Exporting %s records %d to %d", date_value, start, end)
    close_export_dialog_if_open(page)
    click_export_menu(page)
    click_bibtex_menu_item(page)
    page.wait_for_selector("text=Export Records to BibTeX File", timeout=DEFAULT_TIMEOUT_MS)

    range_radio = page.locator('input[type="radio"][value="fromRange"]').first
    range_radio.check(force=True)
    wait_after_action(page, "record range radio")

    start_box = page.get_by_label(re.compile("starting record range", re.I))
    end_box = page.get_by_label(re.compile("ending record range", re.I))
    start_box.fill(str(start))
    wait_after_action(page, "starting record range fill")
    end_box.fill(str(end))
    wait_after_action(page, "ending record range fill")

    record_content = page.get_by_role("combobox", name=re.compile("Filter by", re.I)).first
    record_content.click()
    wait_after_action(page, "record content dropdown")
    page.get_by_role("option", name="Full Record and Cited References").click()
    wait_after_action(page, "record content option")

    download_dir.mkdir(exist_ok=True)
    download = wait_for_export_download_with_recaptcha(page, f"export_{start}_{end}", sms_text)
    suggested_name = download.suggested_filename or f"wos_{start}_{end}.bib"
    download_name = f"wos_{safe_name(date_value)}_{start}_{end}_{int(time.time())}_{suggested_name}"
    download.save_as(download_dir / download_name)
    logging.info("Downloaded %s", download_name)
    return download


def process_task(
    page: Page,
    task: DateTask,
    tasks: list[DateTask],
    sms_text: str,
    account_log_account: Optional[str] = None,
    max_batches: Optional[int] = None,
) -> int:
    logging.info("Processing date %s from progress %s", task.date, task.progress)
    total = retry_step(f"search date {task.date}", 3, lambda: search_date_and_count(page, task.date, sms_text))
    task.total = total
    task.status = "downloading"
    write_date_tasks(tasks)

    if total == 0:
        task.downloaded = 0
        task.status = "done"
        write_date_tasks(tasks)
        logging.info("Finished date %s with zero results.", task.date)
        return 0

    start = max(task.downloaded + 1, 1)
    batches_done = 0
    while start <= total:
        end = min(start + BATCH_SIZE - 1, total)
        try:
            retry_step(
                f"export records {start}-{end}",
                3,
                lambda start=start, end=end: export_batch(page, task.date, start, end, DOWNLOAD_DIR, sms_text),
            )
        except ExportBatchSkipped as exc:
            logging.warning("Skipped export batch %s records %d-%d: %s", task.date, start, end, exc)
            task.downloaded = end
            task.total = total
            task.status = "done" if end >= total else "downloading"
            write_date_tasks(tasks)
            if account_log_account:
                try:
                    update_account_daily_log(
                        account=account_log_account,
                        result="running",
                        batches=0,
                        records=0,
                        message=f"Skipped {task.date} records {start}-{end}: export took too long.",
                        count_run=False,
                    )
                except Exception:
                    logging.exception("Failed to update account daily skip log for %s.", account_log_account)
            if max_batches is not None and batches_done >= max_batches:
                logging.info("Stopped after %d batch(es) by --max-batches.", batches_done)
                break
            start = end + 1
            continue
        except Exception as exc:
            save_debug_artifacts(page, f"export_failed_{task.date}_{start}_{end}", exc)
            task.status = "failed"
            task.total = total
            write_date_tasks(tasks)
            raise AccountDownloadLimit(
                f"Export records {start}-{end} failed after 3 attempts; switching account."
            ) from exc
        task.downloaded = end
        task.total = total
        task.status = "done" if end >= total else "downloading"
        write_date_tasks(tasks)
        batches_done += 1
        if account_log_account:
            try:
                update_account_daily_log(
                    account=account_log_account,
                    result="running",
                    batches=1,
                    records=end - start + 1,
                    message=f"Downloaded {task.date} records {start}-{end}.",
                    count_run=False,
                )
            except Exception:
                logging.exception("Failed to update account daily batch log for %s.", account_log_account)
        if max_batches is not None and batches_done >= max_batches:
            logging.info("Stopped after %d batch(es) by --max-batches.", batches_done)
            break
        start = end + 1
    if task.status == "done":
        logging.info("Finished date %s with progress %s", task.date, task.progress)
    return batches_done


def process_available_tasks(
    page: Page,
    tasks: list[DateTask],
    sms_text: str,
    account_log_account: Optional[str] = None,
    max_batches: Optional[int] = None,
) -> int:
    total_batches_done = 0
    while True:
        task = next_task(tasks)
        if task is None:
            logging.info("All dates in date.csv are done.")
            return total_batches_done

        remaining_batches = None
        if max_batches is not None:
            remaining_batches = max_batches - total_batches_done
            if remaining_batches <= 0:
                return total_batches_done

        total_batches_done += process_task(
            page,
            task,
            tasks,
            sms_text,
            account_log_account=account_log_account,
            max_batches=remaining_batches,
        )

        if max_batches is not None and total_batches_done >= max_batches:
            return total_batches_done


def run(args: argparse.Namespace) -> int:
    configs = load_configs()
    sms_text = read_sms_text()
    tasks = read_date_tasks()
    if next_task(tasks) is None:
        logging.info("No unfinished date in date.csv.")
        return 0

    state = load_state()
    with sync_playwright() as playwright:
        total_batches_done = 0
        while True:
            for account_index, config in enumerate(configs, start=1):
                if next_task(tasks) is None:
                    return 0
                if args.max_batches is not None and total_batches_done >= args.max_batches:
                    return 0

                session: Optional[BrowserSession] = None
                account = config.label or config.username
                account_result = "unknown"
                account_message = ""
                try:
                    session = open_browser_session(playwright, args)
                    context = session.context
                    page = session.page
                    logging.info(
                        "Using account %d/%d: %s",
                        account_index,
                        len(configs),
                        account,
                    )
                    login(page, config)
                    choose_resource(page)
                    wos_page = select_working_entry(context, page, state, config, sms_text, args.manual_captcha)
                    remaining_batches = None
                    if args.max_batches is not None:
                        remaining_batches = args.max_batches - total_batches_done
                    total_batches_done += process_available_tasks(
                        wos_page,
                        tasks,
                        sms_text,
                        account_log_account=account,
                        max_batches=remaining_batches,
                    )
                    if args.max_batches is not None and total_batches_done >= args.max_batches:
                        account_result = "max_batches_reached"
                        account_message = f"Stopped after reaching --max-batches; progress {next_task(tasks).progress if next_task(tasks) else 'done'}."
                        return 0
                    if next_task(tasks) is None:
                        account_result = "completed"
                        account_message = "All date tasks are done."
                        return 0
                except (AccountDownloadLimit, AccountUnavailable) as exc:
                    account_result = "account_limit"
                    account_message = str(exc)
                    if session is not None:
                        for index, open_page in enumerate(session.context.pages):
                            try:
                                save_debug_artifacts(open_page, f"account_limit_page_{index}", exc)
                            except Exception:
                                logging.exception("Failed to collect debug artifacts for page %d.", index)
                    logging.warning(
                        "Account %s cannot continue now (%s); switching account.",
                        account,
                        exc,
                    )
                    continue
                except Exception as exc:
                    if str(exc).startswith("login failed after"):
                        account_result = "login_failed"
                        account_message = str(exc)
                        logging.warning(
                            "Account %s login failed after retries; switching account.",
                            account,
                        )
                        continue
                    if "connect_over_cdp" in "".join(traceback.format_exception_only(type(exc), exc)) or (
                        "Timeout 30000ms exceeded" in str(exc) and session is None
                    ):
                        account_result = "account_unavailable"
                        account_message = f"Browser CDP connection failed: {exc}"
                        logging.warning(
                            "Account %s browser startup failed; switching account.",
                            account,
                        )
                        continue
                    account_result = "workflow_error"
                    account_message = str(exc)
                    task = next_task(tasks)
                    if task is not None:
                        task.status = "failed"
                        write_date_tasks(tasks)
                    if session is not None:
                        for index, open_page in enumerate(session.context.pages):
                            try:
                                save_debug_artifacts(open_page, f"workflow_failed_page_{index}", exc)
                            except Exception:
                                logging.exception("Failed to collect debug artifacts for page %d.", index)
                    progress = task.progress if task is not None else "n/a"
                    logging.exception("Workflow failed. Progress preserved as %s.", progress)
                    return 1
                finally:
                    try:
                        update_account_daily_log(
                            account=account,
                            result=account_result,
                            batches=0,
                            records=0,
                            message=account_message,
                        )
                    except Exception:
                        logging.exception("Failed to update account daily log for %s.", account)
                    if session is not None:
                        close_browser_session(session, args)

            task = next_task(tasks)
            if task is None:
                return 0
            logging.warning(
                "No configured account can continue now. Remaining task %s is preserved as %s. "
                "Reloading accounts and continuing with the next account cycle.",
                task.date,
                task.progress,
            )
            configs = load_configs()
            sms_text = read_sms_text()
            tasks = read_date_tasks()
            state = load_state()
            total_batches_done = 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", dest="headless", action="store_false", help="Show browser UI.")
    parser.add_argument("--headless", dest="headless", action="store_true", help="Run headless.")
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="Pause for manual click-captcha solving instead of calling the configured solver.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop after exporting this many record batches. Useful for verification.",
    )
    parser.add_argument(
        "--ads-profile-id",
        default=None,
        help="Run inside an AdsPower browser profile by user_id/profile_id.",
    )
    parser.add_argument(
        "--ads-fresh-profile",
        action="store_true",
        help="Create a fresh temporary AdsPower profile for this run and delete it afterwards.",
    )
    parser.add_argument(
        "--ads-port",
        type=int,
        default=50325,
        help="AdsPower Local API port.",
    )
    parser.add_argument(
        "--ads-keep-open",
        action="store_true",
        help="Leave the AdsPower profile open after the script exits.",
    )
    parser.add_argument(
        "--ads-no-reset",
        action="store_true",
        help="Do not stop/reset the AdsPower profile before connecting. For debugging only.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    parser.set_defaults(headless=False)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
