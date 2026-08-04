"""Vendor-neutral production adapters shared by the platform services."""

from platform_infra.identity import OidcIdentityMiddleware
from platform_infra.object_storage import S3ObjectStorage
from platform_infra.opa import OpaAuthorizationMiddleware, OpaAuthorizer
from platform_infra.postgres import PostgresConnection, connect_postgres, execute_script

__all__ = [
    "OidcIdentityMiddleware",
    "OpaAuthorizationMiddleware",
    "OpaAuthorizer",
    "PostgresConnection",
    "S3ObjectStorage",
    "connect_postgres",
    "execute_script",
]
