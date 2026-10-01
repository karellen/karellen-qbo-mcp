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

from pybuilder.core import (use_plugin, init, Author)

use_plugin("python.core")
use_plugin("python.unittest")
use_plugin("python.integrationtest")
use_plugin("python.flake8")
use_plugin("python.coverage")
use_plugin("python.coveralls")
use_plugin("copy_resources")
use_plugin("python.distutils")

name = "karellen-qbo-mcp"
version = "0.0.5"

summary = "MCP Server for QuickBooks Online"
authors = [Author("Karellen, Inc.", "supervisor@karellen.co")]
maintainers = [Author("Arcadiy Ivanov", "arcadiy@karellen.co")]
url = "https://github.com/karellen/karellen-qbo-mcp"
urls = {
    "Bug Tracker": "https://github.com/karellen/karellen-qbo-mcp/issues",
    "Source Code": "https://github.com/karellen/karellen-qbo-mcp/",
    "Privacy Policy": "https://github.com/karellen/karellen-qbo-mcp/blob/master/PRIVACY.md",
}
license = "Apache-2.0"

requires_python = ">=3.10"

default_task = ["analyze", "publish"]


@init
def set_properties(project):
    project.depends_on("mcp", ">=2.2.0,<3")
    project.depends_on("httpx2")
    project.depends_on("anyio")
    project.depends_on("filelock", ">=3.15")  # AsyncFileLock
    project.depends_on("platformdirs")

    project.set_property("integrationtest_inherit_environment", True)
    # Opt-in live suite against the signed-in sandbox company: -P qbo_sandbox_tests=true
    if str(project.get_property("qbo_sandbox_tests", "")).lower() in ("1", "true", "yes", "on"):
        environment = dict(project.get_property("integrationtest_additional_environment") or {})
        environment["QBO_MCP_SANDBOX_TESTS"] = "1"
        project.set_property("integrationtest_additional_environment", environment)

    project.set_property("coverage_break_build", False)

    project.set_property("flake8_break_build", True)
    project.set_property("flake8_extend_ignore", "E303,E402")
    project.set_property("flake8_include_test_sources", True)
    project.set_property("flake8_include_scripts", True)
    project.set_property("flake8_max_line_length", 130)

    project.set_property("distutils_readme_description", True)
    project.set_property("distutils_description_overwrite", True)
    project.set_property("distutils_upload_skip_existing", True)
    project.set_property("distutils_console_scripts", ["karellen-qbo-mcp = karellen_qbo_mcp.cli:main"])
    project.set_property("distutils_setup_keywords", ["quickbooks", "quickbooks-online", "qbo", "intuit",
                                                      "accounting", "bookkeeping", "mcp",
                                                      "model-context-protocol"])

    project.set_property("copy_resources_target", "$dir_dist/karellen_qbo_mcp")
    project.get_property("copy_resources_glob").append("PRIVACY.md")
    project.include_file("karellen_qbo_mcp", "PRIVACY.md")

    project.set_property("distutils_classifiers", [
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS",
        "Environment :: Console",
        "Topic :: Office/Business :: Financial :: Accounting",
        "Intended Audience :: Financial and Insurance Industry",
        "Intended Audience :: Developers",
        "Development Status :: 3 - Alpha"
    ])
