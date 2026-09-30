#!/bin/bash
# Check that karellen-qbo-mcp prerequisites are available.
# Runs once on SessionStart. Reports missing dependencies via JSON output.

WARNINGS=""

check_command() {
  local cmd="$1"
  local install_hint="$2"
  local path
  path=$(command -v "$cmd" 2>/dev/null)
  if [ -z "$path" ]; then
    WARNINGS="${WARNINGS}- ${cmd} is not installed. ${install_hint}"$'\n'
  elif [ ! -x "$path" ]; then
    WARNINGS="${WARNINGS}- ${cmd} found at ${path} but is not executable. Run: chmod +x ${path}"$'\n'
  fi
}

check_command "karellen-qbo-mcp" "Run: pip install karellen-qbo-mcp"

[ -z "$WARNINGS" ] && exit 0

if ! command -v jq >/dev/null 2>&1; then
  # Without jq, plain text on stdout still reaches the session as SessionStart context.
  printf 'karellen-qbo-mcp plugin prerequisites are missing:\n%sThe QuickBooks tools will not work until these are resolved.\n' "$WARNINGS"
  exit 0
fi

jq -n --arg warnings "$WARNINGS" '{
  systemMessage: ("karellen-qbo-mcp plugin: missing prerequisites:\n" + $warnings),
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("karellen-qbo-mcp plugin prerequisites are missing:\n" + $warnings + "The QuickBooks tools will not work until these are resolved.")
  }
}'
