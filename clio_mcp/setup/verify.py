#!/usr/bin/env python3
"""Post-setup smoke test — verifies auth and basic API access."""

import sys
from pathlib import Path

from clio_mcp import credentials

CONFIG_DIR = credentials.CONFIG_DIR


def check_config():
    """Verify credentials are available via any storage backend."""
    client_id = credentials.get_secret("CLIO_CLIENT_ID")
    tokens = credentials.get_token_cache(credentials.LOCAL_SUBJECT)

    if not client_id:
        print(f"✗ Missing credentials (checked keyring + {credentials.ENV_FILE})")
        print("  Run: clio-mcp-setup")
        return False

    if not tokens:
        print(f"✗ Missing tokens (checked keyring + {credentials.TOKEN_FILE})")
        print("  Run: clio-mcp-setup")
        return False

    backend = credentials.storage_backend()
    print(f"✓ Credentials found  (backend: {backend})")
    print(f"✓ Tokens found       (backend: {backend})")
    return True


def check_api():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from clio_mcp.client import ClioClient

        client = ClioClient(credentials.LOCAL_SUBJECT)
        client.who_am_i()
        print("✓ Authenticated successfully")

        matters = client.list_matters(limit=5)
        count = len(matters.get("data", []))
        print(f"✓ Matters accessible: {count} returned (limit 5)")

        return True
    except Exception as e:
        print(f"✗ API check failed: {e}")
        return False


def main():
    print("=== clio-mcp Verification ===\n")
    ok = check_config() and check_api()
    if ok:
        print("\n✓ All checks passed. clio-mcp is ready.")
    else:
        print("\n✗ Setup incomplete. Check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
