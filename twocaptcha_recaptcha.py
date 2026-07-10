"""2captcha token captcha solver helpers.

The API key is read from local sms.md notes at runtime. Do not hard-code or
log real captcha provider keys.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


IN_URL = "https://2captcha.com/in.php"
RES_URL = "https://2captcha.com/res.php"
DEFAULT_SMS_PATH = Path(__file__).resolve().parent / "sms.md"


class TwoCaptchaRecaptchaError(RuntimeError):
    """Raised when 2captcha cannot create or solve a token captcha task."""


@dataclass(frozen=True)
class RecaptchaV2Solution:
    task_id: int
    token: str
    cost: Optional[str] = None
    solve_count: Optional[int] = None


def extract_2captcha_api_key(sms_text: str) -> Optional[str]:
    """Extract the 2captcha API key from sms.md style notes."""
    lines = [line.strip() for line in sms_text.splitlines()]
    for index, line in enumerate(lines):
        if "2captcha.com" not in line.lower():
            continue
        for candidate in lines[index + 1 : index + 6]:
            match = re.search(r"API\s*Key\s*:\s*([A-Za-z0-9_-]+)", candidate, re.I)
            if match:
                return match.group(1).strip()
    return None


def load_2captcha_api_key(path: Path = DEFAULT_SMS_PATH) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing captcha config file: {path}")
    api_key = extract_2captcha_api_key(path.read_text(encoding="utf-8"))
    if not api_key:
        raise TwoCaptchaRecaptchaError("2captcha API key was not found in sms.md.")
    return api_key


def _post_2captcha_task(params: dict[str, object], timeout_s: int = 30) -> int:
    """Submit a task through the official classic 2captcha API."""
    payload = {**params, "json": 1}
    response = requests.post(IN_URL, data=payload, timeout=timeout_s)
    response.raise_for_status()
    data = response.json()

    if int(data.get("status", 0)) != 1:
        raise TwoCaptchaRecaptchaError(f"2captcha submit failed: {data.get('request') or data}")

    task_id = data.get("request")
    if not task_id:
        raise TwoCaptchaRecaptchaError(f"2captcha submit returned no captcha id: {data}")
    return int(task_id)


def create_recaptcha_v2_task(
    api_key: str,
    website_url: str,
    website_key: str,
    *,
    invisible: bool = False,
    user_agent: Optional[str] = None,
    cookies: Optional[str] = None,
    recaptcha_data_s_value: Optional[str] = None,
    api_domain: Optional[str] = None,
    timeout_s: int = 30,
) -> int:
    """Create a reCAPTCHA v2 task using 2captcha's classic API."""
    params: dict[str, object] = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": website_key,
        "pageurl": website_url,
        "invisible": 1 if invisible else 0,
    }
    if user_agent:
        params["userAgent"] = user_agent
    if cookies:
        params["cookies"] = cookies
    if recaptcha_data_s_value:
        params["data-s"] = recaptcha_data_s_value
    if api_domain:
        params["domain"] = api_domain

    return _post_2captcha_task(params, timeout_s=timeout_s)


def create_hcaptcha_task(
    api_key: str,
    website_url: str,
    website_key: str,
    *,
    invisible: bool = False,
    user_agent: Optional[str] = None,
    rqdata: Optional[str] = None,
    domain: Optional[str] = None,
    timeout_s: int = 30,
) -> int:
    """Create an hCaptcha task using 2captcha's documented classic API."""
    params: dict[str, object] = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": website_key,
        "pageurl": website_url,
        "invisible": 1 if invisible else 0,
    }
    if user_agent:
        params["userAgent"] = user_agent
    if rqdata:
        # 2captcha calls hCaptcha rqdata/custom data simply "data".
        params["data"] = rqdata
    if domain:
        params["domain"] = domain

    return _post_2captcha_task(params, timeout_s=timeout_s)


def get_recaptcha_v2_result(
    api_key: str,
    task_id: int,
    *,
    poll_interval_s: int = 5,
    timeout_s: int = 180,
    request_timeout_s: int = 30,
) -> RecaptchaV2Solution:
    """Poll 2captcha until a token captcha result is ready."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)
        response = requests.get(
            RES_URL,
            params={
                "key": api_key,
                "action": "get",
                "id": int(task_id),
                "json": 1,
            },
            timeout=request_timeout_s,
        )
        response.raise_for_status()
        data = response.json()

        request_value = str(data.get("request", "")).strip()
        if int(data.get("status", 0)) == 1 and request_value:
            return RecaptchaV2Solution(task_id=int(task_id), token=request_value)

        if request_value == "CAPCHA_NOT_READY":
            continue

        raise TwoCaptchaRecaptchaError(f"2captcha result failed: {request_value or data}")

    raise TwoCaptchaRecaptchaError(f"2captcha token task timed out: {task_id}")


def solve_recaptcha_v2(
    website_url: str,
    website_key: str,
    *,
    api_key: Optional[str] = None,
    sms_path: Path = DEFAULT_SMS_PATH,
    invisible: bool = False,
    user_agent: Optional[str] = None,
    cookies: Optional[str] = None,
    recaptcha_data_s_value: Optional[str] = None,
    api_domain: Optional[str] = None,
    poll_interval_s: int = 5,
    timeout_s: int = 180,
) -> RecaptchaV2Solution:
    """Create and solve a reCAPTCHA v2 task."""
    client_key = api_key or load_2captcha_api_key(sms_path)
    task_id = create_recaptcha_v2_task(
        client_key,
        website_url,
        website_key,
        invisible=invisible,
        user_agent=user_agent,
        cookies=cookies,
        recaptcha_data_s_value=recaptcha_data_s_value,
        api_domain=api_domain,
    )
    return get_recaptcha_v2_result(
        client_key,
        task_id,
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )


def solve_hcaptcha(
    website_url: str,
    website_key: str,
    *,
    api_key: Optional[str] = None,
    sms_path: Path = DEFAULT_SMS_PATH,
    invisible: bool = False,
    user_agent: Optional[str] = None,
    rqdata: Optional[str] = None,
    domain: Optional[str] = "hcaptcha.com",
    poll_interval_s: int = 5,
    timeout_s: int = 180,
) -> RecaptchaV2Solution:
    """Create and solve an hCaptcha task."""
    client_key = api_key or load_2captcha_api_key(sms_path)
    task_id = create_hcaptcha_task(
        client_key,
        website_url,
        website_key,
        invisible=invisible,
        user_agent=user_agent,
        rqdata=rqdata,
        domain=domain,
    )
    return get_recaptcha_v2_result(
        client_key,
        task_id,
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
