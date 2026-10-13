import socket
from typing import Dict

_original_getaddrinfo = socket.getaddrinfo
_overrides: Dict[str, str] = {}


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # Resolve overridden hostname to the specified IP before system DNS lookup.
    # By substituting only the host and keeping the rest of urllib3/httpx's
    # connection logic intact, SNI and SSL cert verification still use the
    # ORIGINAL hostname — so HTTPS works correctly.
    resolved = _overrides.get(host, host)
    return _original_getaddrinfo(resolved, port, family, type, proto, flags)


def apply(host_map: Dict[str, str]) -> None:
    """Install (or replace) socket-level DNS overrides for this process."""
    global _overrides
    _overrides = dict(host_map)
    socket.getaddrinfo = _patched_getaddrinfo if _overrides else _original_getaddrinfo


def clear() -> None:
    apply({})


def current() -> Dict[str, str]:
    return dict(_overrides)


def parse_hosts_text(text: str) -> Dict[str, str]:
    """Parse /etc/hosts-style text → {hostname: ip}.

    Supports:
        1.2.3.4 example.com www.example.com   # multiple hostnames per line
        # comment lines and blank lines are ignored
    """
    result: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[0]
        for hostname in parts[1:]:
            if hostname.startswith('#'):
                break
            if hostname:
                result[hostname] = ip
    return result
