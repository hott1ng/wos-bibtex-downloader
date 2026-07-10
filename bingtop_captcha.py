"""Bingtop captcha recognition helpers."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Optional

import requests


BINGTOP_UPLOAD_URL = "http://www.bingtop.com/ocr/upload/"


@dataclass(frozen=True)
class BingtopCredentials:
    username: str
    password: str


@dataclass(frozen=True)
class ClickPoint:
    x: int
    y: int


class BingtopCaptchaError(RuntimeError):
    """Raised when Bingtop cannot solve or return captcha coordinates."""


def extract_bingtop_credentials(sms_text: str) -> Optional[BingtopCredentials]:
    """Extract Bingtop username and password from sms.md style notes."""
    lines = [line.strip() for line in sms_text.splitlines()]
    for index, line in enumerate(lines):
        if line.lower() != "bingtop":
            continue
        values: list[str] = []
        for candidate in lines[index + 1 : index + 8]:
            if not candidate or candidate.startswith("http"):
                continue
            if "：" in candidate or ":" in candidate:
                continue
            values.append(candidate)
            if len(values) >= 2:
                return BingtopCredentials(username=values[0], password=values[1])
    return None


def solve_click_coordinates(
    credentials: BingtopCredentials,
    image_bytes: bytes,
    captcha_type: int,
    title: str = "",
    timeout_s: int = 60,
) -> list[ClickPoint]:
    """Submit a coordinate captcha to Bingtop and parse returned click points."""
    params = {
        "username": credentials.username,
        "password": credentials.password,
        "captchaData": base64.b64encode(image_bytes).decode("ascii"),
        "captchaType": int(captcha_type),
    }
    if title:
        params["subCaptchaData"] = title

    response = requests.post(BINGTOP_UPLOAD_URL, data=params, timeout=timeout_s)
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise BingtopCaptchaError(f"Bingtop returned non-JSON response: {response.text[:200]!r}") from exc

    if payload.get("code") != 0:
        raise BingtopCaptchaError(f"Bingtop solve failed: {payload.get('message') or payload!r}")

    recognition = str((payload.get("data") or {}).get("recognition") or "").strip()
    if not recognition or recognition.lower() == "none":
        raise BingtopCaptchaError(f"Bingtop returned empty coordinates: {recognition!r}")
    return parse_coordinate_text(recognition)


def parse_coordinate_text(raw: str) -> list[ClickPoint]:
    """Parse Bingtop coordinate text like '100,102|200,202'."""
    points: list[ClickPoint] = []
    for x_raw, y_raw in re.findall(r"(\d+)\s*[,，]\s*(\d+)", raw):
        points.append(ClickPoint(x=int(x_raw), y=int(y_raw)))
    if not points:
        raise BingtopCaptchaError(f"Cannot parse Bingtop coordinates: {raw!r}")
    return points
