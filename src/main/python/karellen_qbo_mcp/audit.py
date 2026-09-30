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

"""Append-only local logs: every write sent to QuickBooks, successful or not, and every error a tool reported."""

import datetime
import json
import logging
from pathlib import Path

from karellen_qbo_mcp.files import append_private_line

logger = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: Path, environment: str):
        self.path = path
        self.environment = environment

    def record(self, operation: str, entity: str | None, request, *, realm_id: str | None = None,
               request_id: str | None = None, result=None, error: str | None = None):
        entry = _entry(self.environment, realm_id, operation=operation, entity=entity, request_id=request_id, request=request)
        if error is not None:
            entry["error"] = error
        else:
            entry["result"] = result
        # The write already happened in QuickBooks; losing the log line must not hide that.
        _append(self.path, entry, "audit log")


class ErrorLog:
    """Every error a tool reported, with the tool's arguments, to share when troubleshooting (e.g. with Intuit)."""

    def __init__(self, path: Path, environment: str):
        self.path = path
        self.environment = environment

    def record(self, tool: str, arguments: dict, error: str, *, realm_id: str | None = None, details: dict | None = None):
        entry = _entry(self.environment, realm_id, tool=tool, arguments=arguments, error=error)
        entry.update(details or {})
        # The tool's error goes to the client regardless; losing the log line must not replace it.
        _append(self.path, entry, "error log")


def _entry(environment: str, realm_id: str | None, **fields) -> dict:
    return dict(timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(), environment=environment,
                realm_id=realm_id, **fields)


def _append(path: Path, entry: dict, what: str):
    try:
        append_private_line(path, json.dumps(entry, default=str, separators=(",", ":")))
    except OSError as e:
        logger.error("Cannot write %s %s: %s", what, path, e)
