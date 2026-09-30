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

"""Append-only local log of every write sent to QuickBooks, successful or not."""

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
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "environment": self.environment,
            "realm_id": realm_id,
            "operation": operation,
            "entity": entity,
            "request_id": request_id,
            "request": request,
        }
        if error is not None:
            entry["error"] = error
        else:
            entry["result"] = result
        try:
            append_private_line(self.path, json.dumps(entry, default=str, separators=(",", ":")))
        except OSError as e:
            # The write already happened in QuickBooks; losing the log line must not hide that.
            logger.error("Cannot write audit log %s: %s", self.path, e)
