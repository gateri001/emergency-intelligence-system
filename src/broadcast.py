"""
Surjection: geo-targeted mass alerting. A surjective function reaches every
element in its target - the point here is guaranteed reach to everyone in
an affected area, independent of anyone choosing to reshare a post.

Pluggable so a real SMS provider can be dropped in later without touching
the calling code in main.py - only get_provider() and BROADCAST_PROVIDER
need to change.
"""
import os
from abc import ABC, abstractmethod


class BroadcastProvider(ABC):
    @abstractmethod
    def send(self, phone_number: str, message: str) -> dict:
        ...


class ConsoleBroadcastProvider(BroadcastProvider):
    """Default provider. No real SMS is sent - logs what would be sent, so
    the whole pipeline (geo-targeting, audit trail, API contract) can be
    built and tested before there's a live SMS account behind it."""

    def send(self, phone_number: str, message: str) -> dict:
        print(f"[BROADCAST -> {phone_number}] {message}")
        return {"success": True, "provider": "console", "phone_number": phone_number}


class AfricasTalkingProvider(BroadcastProvider):
    """
    Real SMS via Africa's Talking. Requires AT_USERNAME and AT_API_KEY
    environment variables - CTO note: I (as of this build pass) do not have
    real credentials to test this against, so this is code-complete but
    UNVERIFIED against a live account. Logged as a known gap, not hidden:
    the first real broadcast through this provider should be treated as a
    test, watched closely, not assumed correct because the code looks
    right. Get a sandbox account at account.africastalking.com (free) to
    verify before using it for a real alert.
    """

    def __init__(self):
        self.username = os.environ.get("AT_USERNAME")
        self.api_key = os.environ.get("AT_API_KEY")
        if not self.username or not self.api_key:
            raise RuntimeError(
                "AfricasTalkingProvider requires AT_USERNAME and AT_API_KEY "
                "environment variables. Unset BROADCAST_PROVIDER to fall back "
                "to the console provider until real credentials exist."
            )
        import africastalking

        africastalking.initialize(self.username, self.api_key)
        self._sms = africastalking.SMS

    def send(self, phone_number: str, message: str) -> dict:
        try:
            response = self._sms.send(message, [phone_number])
        except Exception as e:
            return {"success": False, "provider": "africastalking", "phone_number": phone_number, "error": str(e)}
        return {"success": True, "provider": "africastalking", "phone_number": phone_number, "raw": response}


class SMSGateProvider(BroadcastProvider):
    """
    Real SMS via a self-hosted SMS Gate instance (https://sms-gate.app) -
    an Android phone with a real SIM acting as your own SMS gateway.
    Apache 2.0, no per-message API fee, no aggregator markup: the only real
    cost is whatever your own carrier charges per SMS/bundle. This is the
    scrappy default until Triagia has funding - AfricasTalkingProvider
    stays in the codebase as the option to move to once a paid aggregator
    relationship is worth it (better delivery guarantees at real scale).

    Requires SMS_GATE_URL, SMS_GATE_USERNAME, SMS_GATE_PASSWORD - the
    address and login of your own SMS Gate instance (set it up in "Private
    Mode" per sms-gate.app's docs, not their public cloud relay, so no
    third party ever sees the message content).

    CTO note: UNVERIFIED against a live device - built from SMS Gate's
    documented REST API shape, not tested against a real phone. The exact
    endpoint path may differ slightly for a self-hosted "Private Mode"
    instance vs. their public cloud API - check your instance's own docs
    if this 404s, and treat the first real send as a test, not a known
    -working path.
    """

    def __init__(self):
        self.base_url = os.environ.get("SMS_GATE_URL")
        self.username = os.environ.get("SMS_GATE_USERNAME")
        self.password = os.environ.get("SMS_GATE_PASSWORD")
        if not self.base_url or not self.username or not self.password:
            raise RuntimeError(
                "SMSGateProvider requires SMS_GATE_URL, SMS_GATE_USERNAME, and "
                "SMS_GATE_PASSWORD environment variables - the address and "
                "login of your self-hosted SMS Gate instance (see "
                "https://sms-gate.app). Unset BROADCAST_PROVIDER to fall back "
                "to the console provider until it's set up."
            )

    def send(self, phone_number: str, message: str) -> dict:
        import requests

        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/3rdparty/v1/messages",
                json={"textMessage": {"text": message}, "phoneNumbers": [phone_number]},
                auth=(self.username, self.password),
                timeout=10,
            )
            response.raise_for_status()
            return {"success": True, "provider": "smsgate", "phone_number": phone_number, "raw": response.json()}
        except Exception as e:
            return {"success": False, "provider": "smsgate", "phone_number": phone_number, "error": str(e)}


def get_provider() -> BroadcastProvider:
    name = os.environ.get("BROADCAST_PROVIDER", "console").lower()
    if name == "console":
        return ConsoleBroadcastProvider()
    if name == "smsgate":
        return SMSGateProvider()
    if name == "africastalking":
        return AfricasTalkingProvider()
    raise ValueError(f"Unknown BROADCAST_PROVIDER: {name!r}")


# ---------------------------------------------------------------------------
# Shared dispatch helpers - used by every broadcast path (incident alerts and
# Missing Child Alert) so they behave identically.
# ---------------------------------------------------------------------------

def phone_key(phone: str) -> str:
    """Comparable form of a phone number: digits only, last 9 - so
    +254712345678, 0712 345 678 and 254712345678 are all the same person."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else digits


def select_targets(subscribers, latitude: float, longitude: float, radius_km: float):
    """Subscribers within radius_km, one per person. A number subscribed
    twice (nothing used to stop that) would otherwise be texted twice - a
    cost and, for an urgent alert, a trust problem."""
    from src.geo import haversine_km

    seen, targets = set(), []
    for s in subscribers:
        if haversine_km(latitude, longitude, s["latitude"], s["longitude"]) > radius_km:
            continue
        key = phone_key(s["phone_number"])
        if key in seen:
            continue
        seen.add(key)
        targets.append(s)
    return targets


def send_all(provider: BroadcastProvider, targets, message: str):
    """Returns (sent, failed). Counts what the provider actually reported,
    instead of assuming every send worked: the old code logged
    recipients_reached = len(targets) even when a provider returned
    success=False, so a dead SMS gateway looked like a successful alert."""
    sent = failed = 0
    for s in targets:
        try:
            result = provider.send(s["phone_number"], message)
        except Exception:
            result = {"success": False}
        if result.get("success"):
            sent += 1
        else:
            failed += 1
    return sent, failed
