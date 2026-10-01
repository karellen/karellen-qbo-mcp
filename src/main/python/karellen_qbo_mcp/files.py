#   -*- coding: utf-8 -*-
#   Copyright 2026 Karellen, Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Owner-only file helpers for credentials, tokens and the audit log."""

import os
import shutil
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


class PrivateFile:
    """An owner-only file written piece by piece under a temporary name, and put at `path` atomically on commit.

    As a context manager it commits when the block completes and discards the temporary file when it fails. With
    overwrite=False an existing file is never replaced, not even one created while the data was being written:
    commit raises FileExistsError instead.
    """

    def __init__(self, path: Path, overwrite: bool = True):
        self.path = path
        self.overwrite = overwrite
        self.size = 0
        ensure_private_dir(path.parent)
        fd, self._tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)  # mode 0600
        self._file = os.fdopen(fd, "wb")

    def write(self, data: bytes):
        self._file.write(data)
        self.size += len(data)

    def commit(self):
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            if self.overwrite:
                os.replace(self._tmp, self.path)
            else:
                try:
                    os.link(self._tmp, self.path)  # atomic, and fails if path exists
                except FileExistsError:
                    raise
                except OSError:  # no hard links on this filesystem: create exclusively instead
                    _copy_to_new_file(self._tmp, self.path)
                os.unlink(self._tmp)
        except BaseException:
            self.discard()
            raise

    def discard(self):
        self._file.close()
        try:
            os.unlink(self._tmp)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.discard()


def write_private_file(path: Path, data: bytes, overwrite: bool = True):
    """Atomically put `data` at `path`, readable by the owner only (see PrivateFile)."""
    with PrivateFile(path, overwrite) as f:
        f.write(data)


def _copy_to_new_file(source: str, path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f, open(source, "rb") as src:
            shutil.copyfileobj(src, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(path)
        raise


def append_private_line(path: Path, line: str):
    """Append one line to an owner-only file, creating it if needed."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
