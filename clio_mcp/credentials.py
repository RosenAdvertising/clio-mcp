#!/usr/bin/env python3
"""Pluggable credential storage for clio-mcp.

Secrets (OAuth client credentials, access tokens, refresh tokens) are stored
in the operating system's native secret store via the cross-platform
``keyring`` library:

  * macOS    -> Keychain
  * Windows  -> Credential Manager
  * Linux    -> Secret Service (GNOME Keyring / KWallet)

Storage order (read):  OS keyring  ->  process env  ->  ~/.clio-mcp/.env file
Storage order (write):  OS keyring  (falls back to the 0600 .env file only when
                                     keyring is unavailable or disabled)

The file fallback keeps the historical chmod-0600 ``.env`` behaviour. The
``tokens.json`` fallback is additionally encrypted with Fernet whenever
``CLIO_MCP_TOKEN_KEY`` is set (enabling optional local encryption).

Token cache:
  A version-2 envelope is atomically stored under ``"tokens.json"``. Local
  stdio uses the reserved ``local`` subject and may adopt a legacy singleton
  token record. Generation checks protect refreshes from credential rotation.

Pluggable: keyring backends are user-selectable. Point at any backend (e.g.
``keyrings.cryptfile`` for an encrypted file, or a cloud backend) by setting
the standard ``PYTHON_KEYRING_BACKEND`` environment variable or a
``keyringrc.cfg``. See https://github.com/jaraco/keyring#configuring for
details.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import hashlib
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

# --- per-MCP configuration --------------------------------------------------
SERVICE_NAME = "clio-mcp"


def _default_config_dir() -> Path:
    configured = os.environ.get("CLIO_MCP_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".clio-mcp"


CONFIG_DIR = _default_config_dir()
ENV_FILE = CONFIG_DIR / ".env"
TOKEN_FILE = CONFIG_DIR / "tokens.json"
CREDENTIAL_STATE_FILE = CONFIG_DIR / "credential_state.json"
_TOKEN_CACHE_KEY = "tokens.json"
_USE_KEYRING_FLAG = "CLIO_MCP_USE_KEYRING"
TOKEN_KEY_ENV = "CLIO_MCP_TOKEN_KEY"
TOKEN_KEY_RECOVERY_ENV = "CLIO_MCP_TOKEN_KEY_RECOVERY"
LOCAL_SUBJECT = "local"
LOCAL_EMAIL = "local"
CREDENTIAL_DEAD_MESSAGE = (
    "Your Clio connection needs re-authorization. Run clio-mcp-setup."
)
logger = logging.getLogger(__name__)
_credential_store_lock = threading.RLock()
_refresh_locks_lock = threading.Lock()
_refresh_locks: dict[str, threading.RLock] = {}
# ---------------------------------------------------------------------------


class CredentialStoreConfigError(RuntimeError):
    """Raised when encrypted credential storage is misconfigured."""


class CredentialStateClearError(RuntimeError):
    """Raised when a pending grant cannot be made readable and healthy."""


def _token_fernet(*, required: bool = False) -> Fernet | None:
    """Build the token-file cipher from the environment."""
    raw_key = os.environ.get(TOKEN_KEY_ENV, "").strip()
    if not raw_key:
        if required:
            raise CredentialStoreConfigError(
                f"{TOKEN_KEY_ENV} is required for encrypted token storage"
            )
        return None
    try:
        return Fernet(raw_key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise CredentialStoreConfigError(
            f"{TOKEN_KEY_ENV} must be a valid Fernet key"
        ) from exc


# keyring is an optional-at-runtime dependency: import defensively so a missing
# or broken backend degrades to file storage instead of crashing the server.
# The module is held as ``Any`` because whether it exists and has a usable
# backend is a runtime fact (gated by ``_keyring_enabled()``), not something the
# type checker can prove — so attribute access on it is intentionally untyped.
keyring: Any
try:  # pragma: no cover - import guard
    import keyring as _keyring_mod

    keyring = _keyring_mod
    _KEYRING_IMPORTED = True
except Exception:  # noqa: BLE001 - any import failure means "no keyring"
    keyring = None
    _KEYRING_IMPORTED = False


def _keyring_enabled() -> bool:
    """True when keyring is importable, has a usable backend, and not opted out.

    Opt out with ``CLIO_MCP_USE_KEYRING=0`` (or false/no/off). A failing or
    "null" backend (common on headless Linux) is treated as disabled so we fall
    back to the file store rather than raising.
    """
    if not _KEYRING_IMPORTED:
        return False
    flag = os.environ.get(_USE_KEYRING_FLAG, "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return False
    # keyring.backends.fail.Keyring / .null.Keyring are non-functional sentinels.
    cls = backend.__class__.__module__ + "." + backend.__class__.__name__
    if "fail." in cls or "null." in cls:
        return False
    return True


def _read_private_file(path: Path, *, missing_ok: bool = False) -> bytes | None:
    """Verify every read, repairing legacy modes under the store lock."""
    with _store_transaction():
        directory_fd = _private_directory(path.parent)
        fd = None
        try:
            try:
                fd = _open_private(path, os.O_RDONLY, directory_fd)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise
            _verify_private(fd)
            with os.fdopen(fd, "rb") as stream:
                fd = None
                return stream.read()
        except OSError:
            raise CredentialStoreConfigError(
                "Unable to safely read private credential storage"
            ) from None
        finally:
            if fd is not None:
                os.close(fd)
            if directory_fd is not None:
                os.close(directory_fd)


def _read_env_file() -> dict[str, str]:
    """Only a missing entry is absent; dangling symlinks fail closed."""
    raw = _read_private_file(ENV_FILE, missing_ok=True)
    if raw is None:
        return {}
    try:
        lines = raw.decode().splitlines()
    except UnicodeError:
        raise CredentialStoreConfigError(
            "Invalid local credential file encoding"
        ) from None
    values = {}
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    return values


def _write_env_file(values: dict[str, str]) -> None:
    """Atomically write the fallback .env as a private, regular file."""
    if any("\n" in k or "\n" in v or "\r" in k or "\r" in v for k, v in values.items()):
        raise CredentialStoreConfigError("Credential values cannot contain newlines")
    lines = [f"{k}={v}" for k, v in values.items()]
    _secure_write(ENV_FILE, ("\n".join(lines) + ("\n" if lines else "")).encode())


def _prepare_keyring_fallback(key: str) -> None:
    """Remove a stale OS value before a file write; otherwise fail closed."""
    logger.warning(
        "OS keyring write failed; falling back to private local file storage"
    )
    try:
        if keyring.get_password(SERVICE_NAME, key):
            keyring.delete_password(SERVICE_NAME, key)
        if keyring.get_password(SERVICE_NAME, key):
            raise RuntimeError("stale value remains")
    except Exception:
        raise CredentialStoreConfigError(
            "Cannot safely fall back from the OS keyring; unlock it and retry setup"
        ) from None


def get_secret(key: str, default: str = "") -> str:
    """Return a credential by env-var name.

    Resolution order: OS keyring -> process environment -> ``.env`` file ->
    ``default``. An already-set process env var wins over the file (matching
    the historical ``os.environ.setdefault`` precedence) but keyring wins over
    all, so a rotated/most-recent secret is always preferred.
    """
    if _keyring_enabled():
        try:
            val = keyring.get_password(SERVICE_NAME, key)
            if val:
                return val
        except Exception:
            pass  # fall through to env/file
    # process env set by the parent shell takes next precedence
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    file_val = _read_env_file().get(key)
    if file_val:
        return file_val
    return default


def set_secret(key: str, value: str) -> str:
    """Persist a secret and report the backend actually used."""
    with _store_transaction():
        if _keyring_enabled():
            try:
                keyring.set_password(SERVICE_NAME, key, value)
                if keyring.get_password(SERVICE_NAME, key) != value:
                    raise RuntimeError("unverified keyring write")
            except Exception:
                _prepare_keyring_fallback(key)
            else:
                existing = _read_env_file()
                if key in existing:
                    existing.pop(key)
                    _write_env_file(existing)
                return "keyring"
        existing = _read_env_file()
        existing[key] = value
        _write_env_file(existing)
        return "file"


def load_into_environ(keys: list[str]) -> None:
    """Populate ``os.environ`` (via setdefault) for the given keys.

    Drop-in replacement for the old ``_load_env()`` so existing module-level
    ``os.environ.get(...)`` reads keep working, but values now resolve through
    keyring first. Only sets a key if not already present in the environment.
    """
    for key in keys:
        if os.environ.get(key):
            continue
        val = get_secret(key)
        if val:
            os.environ[key] = val


def storage_backend() -> str:
    """Human-readable name of the active backend, for setup/verify output."""
    if _keyring_enabled():
        try:
            return keyring.get_keyring().__class__.__name__
        except Exception:  # noqa: BLE001
            return "keyring"
    return f"file ({ENV_FILE})"


# ---------------------------------------------------------------------------
# Atomic private-file writer
# ---------------------------------------------------------------------------


def _posix_files() -> bool:
    return os.name != "nt" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


def _check_identity(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or (not directory and info.st_nlink != 1):
        raise CredentialStoreConfigError(
            "Private storage must be regular and have a single link"
        )
    if _posix_files() and info.st_uid != os.getuid():
        raise CredentialStoreConfigError(
            "Private storage must be owned by the current user"
        )


def _verify_private(fd):
    _check_identity(os.fstat(fd))
    if _posix_files():
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        _check_identity(info)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise CredentialStoreConfigError(
                "Cannot enforce private credential file permissions"
            )


def _private_directory(path: Path) -> int | None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_identity(path.lstat(), directory=True)
    if not _posix_files():
        return None
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        _check_identity(os.fstat(fd), directory=True)
        os.fchmod(fd, 0o700)
        final = os.fstat(fd)
        _check_identity(final, directory=True)
        if stat.S_IMODE(final.st_mode) != 0o700:
            raise CredentialStoreConfigError(
                "Cannot enforce private directory permissions"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_private(path: Path, flags: int, directory_fd: int | None):
    # Windows has no directory-descriptor opens: lstat before and after open.
    before = None
    if directory_fd is None:
        try:
            before = path.lstat()
            _check_identity(before)
        except FileNotFoundError:
            pass
    fd = os.open(
        path.name if directory_fd is not None else path,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        0o600,
        **({"dir_fd": directory_fd} if directory_fd is not None else {}),
    )
    try:
        info = os.fstat(fd)
        _check_identity(info)
        if directory_fd is None:
            after = path.lstat()
            _check_identity(after)
            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or (
                before is not None
                and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise CredentialStoreConfigError(
                    "Private storage changed while opening"
                )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _secure_write(path: Path, payload: bytes) -> None:
    """Atomically write private files using the platform's safe-open subset."""
    directory_fd = _private_directory(path.parent)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(16)}")
    try:
        try:
            old = (
                path.lstat()
                if directory_fd is None
                else os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            )
        except FileNotFoundError:
            pass
        else:
            _check_identity(old)
        fd = _open_private(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory_fd)
        with os.fdopen(fd, "wb") as stream:
            _verify_private(stream.fileno())
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if directory_fd is None:
            os.replace(temp, path)
        else:
            os.replace(
                temp.name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        fd = _open_private(path, os.O_RDONLY, directory_fd)
        try:
            _verify_private(fd)
        finally:
            os.close(fd)
    finally:
        try:
            if directory_fd is None:
                temp.unlink()
            else:
                os.unlink(temp.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if directory_fd is not None:
            os.close(directory_fd)


_process_lock_depth = threading.local()


def _os_lock(fd, *, unlock=False):
    """One nonblocking cross-process lock operation on POSIX or Windows."""
    try:
        import fcntl
    except ImportError:
        import msvcrt

        # A real byte keeps CRT locking portable even for newly created locks.
        if not unlock and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _process_lock(name: str) -> Iterator[None]:
    """Reentrant OS lock shared by processes; never replace the lock inode."""
    depths = getattr(_process_lock_depth, "held", None)
    if depths is None:
        depths = _process_lock_depth.held = {}
    identity = (str(CONFIG_DIR.resolve()), name)
    if identity in depths:
        yield
        return
    directory_fd = _private_directory(CONFIG_DIR)
    fd = None
    try:
        path = CONFIG_DIR / name
        try:
            fd = _open_private(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, directory_fd)
        except FileExistsError:
            fd = _open_private(path, os.O_RDWR, directory_fd)
        deadline = time.monotonic() + 90
        while True:
            try:
                _os_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CredentialStoreConfigError(
                        "Timed out waiting for the local credential lock"
                    ) from None
                time.sleep(0.01)
        depths[identity] = True
        try:
            if name == "store.lock":
                _verify_private(fd)
            else:
                with _store_transaction():
                    _verify_private(fd)
            yield
        finally:
            depths.pop(identity, None)
            _os_lock(fd, unlock=True)
    finally:
        if fd is not None:
            os.close(fd)
        if directory_fd is not None:
            os.close(directory_fd)


@contextmanager
def _store_transaction() -> Iterator[None]:
    with _credential_store_lock, _process_lock("store.lock"):
        yield


# ---------------------------------------------------------------------------
# Per-user token envelope
# ---------------------------------------------------------------------------


def _empty_token_envelope() -> dict[str, Any]:
    return {"version": 2, "users": {}}


def _validate_subject(sub: str) -> str:
    if not isinstance(sub, str) or not sub.strip() or len(sub) > 512:
        raise CredentialStoreConfigError(
            "credential subject must be a non-empty string"
        )
    return sub


def _validate_email(email: str) -> str:
    if not isinstance(email, str) or not email.strip() or len(email) > 320:
        raise CredentialStoreConfigError("credential email must be a non-empty string")
    return email.strip().lower()


def _normalize_token_envelope(value: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CredentialStoreConfigError(
            f"The Clio token cache in {source} must contain a JSON object"
        )
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or not isinstance(value.get("users"), dict)
        or set(value) != {"version", "users"}
    ):
        if "version" not in value:
            # Local stdio has one intentional reserved actor and may safely
            # adopt its own pre-v2 token cache.
            return {
                "version": 2,
                "users": {
                    LOCAL_SUBJECT: {
                        "email": LOCAL_EMAIL,
                        "generation": 1,
                        "tokens": dict(value),
                    }
                },
            }
        raise CredentialStoreConfigError(
            f"The Clio token cache in {source} is not a version-2 per-user "
            "envelope. Run clio-mcp-setup to authorize again."
        )
    users: dict[str, Any] = {}
    for raw_sub, raw_record in value["users"].items():
        sub = _validate_subject(raw_sub)
        if not isinstance(raw_record, dict):
            raise CredentialStoreConfigError(
                f"The Clio token cache in {source} contains an invalid user record"
            )
        if not {"email", "generation", "tokens"}.issubset(raw_record) or not set(
            raw_record
        ).issubset({"email", "generation", "tokens", "created", "last_refresh"}):
            raise CredentialStoreConfigError(
                f"The Clio token cache in {source} contains an invalid user record"
            )
        email = raw_record.get("email")
        generation = raw_record.get("generation")
        tokens = raw_record.get("tokens")
        if (
            not isinstance(email, str)
            or not email.strip()
            or type(generation) is not int
            or generation < 0
            or not isinstance(tokens, dict)
            or not all(isinstance(key, str) for key in tokens)
        ):
            raise CredentialStoreConfigError(
                f"The Clio token cache in {source} contains an invalid user record"
            )
        normalized = {
            "email": email.strip().lower(),
            "generation": generation,
            "tokens": dict(tokens),
        }
        for field in ("created", "last_refresh"):
            field_value = raw_record.get(field)
            if field_value is not None and not isinstance(field_value, str):
                raise CredentialStoreConfigError(
                    f"The Clio token cache in {source} contains an invalid {field}"
                )
            if field_value is not None:
                normalized[field] = field_value
        users[sub] = normalized
    return {"version": 2, "users": users}


def _parse_token_envelope(payload: bytes, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialStoreConfigError(
            f"The Clio token cache in {source} is not valid JSON"
        ) from exc
    return _normalize_token_envelope(value, source=source)


def _unreadable_ciphertext_error() -> CredentialStoreConfigError:
    return CredentialStoreConfigError(
        f"Cannot decrypt {TOKEN_FILE} with {TOKEN_KEY_ENV}. Refusing to start or "
        "overwrite the existing credential store. Restore the correct key, or "
        f"set {TOKEN_KEY_RECOVERY_ENV}=1 once to quarantine the unreadable file "
        "and then run clio-mcp-setup."
    )


def _quarantine_unreadable_file(path: Path, *, event: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.unreadable-{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.unreadable-{timestamp}-{counter}")
        counter += 1
    os.replace(path, candidate)
    try:
        candidate.chmod(0o600)
    except OSError:
        pass
    logger.critical(
        "Quarantined unreadable encrypted Clio state under explicit recovery mode",
        extra={
            "event": event,
            "quarantine_path": str(candidate),
        },
    )
    return candidate


def _read_file_token_envelope(
    cipher: Fernet | None = None,
    *,
    migrate_plaintext: bool = True,
) -> dict[str, Any] | None:
    try:
        raw = _read_private_file(TOKEN_FILE, missing_ok=True)
        if raw is None:
            return None
    except OSError as exc:
        raise CredentialStoreConfigError(
            f"Unable to read the Clio token cache at {TOKEN_FILE}"
        ) from exc

    active_cipher = cipher or _token_fernet()
    was_plaintext = False
    try:
        raw_value = json.loads(raw)
        was_plaintext = True
        envelope = _normalize_token_envelope(raw_value, source=str(TOKEN_FILE))
    except (json.JSONDecodeError, UnicodeDecodeError):
        if active_cipher is None:
            raise CredentialStoreConfigError(
                f"Cannot decrypt {TOKEN_FILE}: {TOKEN_KEY_ENV} is not set"
            )
        try:
            plaintext = active_cipher.decrypt(raw)
        except InvalidToken:
            if os.environ.get(TOKEN_KEY_RECOVERY_ENV, "").strip() == "1":
                _quarantine_unreadable_file(
                    TOKEN_FILE, event="clio_token_key_recovery_quarantine"
                )
                return None
            raise _unreadable_ciphertext_error() from None
        envelope = _parse_token_envelope(plaintext, source=str(TOKEN_FILE))

    if was_plaintext and migrate_plaintext and active_cipher is not None:
        payload = json.dumps(envelope, indent=2).encode("utf-8")
        encrypted = active_cipher.encrypt(payload)
        if active_cipher.decrypt(encrypted) != payload:  # pragma: no cover
            raise CredentialStoreConfigError(
                "Unable to verify encrypted Clio token migration"
            )
        _secure_write(TOKEN_FILE, encrypted)
        try:
            verified = _parse_token_envelope(
                active_cipher.decrypt(_read_private_file(TOKEN_FILE)),
                source=str(TOKEN_FILE),
            )
        except (InvalidToken, OSError) as exc:  # pragma: no cover
            raise CredentialStoreConfigError(
                "Encrypted Clio token migration could not be verified"
            ) from exc
        if verified != envelope:
            raise CredentialStoreConfigError(
                "Encrypted Clio token migration changed the token envelope"
            )
    return envelope


def _get_token_envelope_unlocked() -> dict[str, Any]:
    if _keyring_enabled():
        try:
            raw = keyring.get_password(SERVICE_NAME, _TOKEN_CACHE_KEY)
            if raw:
                return _parse_token_envelope(
                    raw.encode("utf-8"), source="the OS keyring"
                )
        except CredentialStoreConfigError:
            raise
        except Exception:
            pass
    return _read_file_token_envelope() or _empty_token_envelope()


def _write_token_envelope_unlocked(envelope: dict[str, Any]) -> str:
    normalized = _normalize_token_envelope(envelope, source="the pending write")
    payload = json.dumps(normalized, indent=2).encode("utf-8")
    if _keyring_enabled():
        try:
            keyring.set_password(
                SERVICE_NAME, _TOKEN_CACHE_KEY, payload.decode("utf-8")
            )
            verified = keyring.get_password(SERVICE_NAME, _TOKEN_CACHE_KEY)
            if (
                not verified
                or _parse_token_envelope(
                    verified.encode("utf-8"), source="the OS keyring"
                )
                != normalized
            ):
                raise CredentialStoreConfigError(
                    "Unable to verify the Clio token envelope keyring write"
                )
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
            return "keyring"
        except CredentialStoreConfigError:
            raise
        except Exception:
            _prepare_keyring_fallback(_TOKEN_CACHE_KEY)

    cipher = _token_fernet()
    disk_payload = cipher.encrypt(payload) if cipher is not None else payload
    _secure_write(TOKEN_FILE, disk_payload)
    try:
        verified_payload = _read_private_file(TOKEN_FILE)
        if cipher is not None:
            verified_payload = cipher.decrypt(verified_payload)
        verified = _parse_token_envelope(verified_payload, source=str(TOKEN_FILE))
    except (OSError, InvalidToken) as exc:  # pragma: no cover - disk failure
        raise CredentialStoreConfigError(
            "Unable to verify the Clio token envelope file write"
        ) from exc
    if verified != normalized:
        raise CredentialStoreConfigError(
            "The verified Clio token envelope differs from the pending write"
        )
    return "file"


def _refresh_lock(sub: str) -> threading.RLock:
    _validate_subject(sub)
    with _refresh_locks_lock:
        return _refresh_locks.setdefault(sub, threading.RLock())


@contextmanager
def credential_transaction(sub: str) -> Iterator[None]:
    """Hold the target user's refresh lock across its Clio HTTP request."""
    name = "refresh-" + hashlib.sha256(sub.encode()).hexdigest() + ".lock"
    with _refresh_lock(sub), _process_lock(name):
        yield


def credential_subjects() -> list[str]:
    with _store_transaction():
        return sorted(_get_token_envelope_unlocked()["users"])


def credential_email(sub: str) -> str | None:
    with _store_transaction():
        record = _get_token_envelope_unlocked()["users"].get(_validate_subject(sub))
        return str(record["email"]) if record else None


def _pending_marker_blocks_generation(
    state_record: dict[str, Any], generation: int
) -> bool:
    marker_generation = state_record.get("credential_generation")
    return bool(
        state_record.get("grant_pending") is True
        and type(marker_generation) is int
        and marker_generation >= generation
    )


def credential_snapshot(sub: str) -> tuple[dict[str, Any], int]:
    with _store_transaction():
        record = _get_token_envelope_unlocked()["users"].get(_validate_subject(sub))
        if record is None:
            return {}, 0
        generation = int(record["generation"])
        state_record = _read_credential_state()["users"].get(sub, {})
        if _pending_marker_blocks_generation(state_record, generation):
            return {}, generation
        return dict(record["tokens"]), generation


def credential_generation(sub: str) -> int:
    """Return the persisted generation even when a pending grant is fail-closed."""
    with _store_transaction():
        record = _get_token_envelope_unlocked()["users"].get(_validate_subject(sub))
        return int(record["generation"]) if record else 0


def get_token_cache(sub: str) -> dict[str, Any] | None:
    tokens, _generation = credential_snapshot(sub)
    return tokens or None


def set_token_cache(sub: str, email: str, tokens: dict[str, Any]) -> str:
    sub = _validate_subject(sub)
    email = _validate_email(email)
    if not isinstance(tokens, dict):
        raise CredentialStoreConfigError("tokens must be a JSON object")
    now = datetime.now(timezone.utc).isoformat()
    with _store_transaction():
        envelope = _get_token_envelope_unlocked()
        current = envelope["users"].get(sub)
        envelope["users"][sub] = {
            "email": email,
            "generation": int(current["generation"]) + 1 if current else 1,
            "tokens": dict(tokens),
            "created": current.get("created", now) if current else now,
            "last_refresh": now,
        }
        return _write_token_envelope_unlocked(envelope)


def replace_token_cache_if_generation(
    sub: str, tokens: dict[str, Any], expected_generation: int
) -> tuple[bool, str, int]:
    sub = _validate_subject(sub)
    with _store_transaction():
        envelope = _get_token_envelope_unlocked()
        current = envelope["users"].get(sub)
        generation = int(current["generation"]) if current else 0
        if current is None or generation != expected_generation:
            return False, "", generation
        current["tokens"] = dict(tokens)
        current["generation"] = generation + 1
        current["last_refresh"] = datetime.now(timezone.utc).isoformat()
        backend = _write_token_envelope_unlocked(envelope)
        return True, backend, generation + 1


def _empty_state_envelope() -> dict[str, Any]:
    return {"version": 2, "users": {}}


def _read_credential_state() -> dict[str, Any]:
    try:
        raw = _read_private_file(CREDENTIAL_STATE_FILE, missing_ok=True)
        if raw is None:
            return _empty_state_envelope()
        value = json.loads(raw.decode())
    except (ValueError, UnicodeError, OSError):
        raise CredentialStoreConfigError(
            "Unable to read the Clio credential state"
        ) from None
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or not isinstance(value.get("users"), dict)
        or set(value) != {"version", "users"}
    ):
        raise CredentialStoreConfigError(
            f"The Clio credential state in {CREDENTIAL_STATE_FILE} is not version 2"
        )
    required_fields = {"email", "credential_dead", "credential_generation"}
    # Read legacy alert metadata for existing version-2 local state compatibility.
    # No alert delivery or new alert metadata is written.
    optional_string_fields = {
        "detail",
        "detected_at",
        "last_alert_attempt_at",
        "next_alert_attempt_at",
        "last_alert_delivered_at",
        "last_alert_at",
    }
    optional_count_fields = {"alert_attempt_count", "alert_failure_count"}
    optional_bool_fields = {"grant_pending"}
    allowed_fields = (
        required_fields
        | optional_string_fields
        | optional_count_fields
        | optional_bool_fields
    )
    for sub, record in value["users"].items():
        if (
            not isinstance(sub, str)
            or not sub
            or not isinstance(record, dict)
            or not required_fields.issubset(record)
            or not set(record).issubset(allowed_fields)
            or not isinstance(record.get("email"), str)
            or not record["email"]
            or type(record.get("credential_dead")) is not bool
            or type(record.get("credential_generation")) is not int
            or record["credential_generation"] < 0
            or any(
                field in record
                and (not isinstance(record[field], str) or not record[field])
                for field in optional_string_fields
            )
            or any(
                field in record
                and (type(record[field]) is not int or record[field] < 0)
                for field in optional_count_fields
            )
            or any(
                field in record and type(record[field]) is not bool
                for field in optional_bool_fields
            )
        ):
            raise CredentialStoreConfigError(
                f"The Clio credential state in {CREDENTIAL_STATE_FILE} contains "
                "an invalid user record"
            )
    return value


def _write_credential_state(state: dict[str, Any]) -> None:
    _secure_write(CREDENTIAL_STATE_FILE, json.dumps(state, indent=2).encode("utf-8"))


def delete_token_cache(sub: str) -> None:
    sub = _validate_subject(sub)
    with credential_transaction(sub), _store_transaction():
        envelope = _get_token_envelope_unlocked()
        if envelope["users"].pop(sub, None) is not None:
            _write_token_envelope_unlocked(envelope)
        state = _read_credential_state()
        if state["users"].pop(sub, None) is not None:
            _write_credential_state(state)
    with _refresh_locks_lock:
        _refresh_locks.pop(sub, None)


def credential_is_dead(sub: str) -> bool:
    with _store_transaction():
        state = _read_credential_state()
        tokens = _get_token_envelope_unlocked()["users"].get(_validate_subject(sub))
        state_record = state["users"].get(sub)
        if not state_record:
            return False
        generation = int(tokens["generation"]) if tokens else 0
        if _pending_marker_blocks_generation(state_record, generation):
            return True
        return bool(
            tokens
            and state_record["credential_dead"]
            and state_record["credential_generation"] == generation
        )


def mark_credential_dead(
    sub: str,
    detail: str,
    *,
    expected_generation: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark a rejected local grant dead only if its generation still matches."""
    sub = _validate_subject(sub)
    event_time = now or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    with _store_transaction():
        token_record = _get_token_envelope_unlocked()["users"].get(sub)
        generation = int(token_record["generation"]) if token_record else 0
        email = str(token_record["email"]) if token_record else sub
        if expected_generation is not None and generation != expected_generation:
            logger.warning(
                "Discarded a stale Clio credential-death result",
                extra={"event": "clio_credential_dead_stale_discarded"},
            )
            return False
        state = _read_credential_state()
        state["users"][sub] = {
            "email": email,
            "credential_dead": True,
            "credential_generation": generation,
            "detail": detail,
            "detected_at": event_time.astimezone(timezone.utc).isoformat(),
        }
        _write_credential_state(state)
    logger.error("Clio credential marked dead", extra={"event": "clio_credential_dead"})
    return True


def clear_credential_dead(sub: str) -> None:
    sub = _validate_subject(sub)
    with _store_transaction():
        token_record = _get_token_envelope_unlocked()["users"].get(sub)
        generation = int(token_record["generation"]) if token_record else 0
        email = str(token_record["email"]) if token_record else sub
        state = _read_credential_state()
        try:
            state["users"][sub] = {
                "email": email,
                "credential_dead": False,
                "credential_generation": generation,
            }
            _write_credential_state(state)
        except OSError:
            logger.critical(
                "Unable to persist cleared Clio credential state",
                extra={"event": "clio_credential_state_clear_failed"},
            )
            raise


def commit_authorization_grant(sub: str, email: str, tokens: dict[str, Any]) -> str:
    """Commit a grant behind a durable marker that fail-closes partial writes."""
    sub = _validate_subject(sub)
    email = _validate_email(email)
    now = datetime.now(timezone.utc).isoformat()
    with credential_transaction(sub), _store_transaction():
        envelope = _get_token_envelope_unlocked()
        current = envelope["users"].get(sub)
        state = _read_credential_state()
        prior_generation = state["users"].get(sub, {}).get("credential_generation", 0)
        generation = (
            max(int(current["generation"]) if current else 0, prior_generation) + 1
        )
        state["users"][sub] = {
            "email": email,
            "credential_dead": True,
            "credential_generation": generation,
            "grant_pending": True,
            "detail": "authorization grant commit is pending recovery",
        }
        try:
            _write_credential_state(state)
        except (OSError, CredentialStoreConfigError) as exc:
            raise CredentialStateClearError(
                "failed to persist the pending grant marker"
            ) from exc
        envelope["users"][sub] = {
            "email": email,
            "generation": generation,
            "tokens": dict(tokens),
            "created": current.get("created", now) if current else now,
            "last_refresh": now,
        }
        backend = _write_token_envelope_unlocked(envelope)
        try:
            state["users"][sub] = {
                "email": email,
                "credential_dead": False,
                "credential_generation": generation,
            }
            _write_credential_state(state)
        except (OSError, CredentialStoreConfigError) as exc:
            logger.critical(
                "A Clio grant remains fail-closed behind its pending marker",
                extra={"event": "clio_grant_state_cleanup_failed"},
            )
            raise CredentialStateClearError(
                "stored grant remains pending and unusable"
            ) from exc
        return backend


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppCredentialPair:
    """A deliberately non-representable in-memory Clio application credential."""

    client_id: str = dataclass_field(repr=False)
    client_secret: str = dataclass_field(repr=False)
    generation: int


def resolve_app_credentials() -> AppCredentialPair | None:
    """Resolve local app credentials through keyring, environment, and file."""
    client_id = get_secret("CLIO_CLIENT_ID")
    client_secret = get_secret("CLIO_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return AppCredentialPair(client_id, client_secret, 0)
