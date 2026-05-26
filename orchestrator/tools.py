"""Tool definitions and executors for the Copilot Agent backend.

Each tool is exposed to the LLM via OpenAI function-calling format.
`execute_tool()` is the single dispatch entry point.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

# ── Limits ────────────────────────────────────────────────────────────────────

_MAX_READ_BYTES = 1 * 1024 * 1024   # 1 MB per file read
_MAX_OUTPUT_BYTES = 200 * 1024      # 200 KB for bash / search output
_MAX_BASH_TIMEOUT = 120             # seconds hard cap

# ── Tool schemas (OpenAI function-calling format) ─────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the full text contents of a file. "
                "Returns the file text or an error message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file, creating it or overwriting it entirely. "
                "Parent directories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_file",
            "description": (
                "Replace one exact occurrence of old_str with new_str inside a file. "
                "Fails if old_str is not found or appears more than once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The exact string to find (must appear exactly once).",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The replacement string.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory and all intermediate parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory to create.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the contents of a directory, showing files and subdirectories with sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory to list.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a shell command and return its stdout and stderr. "
                "Use for running tests, compilers, linters, git operations, package managers, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command (optional, defaults to job working_dir).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for a text pattern in files using grep. "
                "Returns matching lines with file paths and line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The text or regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in (defaults to current directory).",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "File glob to filter files, e.g. '*.py' (optional).",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether to do a case-sensitive search (default true).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

# ── Path helper ───────────────────────────────────────────────────────────────


def _resolve(path: str, cwd: str | None) -> str:
    """Resolve a path relative to cwd if not absolute."""
    if os.path.isabs(path):
        return path
    return str(Path(cwd or os.getcwd()) / path)


# ── Executors ─────────────────────────────────────────────────────────────────


def tool_read_file(path: str, cwd: str | None = None) -> str:
    resolved = _resolve(path, cwd)
    try:
        size = os.path.getsize(resolved)
        if size > _MAX_READ_BYTES:
            return f"Error: file too large ({size} bytes, max {_MAX_READ_BYTES}). Use search_files or run_bash with head/tail."
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file not found: {resolved}"
    except IsADirectoryError:
        return f"Error: path is a directory — use list_directory instead: {resolved}"
    except OSError as e:
        return f"Error reading file: {e}"


def tool_write_file(path: str, content: str, cwd: str | None = None) -> str:
    resolved = _resolve(path, cwd)
    try:
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} characters to {resolved}"
    except OSError as e:
        return f"Error writing file: {e}"


def tool_str_replace_file(
    path: str, old_str: str, new_str: str, cwd: str | None = None
) -> str:
    resolved = _resolve(path, cwd)
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            text = f.read()
        count = text.count(old_str)
        if count == 0:
            return f"Error: old_str not found in {resolved}"
        if count > 1:
            return f"Error: old_str appears {count} times in {resolved} (must be exactly 1)"
        new_text = text.replace(old_str, new_str, 1)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_text)
        return f"Replaced 1 occurrence in {resolved}"
    except FileNotFoundError:
        return f"Error: file not found: {resolved}"
    except OSError as e:
        return f"Error: {e}"


def tool_create_directory(path: str, cwd: str | None = None) -> str:
    resolved = _resolve(path, cwd)
    try:
        os.makedirs(resolved, exist_ok=True)
        return f"Created directory: {resolved}"
    except OSError as e:
        return f"Error creating directory: {e}"


def tool_list_directory(path: str, cwd: str | None = None) -> str:
    resolved = _resolve(path, cwd)
    try:
        entries = sorted(os.scandir(resolved), key=lambda e: (not e.is_dir(), e.name))
        lines = []
        for e in entries:
            prefix = "d" if e.is_dir() else "f"
            try:
                size = "" if e.is_dir() else f"  ({e.stat().st_size} bytes)"
            except OSError:
                size = ""
            lines.append(f"[{prefix}] {e.name}{size}")
        return "\n".join(lines) if lines else "(empty directory)"
    except FileNotFoundError:
        return f"Error: directory not found: {resolved}"
    except NotADirectoryError:
        return f"Error: not a directory: {resolved}"
    except OSError as e:
        return f"Error listing directory: {e}"


def tool_run_bash(
    command: str,
    cwd: str | None = None,
    timeout: int = 30,
) -> str:
    timeout = min(max(1, int(timeout)), _MAX_BASH_TIMEOUT)
    run_cwd = cwd or os.getcwd()
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=run_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        combined = out
        if err:
            combined += f"\n[stderr]\n{err}"
        if len(combined) > _MAX_OUTPUT_BYTES:
            combined = combined[:_MAX_OUTPUT_BYTES] + "\n... (truncated)"
        rc_note = "" if result.returncode == 0 else f"\n[exit code: {result.returncode}]"
        return (combined + rc_note).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except OSError as e:
        return f"Error running command: {e}"


def tool_search_files(
    pattern: str,
    path: str | None = None,
    file_glob: str | None = None,
    case_sensitive: bool = True,
    cwd: str | None = None,
) -> str:
    search_path = _resolve(path, cwd) if path else (cwd or os.getcwd())
    cmd = ["grep", "-rn", "--color=never"]
    if not case_sensitive:
        cmd.append("-i")
    if file_glob:
        cmd += ["--include", file_glob]
    cmd += [pattern, search_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = result.stdout
        if len(out) > _MAX_OUTPUT_BYTES:
            out = out[:_MAX_OUTPUT_BYTES] + "\n... (truncated)"
        return out.strip() or "(no matches)"
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except OSError as e:
        return f"Error searching: {e}"


# ── Dispatcher ────────────────────────────────────────────────────────────────


def execute_tool(name: str, arguments: dict[str, Any], cwd: str | None = None) -> str:
    """Execute a named tool call and return the string result."""
    if name == "read_file":
        return tool_read_file(arguments["path"], cwd=cwd)
    if name == "write_file":
        return tool_write_file(arguments["path"], arguments["content"], cwd=cwd)
    if name == "str_replace_file":
        return tool_str_replace_file(
            arguments["path"], arguments["old_str"], arguments["new_str"], cwd=cwd
        )
    if name == "create_directory":
        return tool_create_directory(arguments["path"], cwd=cwd)
    if name == "list_directory":
        return tool_list_directory(arguments["path"], cwd=cwd)
    if name == "run_bash":
        return tool_run_bash(
            arguments["command"],
            cwd=arguments.get("cwd") or cwd,
            timeout=int(arguments.get("timeout", 30)),
        )
    if name == "search_files":
        return tool_search_files(
            arguments["pattern"],
            path=arguments.get("path"),
            file_glob=arguments.get("file_glob"),
            case_sensitive=bool(arguments.get("case_sensitive", True)),
            cwd=cwd,
        )
    return f"Error: unknown tool '{name}'"
