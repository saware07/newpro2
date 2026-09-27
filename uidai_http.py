import os
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from dotenv import load_dotenv

load_dotenv()

UIDAI_HOST = "tathya.uidai.gov.in"


def _proxy_url():
    # Automatically read and parse proxies.txt if it exists
    proxies_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
    if os.path.exists(proxies_file):
        try:
            with open(proxies_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    
                    # Format: host:port:username:password
                    parts = line.split(":")
                    if len(parts) >= 4:
                        host = parts[0]
                        port = parts[1]
                        username = parts[2]
                        password = ":".join(parts[3:]) # Handles passwords with colons if any
                        return f"http://{username}:{password}@{host}:{port}"
                    elif len(parts) == 2:
                        # Fallback for host:port format
                        return f"http://{parts[0]}:{parts[1]}"
                    elif line.startswith("http://") or line.startswith("https://"):
                        return line
        except Exception as e:
            print(f"⚠️ Error reading proxies.txt: {e}")

    return (
        os.getenv("UIDAI_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or ""
    ).strip()


def _redact_proxy(url):
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.hostname or ""
            if parts.username:
                netloc = f"{parts.username}:***@{netloc}"
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return url


def make_uidai_session(retries=0, log=print):
    """Session for UIDAI. Automatically loads proxy from proxies.txt."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        connect=0,
        read=retries,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))

    proxy = _proxy_url()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        log(f"🌐 UIDAI requests via proxy: {_redact_proxy(proxy)}")
    else:
        log("⚠️ Warning: No proxy found in proxies.txt or environment variables. UIDAI may block direct connections.")
    return session


def check_uidai_reachability(timeout=8):
    """Returns (ok, message) after a TCP/HTTPS probe to UIDAI."""
    session = make_uidai_session(retries=0, log=lambda *_: None)
    proxy = _proxy_url()
    via = f"proxy {_redact_proxy(proxy)}" if proxy else "direct"
    try:
        res = session.get(f"https://{UIDAI_HOST}/", timeout=timeout, allow_redirects=True)
        return True, f"{via} -> HTTP {res.status_code}"
    except Exception as e:
        return False, f"{via} -> {e}"
