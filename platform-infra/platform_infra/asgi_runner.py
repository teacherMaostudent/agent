"""Start an internal ASGI service with mandatory client-certificate verification.

The runner is deliberately configured through generic ``PLATFORM_*`` variables
so application packages do not need to duplicate Uvicorn TLS command handling.
Local development keeps plaintext HTTP; production must explicitly enable it.
"""

from __future__ import annotations

import os
import ssl


def main() -> None:
    """Launch the configured ASGI application and fail closed for incomplete mTLS."""
    import uvicorn

    app = os.environ.get("PLATFORM_ASGI_APP", "").strip()
    if not app:
        raise ValueError("PLATFORM_ASGI_APP is required")
    kwargs: dict[str, object] = {
        "host": os.environ.get("PLATFORM_ASGI_HOST", "0.0.0.0"),
        "port": int(os.environ.get("PLATFORM_ASGI_PORT", "8000")),
    }
    if os.environ.get("PLATFORM_MTLS_SERVER_ENABLED", "false").lower() == "true":
        cert_file = os.environ.get("PLATFORM_MTLS_CERT_FILE", "")
        key_file = os.environ.get("PLATFORM_MTLS_KEY_FILE", "")
        ca_file = os.environ.get("PLATFORM_MTLS_CA_FILE", "")
        if not all((cert_file, key_file, ca_file)):
            raise ValueError("server mTLS requires certificate, key and CA paths")
        # CERT_REQUIRED prevents a reachable internal port from accepting a
        # client that has no certificate issued by the platform CA.
        kwargs.update(
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
            ssl_ca_certs=ca_file,
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        )
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    main()
