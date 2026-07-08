"""Automate authorized Web of Science/SCI BibTeX downloads.

The script follows the project skill in
`.cursor/skills/wos-bibtex-downloader/SKILL.md`.
"""

from __future__ import annotations

import argparse
import ast
import csv
import importlib.util
import json
import logging
import re
import sys
import time
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


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.py"
SMS_PATH = BASE_DIR / "sms.md"
DATE_CSV_PATH = BASE_DIR / "date.csv"
STATE_PATH = BASE_DIR / ".wos_state.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DEBUG_DIR = BASE_DIR / "debug"

ENGLISH_DATABASE_XPATH = "/html/body/div[4]/div[2]/div/ul/li[2]/a"
WOS_SCI_XPATH = "/html/body/div[4]/div[3]/div/div/div[2]/a[6]"
ADVANCED_SEARCH_XPATH = (
    "/html/body/app-wos/main/div/app-header/div[1]/header/div[2]/div[2]"
    "/div/nav/div[2]/div/div/a[2]/span[2]/span"
)

ENTRY_NAMES = ["wos2(定制)", "临时", "wos定制", "sh", "wos1", "wos4"]
BATCH_SIZE = 500
DEFAULT_TIMEOUT_MS = 30_000
RETRY_DELAY_S = 2

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
    'input[id*="captcha" i]',
    'input[id*="verify" i]',
    'input[id*="code" i]',
    'input[placeholder*="验证码"]',
]
NUMERIC_CAPTCHA_IMAGE_SELECTORS = [
    'img[src*="ShowKey" i]',
    'img[src*="captcha" i]',
    'img[src*="verify" i]',
    'img[src*="code" i]',
    'img[id*="captcha" i]',
    'img[id*="verify" i]',
    'img[id*="code" i]',
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


def retry_step(label: str, attempts: int, func):
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
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


def read_sms_text(path: Path = SMS_PATH) -> str:
    if not path.exists():
        logging.warning("sms.md not found; click captcha solving may require manual mode.")
        return ""
    return path.read_text(encoding="utf-8")


def extract_2captcha_key(sms_text: str) -> Optional[str]:
    match = re.search(r"2captcha\.com\s*\n\s*API Key:\s*([A-Za-z0-9]+)", sms_text, re.I)
    return match.group(1).strip() if match else None


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


def click_xpath(page: Page, xpath: str, label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    locator = page.locator(f"xpath={xpath}")
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.click()
    logging.info("Clicked %s", label)


def click_by_text(page: Page, texts: Iterable[str], label: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    last_error: Optional[Exception] = None
    for text in texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click()
            logging.info("Clicked %s by text: %s", label, text)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise WorkflowError(f"Cannot click {label}") from last_error


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
                    page.wait_for_timeout(500)
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
        return code
    except WorkflowError:
        logging.info("Numeric captcha elements were not detected.")
        return None


def is_logged_in(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        return False
    return "亲爱的" in text and "退出" in text


def login(page: Page, config: Config) -> None:
    logging.info("Opening login URL: %s", config.url)
    page.goto(config.url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)

    ocr = ddddocr.DdddOcr(show_ad=False)

    def attempt_login() -> None:
        first_visible(page, USERNAME_SELECTORS).fill(config.username)
        first_visible(page, PASSWORD_SELECTORS).fill(config.password)
        solve_numeric_captcha(page, ocr)

        try:
            click_by_text(page, ["立即登录", "登录", "Login", "Sign in"], "login button", timeout_ms=8_000)
        except WorkflowError:
            page.locator('button[type="submit"], input[type="submit"]').first.click()

        page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        page.wait_for_timeout(1_500)
        if not is_logged_in(page):
            captcha = page.locator('img[src*="ShowKey" i]').first
            if captcha.count():
                captcha.click()
            raise WorkflowError("Login did not reach authenticated resource page.")

    retry_step("login", 4, attempt_login)
    logging.info("Login succeeded; current URL: %s", page.url)


def choose_resource(page: Page) -> None:
    click_xpath(page, ENGLISH_DATABASE_XPATH, "English database tab")
    page.wait_for_timeout(800)
    click_xpath(page, WOS_SCI_XPATH, "Web of Science/SCI")
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


def wait_for_wos_page(page: Page, timeout_ms: int = 45_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        text = page.locator("body").inner_text(timeout=5_000)
        if re.search(r"Advanced Search|Web of Science|QUERY BUILDER|Search", text, re.I):
            return True
        if "captcha" in text.lower() or "验证码" in text:
            return False
        page.wait_for_timeout(1_000)
    return False


def solve_click_captcha_if_present(
    page: Page,
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

    key = extract_2captcha_key(sms_text)
    if not key:
        raise WorkflowError("Click captcha detected, but no 2captcha key was found in sms.md.")

    captcha = candidates.first
    captcha.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    image_bytes = captcha.screenshot()
    instruction = extract_click_captcha_instruction(body_text)
    points = solve_2captcha_coordinates(key, image_bytes, instruction)
    expected_count = expected_click_count(instruction)
    if expected_count:
        points = points[:expected_count]

    box = captcha.bounding_box()
    if not box:
        raise WorkflowError("Cannot locate captcha image bounding box.")

    for point in points:
        page.mouse.click(box["x"] + point["x"], box["y"] + point["y"])
        page.wait_for_timeout(300)
    logging.info("Clicked %d captcha coordinates.", len(points))
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(2_000)
    return True


def extract_click_captcha_instruction(body_text: str) -> str:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    for line in lines:
        if "请" in line and ("点击" in line or "依次" in line):
            return f"Click only these Chinese characters in order: {line[:120]}"
    return "Click the requested objects in order."


def expected_click_count(instruction: str) -> int:
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", instruction)
    if quoted:
        return len(quoted)
    match = re.search(r"点击\s*([\u4e00-\u9fff])", instruction)
    return 1 if match else 0


def solve_2captcha_coordinates(
    api_key: str,
    image_bytes: bytes,
    instruction: str,
    poll_interval: int = 5,
    timeout_s: int = 180,
) -> list[dict[str, int]]:
    response = requests.post(
        "http://2captcha.com/in.php",
        data={
            "key": api_key,
            "method": "post",
            "coordinatescaptcha": 1,
            "textinstructions": instruction,
            "json": 1,
        },
        files={"file": ("captcha.png", image_bytes, "image/png")},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 1:
        raise WorkflowError(f"2captcha submit failed: {payload.get('request')}")

    captcha_id = payload["request"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        result = requests.get(
            "http://2captcha.com/res.php",
            params={"key": api_key, "action": "get", "id": captcha_id, "json": 1},
            timeout=30,
        )
        result.raise_for_status()
        data = result.json()
        if data.get("status") == 1:
            return parse_2captcha_coordinates(str(data.get("request", "")))
        if data.get("request") != "CAPCHA_NOT_READY":
            raise WorkflowError(f"2captcha solve failed: {data.get('request')}")

    raise WorkflowError("2captcha timed out.")


def parse_2captcha_coordinates(raw: str) -> list[dict[str, int]]:
    points: list[dict[str, int]] = []
    for x_raw, y_raw in re.findall(r"x\s*=\s*(\d+)\s*,\s*y\s*=\s*(\d+)", raw):
        points.append({"x": int(x_raw), "y": int(y_raw)})
    if points:
        return points

    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        parsed = None

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and "x" in item and "y" in item:
                points.append({"x": int(item["x"]), "y": int(item["y"])})
    if not points:
        raise WorkflowError(f"Cannot parse 2captcha coordinates: {raw!r}")
    return points


def click_entry_and_get_page(
    context: BrowserContext,
    page: Page,
    entry_name: str,
    sms_text: str,
    manual_captcha: bool,
) -> Page:
    link = find_entry_link(page, entry_name)
    link.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    old_pages = set(context.pages)

    logging.info("Trying WOS entry: %s", entry_name)
    link.click()
    page.wait_for_timeout(2_000)

    new_pages = [candidate for candidate in context.pages if candidate not in old_pages]
    active_page = new_pages[-1] if new_pages else page
    active_page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    solve_click_captcha_if_present(active_page, sms_text, manual_captcha)
    active_page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)

    if wait_for_wos_page(active_page):
        return active_page
    raise WorkflowError(f"Entry did not reach WOS page: {entry_name}")


def select_working_entry(
    context: BrowserContext,
    page: Page,
    state: dict[str, Any],
    sms_text: str,
    manual_captcha: bool,
) -> Page:
    last_error: Optional[Exception] = None
    for entry_name in ordered_entries(state):
        try:
            wos_page = click_entry_and_get_page(context, page, entry_name, sms_text, manual_captcha)
            state["last_working_entry"] = entry_name
            save_state(state)
            logging.info("Selected WOS entry: %s", entry_name)
            return wos_page
        except Exception as exc:
            last_error = exc
            logging.warning("Entry failed: %s (%s)", entry_name, exc)
            if page.is_closed():
                page = context.pages[0]
    raise WorkflowError("No WOS entry is currently usable.") from last_error


def open_advanced_search(page: Page) -> None:
    dismiss_wos_overlays(page)
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
    if not clicked:
        try:
            page.get_by_text(re.compile(r"^QUERY BUILDER$", re.I)).first.click(timeout=8_000)
            clicked = True
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


def fill_publication_date_query(page: Page, date_value: str) -> None:
    field_selector = page.get_by_role("combobox", name=re.compile("Select search field", re.I)).first
    field_selector.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    field_selector.click()

    option = page.get_by_role("option", name=re.compile(r"^Publication Date$", re.I))
    for _ in range(20):
        try:
            option.click(timeout=1_000)
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

    page.wait_for_selector("text=Publication Date", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(500)
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
    if filled != [date_value, date_value]:
        save_debug_artifacts(page, "publication_date_range_not_written")
        raise WorkflowError(f"Publication Date range was not written. Current value: {filled!r}")

    add_button = page.get_by_role("button", name=re.compile(r"^Add to query$", re.I)).first
    add_button.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    add_button.click()
    page.wait_for_timeout(1_000)

    preview_value = page.locator('textarea[placeholder*="Enter or edit"], textarea').first.input_value(timeout=5_000)
    if "DOP=" not in preview_value or " to " in preview_value:
        save_debug_artifacts(page, "publication_date_not_added_to_query")
        raise WorkflowError(f"Publication Date was not added to query preview: {preview_value!r}")
    logging.info("Added publication date query through UI: %s", preview_value)


def run_search_and_count(page: Page) -> int:
    dismiss_wos_overlays(page)
    save_debug_artifacts(page, "before_search_click")
    page.evaluate(
        """() => {
            const buttons = [...document.querySelectorAll('button')]
              .filter((button) => (button.innerText || '').trim() === 'Search' && !button.disabled);
            const target = buttons[buttons.length - 1];
            if (!target) throw new Error('enabled Search button not found');
            target.click();
        }"""
    )
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_selector("text=/results from Web of Science Core Collection/i", timeout=90_000)
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


def export_batch(page: Page, start: int, end: int, download_dir: Path) -> Download:
    logging.info("Exporting records %d to %d", start, end)
    page.get_by_role("button", name=re.compile(r"^Export$", re.I)).last.click()
    page.get_by_role("menuitem", name=re.compile(r"^BibTeX$", re.I)).click()
    page.wait_for_selector("text=Export Records to BibTeX File", timeout=DEFAULT_TIMEOUT_MS)

    range_radio = page.locator('input[type="radio"][value="fromRange"]').first
    range_radio.check(force=True)

    start_box = page.get_by_label(re.compile("starting record range", re.I))
    end_box = page.get_by_label(re.compile("ending record range", re.I))
    start_box.fill(str(start))
    end_box.fill(str(end))

    record_content = page.get_by_role("combobox", name=re.compile("Filter by", re.I)).first
    record_content.click()
    page.get_by_role("option", name="Full Record and Cited References").click()

    download_dir.mkdir(exist_ok=True)
    with page.expect_download(timeout=180_000) as download_info:
        page.get_by_role("button", name=re.compile(r"^Export$", re.I)).first.click()
    download = download_info.value
    suggested_name = download.suggested_filename or f"wos_{start}_{end}.bib"
    safe_name = f"wos_{start}_{end}_{int(time.time())}_{suggested_name}"
    download.save_as(download_dir / safe_name)
    logging.info("Downloaded %s", safe_name)
    return download


def process_task(
    page: Page,
    task: DateTask,
    tasks: list[DateTask],
    max_batches: Optional[int] = None,
) -> int:
    logging.info("Processing date %s from progress %s", task.date, task.progress)
    open_advanced_search(page)
    fill_publication_date_query(page, task.date)
    total = run_search_and_count(page)
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
                lambda start=start, end=end: export_batch(page, start, end, DOWNLOAD_DIR),
            )
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
        for account_index, config in enumerate(configs, start=1):
            if next_task(tasks) is None:
                return 0
            if args.max_batches is not None and total_batches_done >= args.max_batches:
                return 0

            browser = playwright.chromium.launch(headless=args.headless)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            try:
                logging.info(
                    "Using account %d/%d: %s",
                    account_index,
                    len(configs),
                    config.label or config.username,
                )
                login(page, config)
                choose_resource(page)
                wos_page = select_working_entry(context, page, state, sms_text, args.manual_captcha)
                remaining_batches = None
                if args.max_batches is not None:
                    remaining_batches = args.max_batches - total_batches_done
                total_batches_done += process_available_tasks(
                    wos_page,
                    tasks,
                    max_batches=remaining_batches,
                )
                if args.max_batches is not None and total_batches_done >= args.max_batches:
                    return 0
                if next_task(tasks) is None:
                    return 0
            except AccountDownloadLimit as exc:
                for index, open_page in enumerate(context.pages):
                    try:
                        save_debug_artifacts(open_page, f"account_limit_page_{index}", exc)
                    except Exception:
                        logging.exception("Failed to collect debug artifacts for page %d.", index)
                logging.warning(
                    "Account %s appears to have reached today's download limit; switching account.",
                    config.label or config.username,
                )
                continue
            except Exception as exc:
                task = next_task(tasks)
                if task is not None:
                    task.status = "failed"
                    write_date_tasks(tasks)
                for index, open_page in enumerate(context.pages):
                    try:
                        save_debug_artifacts(open_page, f"workflow_failed_page_{index}", exc)
                    except Exception:
                        logging.exception("Failed to collect debug artifacts for page %d.", index)
                progress = task.progress if task is not None else "n/a"
                logging.exception("Workflow failed. Progress preserved as %s.", progress)
                return 1
            finally:
                context.close()
                browser.close()

        task = next_task(tasks)
        if task is None:
            return 0
        logging.error(
            "No more configured accounts. Remaining task %s is preserved as %s.",
            task.date,
            task.progress,
        )
        return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", dest="headless", action="store_false", help="Show browser UI.")
    parser.add_argument("--headless", dest="headless", action="store_true", help="Run headless.")
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="Pause for manual click-captcha solving instead of calling 2captcha.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop after exporting this many 500-record batches. Useful for verification.",
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
