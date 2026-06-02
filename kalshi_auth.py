"""Kalshi API auth: loads per-developer credentials from env + local key file.

No secrets live in this file or anywhere in git. Each developer supplies their
own KALSHI_*_KEY_ID and a local .pem (see .env.example). Switch between the
demo and prod accounts with KALSHI_ENV=demo|prod.

Usage:
    from kalshi_auth import KalshiClient
    client = KalshiClient.from_env()          # uses KALSHI_ENV
    print(client.get("/portfolio/balance"))   # signed GET
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Use the OS trust store (macOS keychain) so corporate-proxy root CAs are
# trusted. Without this, requests uses certifi's bundle and TLS-intercepting
# proxies (Zscaler/Netskope) cause CERTIFICATE_VERIFY_FAILED. No-op if the
# package isn't installed.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency). Does not overwrite vars
    already set in the real environment."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str, base_url: str):
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        key_bytes = Path(private_key_path).read_bytes()
        self.private_key = serialization.load_pem_private_key(key_bytes, password=None)
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=64)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    @classmethod
    def from_env(cls, env: str | None = None) -> "KalshiClient":
        _load_dotenv()
        env = (env or os.environ.get("KALSHI_ENV", "demo")).lower()
        if env not in BASE_URLS:
            raise ValueError(f"KALSHI_ENV must be one of {list(BASE_URLS)}, got {env!r}")
        prefix = "KALSHI_DEMO" if env == "demo" else "KALSHI_PROD"
        key_id = os.environ.get(f"{prefix}_KEY_ID")
        key_path = os.environ.get(f"{prefix}_PRIVATE_KEY_PATH")
        if not key_id or not key_path:
            raise RuntimeError(
                f"Missing {prefix}_KEY_ID / {prefix}_PRIVATE_KEY_PATH. "
                "Copy .env.example to .env and fill in your values."
            )
        return cls(key_id=key_id, private_key_path=key_path, base_url=BASE_URLS[env])

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        # Kalshi signs: timestamp + METHOD + path (path = full request path, no query)
        message = (timestamp_ms + method.upper() + path).encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        # The signed path must include the API prefix that follows the host.
        path = "/trade-api/v2" + endpoint
        resp = self._session.get(
            self.base_url + endpoint,
            headers=self._headers("GET", path),
            params=params,
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    # Connectivity test: authenticate and fetch the account balance.
    client = KalshiClient.from_env()
    env = os.environ.get("KALSHI_ENV", "demo")
    print(f"Testing Kalshi [{env}] as key {client.key_id[:8]}… → {client.base_url}")
    balance = client.get("/portfolio/balance")
    print("✅ Auth OK. Balance:", balance)
