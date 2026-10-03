"""Confined local document files with portable safe-open checks."""

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from clio_mcp import credentials

DEFAULT_MAX_BYTES = 100 * 1024 * 1024


class DocumentFileError(ValueError):
    """Safe error for a rejected local document path."""


def max_bytes() -> int:
    try:
        value = int(os.environ.get("CLIO_MCP_MAX_FILE_BYTES", DEFAULT_MAX_BYTES))
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise DocumentFileError(
            "CLIO_MCP_MAX_FILE_BYTES must be a positive integer"
        ) from None


def _same(a, b):
    if os.path.normcase(str(a)) == os.path.normcase(str(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _relative(path, root):
    parts = []
    cursor = path
    while not _same(cursor, root):
        if cursor == cursor.parent:
            return None
        parts.append(cursor.name)
        cursor = cursor.parent
    return tuple(reversed(parts))


def _no_symlinks(path):
    cursor = path
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise DocumentFileError("Symlinks are not allowed for document files")
        cursor = cursor.parent


def files_directory() -> Path:
    root = Path(os.environ.get("CLIO_MCP_FILES_DIR", "~/clio-mcp-files")).expanduser()
    if not root.is_absolute():
        raise DocumentFileError("CLIO_MCP_FILES_DIR must be absolute")
    home = Path.home().resolve()
    resolved = root.resolve()
    forbidden = [
        home / ".clio-mcp",
        home / ".ssh",
        home / ".aws",
        credentials.CONFIG_DIR.resolve(),
    ]
    if _relative(home, resolved) is not None or any(
        _relative(resolved, p) is not None or _relative(p, resolved) is not None
        for p in forbidden
    ):
        raise DocumentFileError(
            "Document directory must be separate from home and credential directories"
        )
    _no_symlinks(root)
    try:
        # Existing directories retain their permissions, including shared folders.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise DocumentFileError("CLIO_MCP_FILES_DIR must be a directory")
    except OSError:
        raise DocumentFileError("Cannot open the document directory safely") from None
    return root.resolve()


def confined_path(value: str) -> Path:
    root = files_directory()
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise DocumentFileError("Parent traversal is not allowed")
    if not path.is_absolute():
        path = root / path
    _no_symlinks(path)
    parts = _relative(path.resolve(), root)
    if not parts:
        raise DocumentFileError(
            "File must be inside CLIO_MCP_FILES_DIR (default ~/clio-mcp-files)"
        )
    return root.joinpath(*parts)


def _dir_flags():
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def _parent(path: Path):
    path = confined_path(str(path))
    fd = None
    try:
        if not path.parent.is_dir():
            raise DocumentFileError("The destination folder must exist")
        if credentials._posix_files():
            root = files_directory()
            fd = os.open(root, _dir_flags())
            for part in path.relative_to(root).parts[:-1]:
                child = os.open(part, _dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            target = path.name
        else:
            _no_symlinks(path.parent)
            target = path
    except OSError:
        if fd is not None:
            os.close(fd)
        raise DocumentFileError(
            "Cannot open the document destination safely; the folder must exist"
        ) from None
    try:
        yield fd, target
    finally:
        if fd is not None:
            os.close(fd)


def _kwargs(fd):
    return {"dir_fd": fd} if fd is not None else {}


def _open(name, flags, fd, mode=0o600):
    before = None
    if fd is None:
        try:
            before = os.lstat(name)
            if not stat.S_ISREG(before.st_mode):
                raise DocumentFileError(
                    "Document must be a regular file without symlinks"
                )
        except FileNotFoundError:
            pass
    opened = os.open(
        name,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        mode,
        **_kwargs(fd),
    )
    if fd is None:
        try:
            after = os.lstat(name)
            info = os.fstat(opened)
            if (
                not stat.S_ISREG(after.st_mode)
                or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
                or (
                    before is not None
                    and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                )
            ):
                raise DocumentFileError("Document changed while opening")
        except BaseException:
            os.close(opened)
            raise
    return opened


@contextmanager
def open_upload(value: str):
    path = confined_path(value)
    fd = None
    try:
        with _parent(path) as (directory_fd, name):
            fd = _open(name, os.O_RDONLY, directory_fd)
        info = os.fstat(fd)
    except FileNotFoundError:
        raise DocumentFileError("file not found") from None
    except OSError:
        if fd is not None:
            os.close(fd)
        raise DocumentFileError("Cannot open a regular document file safely") from None
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DocumentFileError("Upload must be a regular file without hard links")
        if info.st_size > max_bytes():
            raise DocumentFileError("Document exceeds CLIO_MCP_MAX_FILE_BYTES")
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream, info.st_size, path.name


def validate_output(value: str) -> Path:
    path = confined_path(value)
    try:
        with _parent(path) as (fd, name):
            try:
                os.stat(name, follow_symlinks=False, **_kwargs(fd))
            except FileNotFoundError:
                return path
            raise DocumentFileError("Destination exists; overwriting is not allowed")
    except OSError:
        raise DocumentFileError(
            "Cannot open the document destination safely; the folder must exist"
        ) from None


def _private_download(fd):
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    except OSError:
        pass  # Document files may live on FAT/exFAT/SMB; credentials fail closed.


def _publish(temp, name, parent_fd):
    kwargs = (
        {"src_dir_fd": parent_fd, "dst_dir_fd": parent_fd}
        if parent_fd is not None
        else {}
    )
    try:
        os.link(temp, name, follow_symlinks=False, **kwargs)
        return
    except (NotImplementedError, AttributeError):
        pass
    except OSError as exc:
        if exc.errno not in (
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EPERM,
        ):
            raise
    # Reserve exclusively before rename: never replace a pre-existing destination.
    reserve = _open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, parent_fd)
    _private_download(reserve)
    identity = os.fstat(reserve)  # fchmod can change the inode on FAT/exFAT.
    os.close(reserve)
    try:
        current = os.stat(name, follow_symlinks=False, **_kwargs(parent_fd))
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise DocumentFileError("Destination changed; overwriting is not allowed")
        os.replace(temp, name, **kwargs)
    except BaseException:
        current = os.stat(name, follow_symlinks=False, **_kwargs(parent_fd))
        if (identity.st_dev, identity.st_ino) == (current.st_dev, current.st_ino):
            os.unlink(name, **_kwargs(parent_fd))
        raise


@contextmanager
def download_target(value: str):
    path = validate_output(value)
    with _parent(path) as (parent_fd, name):
        temp = ".download-" + secrets.token_hex(16)
        if parent_fd is None:
            temp = path.parent / temp
        try:
            fd = _open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, parent_fd)
        except OSError:
            raise DocumentFileError(
                "Cannot create the document destination safely"
            ) from None
        try:
            with os.fdopen(fd, "wb") as stream:
                _private_download(stream.fileno())
                yield stream
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError:
                    raise DocumentFileError("Cannot save document safely") from None
            try:
                _publish(temp, name, parent_fd)
            except OSError:
                raise DocumentFileError(
                    "Cannot save document safely; destination may already exist"
                ) from None
        finally:
            try:
                os.unlink(temp, **_kwargs(parent_fd))
            except FileNotFoundError:
                pass
