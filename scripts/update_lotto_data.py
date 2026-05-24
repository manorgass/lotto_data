#!/usr/bin/env python3
"""Fetch the latest Lotto 6/45 draw data from dhlottery and update JSON data."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = REPO_ROOT / "lotto_data.json"
RESULT_URL = os.getenv("LOTTO_RESULT_URL", "https://www.dhlottery.co.kr/lt645/result")
ROUND_INFO_URL = os.getenv(
    "LOTTO_ROUND_INFO_URL",
    "https://www.dhlottery.co.kr/lt645/selectPstLt645InfoNew.do",
)
FETCH_RETRIES = int(os.getenv("LOTTO_FETCH_RETRIES", "3"))
FETCH_TIMEOUT_SECONDS = int(os.getenv("LOTTO_FETCH_TIMEOUT_SECONDS", "30"))
MAX_PROBE_ROUNDS = int(os.getenv("LOTTO_MAX_PROBE_ROUNDS", "10"))

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

AJAX_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "AJAX": "true",
    "Referer": RESULT_URL,
    "X-Requested-With": "XMLHttpRequest",
    "requestMenuUri": "/lt645/result",
}


class RoundNotFoundError(RuntimeError):
    pass


def fetch_text(url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> str:
    request_url = url
    if params:
        request_url = f"{url}?{urlencode(params)}"

    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        request = Request(request_url, headers=headers or BASE_HEADERS)
        try:
            with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except HTTPError as exc:
            last_error = RuntimeError(f"HTTP {exc.code} while fetching {request_url}")
        except URLError as exc:
            last_error = RuntimeError(f"Network error while fetching {request_url}: {exc.reason}")
        except TimeoutError as exc:
            last_error = RuntimeError(f"Timeout while fetching {request_url}")

        if attempt < FETCH_RETRIES:
            time.sleep(attempt * 2)

    assert last_error is not None
    raise last_error


def load_lotto_data() -> list[dict[str, Any]]:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{DATA_FILE} must contain a JSON array")
    return data


def find_latest_round() -> int:
    html = fetch_text(RESULT_URL)
    candidates: set[int] = set()

    patterns = [
        r'data-value=["\'](\d{1,5})["\']',
        r'<option\s+value=["\'](\d{1,5})["\'][^>]*>\s*\d+\s*회',
        r'id=["\']opt_val["\']\s+value=["\'](\d{1,5})["\']',
    ]
    for pattern in patterns:
        candidates.update(int(match) for match in re.findall(pattern, html))

    candidates = {round_no for round_no in candidates if 1 <= round_no <= 9999}
    if not candidates:
        raise RuntimeError("Could not find latest round number on dhlottery result page")

    return max(candidates)


def fetch_round_info(round_no: int) -> dict[str, Any]:
    response_text = fetch_text(
        ROUND_INFO_URL,
        params={"srchDir": "center", "srchLtEpsd": str(round_no)},
        headers=AJAX_HEADERS,
    )

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        snippet = response_text[:120].replace("\n", " ")
        raise RuntimeError(f"Expected JSON for round {round_no}, got: {snippet}") from exc

    rounds = payload.get("data", {}).get("list", [])
    if not isinstance(rounds, list):
        raise RuntimeError(f"Unexpected response shape for round {round_no}")

    for item in rounds:
        if int(item.get("ltEpsd", 0)) == round_no:
            return item

    raise RoundNotFoundError(f"Round {round_no} was not found in dhlottery response")


def normalize_draw_date(raw_date: Any) -> str:
    date_text = str(raw_date)
    parsed = datetime.strptime(date_text, "%Y%m%d")
    return parsed.strftime("%Y-%m-%d")


def to_lotto_record(item: dict[str, Any]) -> dict[str, Any]:
    numbers = [int(item[f"tm{index}WnNo"]) for index in range(1, 7)]
    bonus_number = int(item["bnsWnNo"])

    validate_numbers(numbers, bonus_number)

    return {
        "round": int(item["ltEpsd"]),
        "drawDate": normalize_draw_date(item["ltRflYmd"]),
        "numbers": numbers,
        "bonusNumber": bonus_number,
        "totalPrize": int(item["rnk1SumWnAmt"]),
        "prizePerWinner": int(item["rnk1WnAmt"]),
        "winners": int(item["rnk1WnNope"]),
    }


def validate_numbers(numbers: list[int], bonus_number: int) -> None:
    all_numbers = numbers + [bonus_number]
    if len(numbers) != 6:
        raise ValueError("A lotto record must have exactly six winning numbers")
    if len(set(all_numbers)) != 7:
        raise ValueError(f"Winning numbers and bonus number must be unique: {all_numbers}")
    if any(number < 1 or number > 45 for number in all_numbers):
        raise ValueError(f"Winning numbers must be between 1 and 45: {all_numbers}")


def validate_rounds(records: list[dict[str, Any]]) -> None:
    rounds = [int(record["round"]) for record in records]
    duplicate_rounds = sorted({round_no for round_no in rounds if rounds.count(round_no) > 1})
    if duplicate_rounds:
        raise ValueError(f"Duplicate rounds found: {duplicate_rounds}")


def format_record(record: dict[str, Any]) -> str:
    numbers = ", ".join(str(number) for number in record["numbers"])
    lines = [
        "  {",
        f'    "round": {int(record["round"])},',
        f'    "drawDate": "{record["drawDate"]}",',
        f'    "numbers": [{numbers}],',
        f'    "bonusNumber": {int(record["bonusNumber"])},',
        f'    "totalPrize": {int(record["totalPrize"])},',
        f'    "prizePerWinner": {int(record["prizePerWinner"])},',
        f'    "winners": {int(record["winners"])}',
        "  }",
    ]
    return "\n".join(lines)


def write_lotto_data(records: list[dict[str, Any]]) -> None:
    formatted_records = ",\n".join(format_record(record) for record in records)
    DATA_FILE.write_text(f"[\n{formatted_records}\n]\n", encoding="utf-8")


def fetch_new_records(current_latest: int) -> tuple[list[dict[str, Any]], int | None]:
    try:
        source_latest = find_latest_round()
    except RuntimeError as exc:
        print(f"Could not read latest round from result page, probing API instead: {exc}", file=sys.stderr)
        source_latest = None

    if source_latest is not None:
        if source_latest <= current_latest:
            return [], source_latest
        rounds_to_fetch = range(current_latest + 1, source_latest + 1)
        return [to_lotto_record(fetch_round_info(round_no)) for round_no in rounds_to_fetch], source_latest

    new_records: list[dict[str, Any]] = []
    for round_no in range(current_latest + 1, current_latest + MAX_PROBE_ROUNDS + 1):
        try:
            new_records.append(to_lotto_record(fetch_round_info(round_no)))
        except RoundNotFoundError:
            break

    probed_latest = max((record["round"] for record in new_records), default=None)
    return new_records, probed_latest


def main() -> int:
    records = load_lotto_data()
    validate_rounds(records)

    existing_rounds = {int(record["round"]) for record in records}
    current_latest = max(existing_rounds) if existing_rounds else 0
    new_records, source_latest = fetch_new_records(current_latest)

    if not new_records:
        print(f"Already up to date. local={current_latest}, source={source_latest}")
        return 0

    updated_records = sorted([*new_records, *records], key=lambda record: int(record["round"]), reverse=True)
    validate_rounds(updated_records)
    write_lotto_data(updated_records)

    added = ", ".join(str(record["round"]) for record in new_records)
    latest = max(int(record["round"]) for record in updated_records)
    print(f"Updated {DATA_FILE.name}. added={added}, latest={latest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"update_lotto_data.py failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
