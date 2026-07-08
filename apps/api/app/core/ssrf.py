"""
SSRF guard for outbound webhook URLs.

Webhooks are user-supplied URLs that the worker later makes real HTTP
requests to. Without validation, a user could register a webhook pointing
at an internal service (http://localhost:5432), a cloud metadata endpoint
(http://169.254.169.254/...), or any other address inside our own network,
and use webhook delivery as a server-side request forgery primitive.

This module is intentionally dependency-free (stdlib only) and is used by
both the create and update webhook schemas so the two contracts can't drift.
"""
import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


def _is_safe_public_host(hostname: str | None) -> bool:
    if not hostname or hostname.lower() in _BLOCKED_HOSTNAMES:
        return False

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Can't resolve — reject rather than allow through
        return False

    if not addr_infos:
        return False

    for _family, _type, _proto, _canon, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False

    return True


def validate_webhook_url(url: str) -> str:
    """
    Raises ValueError if the URL is not https:// or resolves to a
    non-routable / internal address. Returns the URL unchanged otherwise,
    so it can be used directly inside a Pydantic field_validator.
    """
    if not url.startswith("https://"):
        raise ValueError("Webhook URL must use https://")

    hostname = urlparse(url).hostname
    if not _is_safe_public_host(hostname):
        raise ValueError(
            "Webhook URL must resolve to a public, routable address"
        )

    return url