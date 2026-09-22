import ipaddress
import socket
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit


def validate_public_url(value, allowed_hosts):
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid_url") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UnsafeUrlError("invalid_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("userinfo_not_allowed")
    if port is not None and port != (80 if parsed.scheme == "http" else 443):
        raise UnsafeUrlError("port_not_allowed")
    host = (parsed.hostname or "").rstrip(".").lower()
    allowed = tuple(str(item).lower().lstrip(".") for item in (allowed_hosts or ()))
    if not host or not any(host == item or host.endswith("." + item) for item in allowed):
        raise UnsafeUrlError("host_not_allowed")
    try:
        ipaddress.ip_address(host)
        raise UnsafeUrlError("ip_not_allowed")
    except ValueError:
        pass
    try:
        addresses = {item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise UnsafeUrlError("host_unresolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeUrlError("local_address_not_allowed")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
from urllib.request import HTTPRedirectHandler, Request, build_opener


_ALLOWED_HOSTS = {"b23.tv"}
_ALLOWED_SUFFIX = ".bilibili.com"


class UnsafeUrlError(ValueError):
    pass


def validate_bilibili_url(value, resolve_host=True):
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid_url") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UnsafeUrlError("invalid_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("userinfo_not_allowed")
    if port is not None and port != (80 if parsed.scheme == "http" else 443):
        raise UnsafeUrlError("port_not_allowed")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or not (host in _ALLOWED_HOSTS or host == "bilibili.com" or host.endswith(_ALLOWED_SUFFIX)):
        raise UnsafeUrlError("host_not_allowed")
    try:
        ipaddress.ip_address(host)
        raise UnsafeUrlError("ip_not_allowed")
    except ValueError:
        pass
    if resolve_host:
        try:
            addresses = {item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise UnsafeUrlError("host_unresolved") from exc
        if not addresses:
            raise UnsafeUrlError("host_unresolved")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise UnsafeUrlError("local_address_not_allowed")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url = validate_bilibili_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def resolve_bilibili_redirects(value, headers=None, timeout=8, max_bytes=0):
    safe_url = validate_bilibili_url(value)
    opener = build_opener(ValidatingRedirectHandler())
    request = Request(safe_url, headers=headers or {})
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        final_url = validate_bilibili_url(response.geturl())
        body = response.read(max_bytes) if max_bytes else b""
    return final_url, body


def parse_single_range(value, file_size):
    if not value:
        return None
    if file_size <= 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid_range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("invalid_range")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        if not end_text.isdigit():
            raise ValueError("invalid_range")
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("unsatisfiable_range")
        length = min(suffix, file_size)
        return file_size - length, file_size - 1
    if not start_text.isdigit() or (end_text and not end_text.isdigit()):
        raise ValueError("invalid_range")
    start = int(start_text)
    if start >= file_size:
        raise ValueError("unsatisfiable_range")
    end = int(end_text) if end_text else file_size - 1
    if end < start:
        raise ValueError("invalid_range")
    return start, min(end, file_size - 1)
