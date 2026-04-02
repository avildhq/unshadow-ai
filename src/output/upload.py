import logging
import ssl
import sys
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def upload_results(
    data: bytes,
    url: str,
    content_type: str,
    method: str = "PUT",
    verify_ssl: bool = True,
) -> bool:
    """Upload data to a URL via HTTP PUT or POST.

    Returns True on success, False on failure.
    """
    ctx = None
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(data)))
    req.add_header("User-Agent", "Unshadow-AI/2.0")

    try:
        response = urllib.request.urlopen(req, context=ctx, timeout=120)
        status = response.getcode()
        print(
            f"Upload successful: {method.upper()} {url} -> HTTP {status} ({len(data)} bytes)",
            file=sys.stderr,
        )
        return True
    except urllib.error.HTTPError as e:
        print(
            f"Upload failed: HTTP {e.code} {e.reason} -> {url}",
            file=sys.stderr,
        )
        return False
    except urllib.error.URLError as e:
        print(
            f"Upload failed: {e.reason} -> {url}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(
            f"Upload failed: {e} -> {url}",
            file=sys.stderr,
        )
        return False
