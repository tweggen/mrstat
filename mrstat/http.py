from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session(verify: bool | str, headers: dict[str, str], auth=None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "mrstat/0.1", **headers})
    s.verify = verify
    s.auth = auth
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],  # Jira search is a read-only POST
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
