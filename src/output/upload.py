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


def upload_to_s3(
    data: bytes,
    s3_uri: str,
    filename: str,
    content_type: str,
    verify_ssl: bool = True,
) -> bool:
    """Upload data to a public/overpermissive S3 bucket via unsigned REST PUT.

    s3_uri: "s3://bucket-name" or "s3://bucket-name/prefix"
    filename: the object key suffix (e.g. "DESKTOP-ABC-20260403-120000.json")

    This constructs a direct HTTPS PUT to s3.amazonaws.com without any AWS
    credentials or signing — works only on buckets that allow public writes
    or have overly permissive ACLs/policies.
    """
    # Parse s3://bucket/optional-prefix
    path = s3_uri.replace("s3://", "", 1)
    parts = path.split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") + "/" if len(parts) > 1 and parts[1] else ""

    key = f"{prefix}{filename}"
    url = f"https://{bucket}.s3.amazonaws.com/{key}"

    ctx = None
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(data)))
    req.add_header("User-Agent", "Unshadow-AI/2.0")

    try:
        response = urllib.request.urlopen(req, context=ctx, timeout=120)
        status = response.getcode()
        print(
            f"S3 upload successful: PUT {url} -> HTTP {status} ({len(data)} bytes)",
            file=sys.stderr,
        )
        return True
    except urllib.error.HTTPError as e:
        print(
            f"S3 upload failed: HTTP {e.code} {e.reason} -> {url}",
            file=sys.stderr,
        )
        if e.code == 403:
            print(
                "  Hint: bucket may not allow public/unsigned writes. "
                "Use --upload with a presigned URL instead.",
                file=sys.stderr,
            )
        return False
    except urllib.error.URLError as e:
        print(
            f"S3 upload failed: {e.reason} -> {url}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(
            f"S3 upload failed: {e} -> {url}",
            file=sys.stderr,
        )
        return False
