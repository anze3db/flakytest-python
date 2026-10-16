"""Talking to flakytest.dev. Everything here is a no-op without FLAKYTEST_SECRET_TOKEN."""

import gzip
import json
import logging
import os
from typing import Any

import urllib3
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("flakytest")

http = urllib3.PoolManager(timeout=25.0, retries=3)

token = os.environ.get("FLAKYTEST_SECRET_TOKEN")
host = os.environ.get("FLAKYTEST_HOST", "https://flakytest.dev") + "/api/v1"
stash: dict[str, Any] = {}  # Can't use Stash from Pytest 7.x because it's not available in 6.x


def make_request(url: str, json_data: dict[str, Any], *, compress: bool = False) -> dict[str, Any] | None:
    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token or ""}
    try:
        if compress:
            headers["Content-Encoding"] = "gzip"
            body = gzip.compress(json.dumps(json_data, separators=(",", ":")).encode())
            response = http.request("POST", host + url, body=body, headers=headers)
        else:
            response = http.request("POST", host + url, json=json_data, headers=headers)
    except:
        return None
    if response.status != 200:
        try:
            message = response.json()["message"]
        except:
            message = ""
        logger.error(f"Flakytest: Failed to send data to {host}{url} non 200 response]\n{message}")
        return None
    response_json = response.json()
    if response_json.get("message"):
        logger.warning(f"\n{response_json['message']}")
    return response_json


def upload_profile(profile: dict[str, Any]) -> str | None:
    """Upload a profile document; returns its URL on flakytest.dev."""
    if not token or os.environ.get("FLAKYTEST_UPLOAD", "").lower() in ("0", "false", "no", "off"):
        return None
    payload = dict(profile)
    if stash.get("ingest_id"):
        payload["ingest_id"] = stash["ingest_id"]
    response = make_request("/profile", payload, compress=True)
    return response.get("url") if response else None
