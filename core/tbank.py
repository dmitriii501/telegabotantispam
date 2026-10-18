"""T-Bank internet acquiring (the former Tinkoff Acquiring): payment links, signed notifications, refunds.

The owner of the service is an individual entrepreneur, so every payment carries an online receipt (54-FZ).
Docs: https://www.tbank.ru/kassa/dev/payments/ . Everything that can be tested without the network is a plain
function here; the HTTP call is one small method that tests replace.
"""

import hashlib
import hmac
import logging
import os
import re
import ssl
from functools import lru_cache

import aiohttp

log = logging.getLogger(__name__)

API_URL = os.getenv("TBANK_API_URL", "https://securepay.tinkoff.ru/v2").rstrip("/")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")


DEFAULT_CA_FILE = "/etc/defenceai/russian_trusted_root_ca.pem"


class TBankError(Exception):
    pass


@lru_cache(maxsize=4)
def _context(path: str) -> ssl.SSLContext:
    context = ssl.create_default_context()  # the usual system roots stay trusted
    try:
        context.load_verify_locations(cafile=path)  # and, for the bank only, the Russian Trusted Root CA
    except FileNotFoundError:
        pass  # not installed on this machine: the usual roots are enough, if the bank's certificate allows
    except (ssl.SSLError, OSError) as e:
        log.warning("Cannot use the CA file %s, the system roots only: %s", path, e)
    return context


def ssl_context() -> ssl.SSLContext:
    """Verification is never switched off. The file path comes from TBANK_CA_FILE and lives outside the
    application folder, so updating the bot does not touch it."""
    return _context(os.getenv("TBANK_CA_FILE", DEFAULT_CA_FILE))


def sign(params: dict, password: str) -> str:
    """Token: SHA-256 of the values of the root parameters (nested objects excluded) plus the terminal password,
    taken in the alphabetical order of the parameter names."""
    items = {k: v for k, v in params.items() if k != "Token" and not isinstance(v, (dict, list))}
    items["Password"] = password
    text = "".join(_text(items[k]) for k in sorted(items))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def kopecks(rubles: int) -> int:
    return rubles * 100


def receipt(email: str, name: str, amount_kop: int, taxation: str) -> dict:
    """One line: a service paid in full. No VAT (an entrepreneur on the simplified tax system)."""
    return {
        "Email": email,
        "Taxation": taxation,
        "Items": [
            {
                "Name": name[:128],
                "Price": amount_kop,
                "Quantity": 1.0,
                "Amount": amount_kop,
                "Tax": "none",
                "PaymentMethod": "full_payment",
                "PaymentObject": "service",
            }
        ],
    }


class TBank:
    def __init__(self, terminal_key: str, password: str, taxation: str = "usn_income", use_receipt: bool = True):
        self.terminal_key = terminal_key
        self.password = password
        self.taxation = taxation
        self.use_receipt = use_receipt

    @classmethod
    def from_env(cls) -> "TBank | None":
        key, password = os.getenv("TBANK_TERMINAL_KEY", "").strip(), os.getenv("TBANK_PASSWORD", "").strip()
        if not key or not password:
            return None
        return cls(key, password, os.getenv("TBANK_TAXATION", "usn_income"), os.getenv("TBANK_RECEIPT", "1") != "0")

    # ------------------------------------------------------------------ requests

    def init_payload(
        self, order_id: str, amount_kop: int, description: str, email: str | None, notify_url: str | None, success_url: str | None
    ) -> dict:
        payload = {
            "TerminalKey": self.terminal_key,
            "Amount": amount_kop,
            "OrderId": order_id,
            "Description": description[:140],
            "Language": "ru",
            "PayType": "O",  # one stage: the money is taken at once
        }
        if notify_url:
            payload["NotificationURL"] = notify_url
        if success_url:
            payload["SuccessURL"] = success_url
        if self.use_receipt and email:
            payload["Receipt"] = receipt(email, description, amount_kop, self.taxation)
        payload["Token"] = sign(payload, self.password)
        return payload

    async def create_payment(self, order_id, amount_kop, description, email, notify_url=None, success_url=None) -> tuple[str, str]:
        """Returns (payment id, link to pay)."""
        data = await self._post("Init", self.init_payload(order_id, amount_kop, description, email, notify_url, success_url))
        return str(data["PaymentId"]), data["PaymentURL"]

    async def cancel(self, payment_id: str, amount_kop: int, description: str, email: str | None) -> None:
        payload = {"TerminalKey": self.terminal_key, "PaymentId": payment_id, "Amount": amount_kop}
        if self.use_receipt and email:
            payload["Receipt"] = receipt(email, description, amount_kop, self.taxation)
        payload["Token"] = sign(payload, self.password)
        await self._post("Cancel", payload)

    async def _post(self, method: str, payload: dict) -> dict:
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_context())
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post(f"{API_URL}/{method}", json=payload) as response:
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise TBankError(f"нет связи с банком: {e}") from e
        if not isinstance(data, dict) or not data.get("Success"):
            log.warning("T-Bank %s refused: %s", method, {k: data.get(k) for k in ("ErrorCode", "Message", "Details")} if isinstance(data, dict) else data)
            raise TBankError((data or {}).get("Message") or "банк отклонил запрос")
        return data

    # ------------------------------------------------------------- notifications

    def verify(self, data: dict) -> bool:
        """The bank signs every notification the same way as we sign requests."""
        token = data.get("Token")
        return (
            isinstance(token, str)
            and data.get("TerminalKey") == self.terminal_key
            and hmac.compare_digest(token, sign(data, self.password))
        )
