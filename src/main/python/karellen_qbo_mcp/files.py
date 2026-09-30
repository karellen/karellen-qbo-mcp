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
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_private_file(path: Path, data: bytes, overwrite: bool = True):
    """Atomically put `data` at `path`, readable by the owner only.

    With overwrite=False an existing file is never replaced, not even one created while the data was being
    written: FileExistsError is raised instead.
    """
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)  # atomic, and fails if path exists
            except FileExistsError:
                raise
            except OSError:  # no hard links on this filesystem: create exclusively instead
                _write_new_file(path, data)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_new_file(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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
