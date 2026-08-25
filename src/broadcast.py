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
    Real SMS via Africa's Talking. Requires AT_USERNAME and AT_API_KEY.
    Not wired to a live account in this codebase yet - once there's a real
    account, implement send() with the `africastalking` SDK:

        import africastalking
        africastalking.initialize(self.username, self.api_key)
        africastalking.SMS.send(message, [phone_number])

    Kept as a stub rather than a fake implementation on purpose - a
    provider that silently pretends to send real SMS is worse than one
    that clearly refuses to run without real credentials.
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

    def send(self, phone_number: str, message: str) -> dict:
        raise NotImplementedError(
            "Wire up the africastalking SDK here once there's a real account "
            "- see this class's docstring for the exact call shape."
        )


def get_provider() -> BroadcastProvider:
    name = os.environ.get("BROADCAST_PROVIDER", "console").lower()
    if name == "console":
        return ConsoleBroadcastProvider()
    if name == "africastalking":
        return AfricasTalkingProvider()
    raise ValueError(f"Unknown BROADCAST_PROVIDER: {name!r}")
