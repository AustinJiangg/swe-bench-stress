"""
Parse OpenHands trajectories from SWE-rebench-openhands-trajectories.

Each trajectory is a list of messages with roles:
  system    : role, content
  assistant : role, content, tool_calls
  user      : role, content
  tool      : role, content, name, tool_call_id

Tool calls from the assistant have a `function` sub-object:
  {
    "id": "call_xxx",
    "type": "function",
    "function": {
      "name": "bash",          # or "str_replace_editor", "write_file", etc.
      "arguments": {...}       # already a dict after deserialization
    }
  }

We extract SandboxOp objects (ordered) that can be replayed in an E2B sandbox.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Data models                                                                  #
# --------------------------------------------------------------------------- #

class OpType(str, Enum):
    BASH = "bash"
    FILE_WRITE = "file_write"
    FILE_READ = "file_read"
    FILE_STR_REPLACE = "file_str_replace"
    UNKNOWN = "unknown"


@dataclass
class SandboxOp:
    """A single sandbox operation extracted from a trajectory."""
    op_type: OpType
    # bash
    command: str = ""
    # file ops
    path: str = ""
    content: str = ""
    old_str: str = ""
    new_str: str = ""
    insert_line: int = -1       # >=0 means insert new_str after this line number
    # metadata
    tool_call_id: str = ""
    raw_args: dict = field(default_factory=dict)

    def __str__(self) -> str:
        if self.op_type == OpType.BASH:
            short = self.command[:80].replace("\n", "\\n")
            return f"BASH: {short}"
        if self.op_type == OpType.FILE_WRITE:
            return f"WRITE: {self.path}"
        if self.op_type == OpType.FILE_READ:
            return f"READ: {self.path}"
        if self.op_type == OpType.FILE_STR_REPLACE:
            return f"STR_REPLACE: {self.path}"
        return f"UNKNOWN: {self.raw_args}"


@dataclass
class ParsedTrajectory:
    instance_id: str
    ops: list[SandboxOp]
    raw_messages: list[dict]
    n_assistant_turns: int
    n_tool_calls: int
    model_patch: str = ""           # expected patch from trajectory data
    workspace_path: str = ""        # e.g. /workspace/sdf-xarray, detected from ops


# --------------------------------------------------------------------------- #
#  Known tool name → OpType mapping                                            #
# --------------------------------------------------------------------------- #

# OpenHands tool names vary across versions; map them all.
_BASH_TOOLS = {
    "bash",
    "execute_bash",
    "run_bash",
    "shell",
    "terminal",
    "ipython",          # Jupyter-style execution
}

_WRITE_TOOLS = {
    "write_file",
    "create_file",
    "file_write",
    "write",
}

_READ_TOOLS = {
    "read_file",
    "view_file",
    "file_read",
    "read",
    "open",
}

_STR_REPLACE_TOOLS = {
    "str_replace_editor",
    "str_replace_based_edit_tool",
    "edit_file",
    "replace",
    "str_replace",
}


def _tool_name_to_op_type(name: str) -> OpType:
    n = name.lower()
    if n in _BASH_TOOLS:
        return OpType.BASH
    if n in _WRITE_TOOLS:
        return OpType.FILE_WRITE
    if n in _READ_TOOLS:
        return OpType.FILE_READ
    if n in _STR_REPLACE_TOOLS:
        return OpType.FILE_STR_REPLACE
    return OpType.UNKNOWN


# --------------------------------------------------------------------------- #
#  Argument extraction helpers                                                  #
# --------------------------------------------------------------------------- #

def _extract_bash(args: dict) -> SandboxOp:
    cmd = (
        args.get("command")
        or args.get("cmd")
        or args.get("code")
        or args.get("script")
        or ""
    )
    return SandboxOp(op_type=OpType.BASH, command=str(cmd), raw_args=args)


def _extract_file_write(args: dict) -> SandboxOp:
    path = args.get("path") or args.get("file_path") or args.get("filename") or ""
    content = args.get("content") or args.get("file_text") or args.get("text") or ""
    return SandboxOp(op_type=OpType.FILE_WRITE, path=str(path), content=str(content), raw_args=args)


def _extract_file_read(args: dict) -> SandboxOp:
    path = args.get("path") or args.get("file_path") or args.get("filename") or ""
    return SandboxOp(op_type=OpType.FILE_READ, path=str(path), raw_args=args)


def _extract_str_replace(args: dict) -> SandboxOp:
    path = args.get("path") or args.get("file_path") or ""
    command = args.get("command", "")
    if command == "view":
        # str_replace_editor also handles file viewing
        return SandboxOp(op_type=OpType.FILE_READ, path=str(path), raw_args=args)
    if command == "create":
        content = args.get("file_text") or args.get("content") or ""
        return SandboxOp(op_type=OpType.FILE_WRITE, path=str(path), content=str(content), raw_args=args)
    if command == "insert":
        # Insert new_str after the specified line number
        new_str = args.get("new_str") or args.get("new_string") or ""
        raw_line = args.get("insert_line") or args.get("line") or args.get("line_number") or 0
        try:
            insert_line = int(raw_line)
        except (ValueError, TypeError):
            insert_line = 0
        return SandboxOp(
            op_type=OpType.FILE_STR_REPLACE,
            path=str(path),
            new_str=str(new_str),
            insert_line=insert_line,
            raw_args=args,
        )
    old_str = args.get("old_str") or args.get("old_string") or ""
    new_str = args.get("new_str") or args.get("new_string") or ""
    return SandboxOp(
        op_type=OpType.FILE_STR_REPLACE,
        path=str(path),
        old_str=str(old_str),
        new_str=str(new_str),
        raw_args=args,
    )


_EXTRACTORS = {
    OpType.BASH: _extract_bash,
    OpType.FILE_WRITE: _extract_file_write,
    OpType.FILE_READ: _extract_file_read,
    OpType.FILE_STR_REPLACE: _extract_str_replace,
}


# --------------------------------------------------------------------------- #
#  Deserialisation helpers                                                      #
# --------------------------------------------------------------------------- #

def _deserialize_arguments(raw: Any) -> dict:
    """HuggingFace stores tool_call arguments as a JSON string; decode it."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _deserialize_tool_calls(tool_calls: Any) -> list[dict]:
    if not tool_calls:
        return []
    result = []
    for tc in tool_calls:
        tc = dict(tc)
        fn = tc.get("function")
        if isinstance(fn, dict) and "arguments" in fn:
            fn = dict(fn)
            fn["arguments"] = _deserialize_arguments(fn["arguments"])
            tc["function"] = fn
        result.append(tc)
    return result


# --------------------------------------------------------------------------- #
#  Role-aware field filtering (mirrors the HF dataset README example)          #
# --------------------------------------------------------------------------- #

_ROLE_FIELDS = {
    "system":    ["role", "content"],
    "assistant": ["role", "content", "tool_calls"],
    "user":      ["role", "content"],
    "tool":      ["role", "content", "name", "tool_call_id"],
}


def _clean_message(msg: dict) -> dict:
    role = msg.get("role", "user")
    keep = _ROLE_FIELDS.get(role, ["role", "content"])
    cleaned = {k: msg[k] for k in keep if k in msg}
    if role == "assistant" and "tool_calls" in cleaned:
        cleaned["tool_calls"] = _deserialize_tool_calls(cleaned["tool_calls"])
    return cleaned


# --------------------------------------------------------------------------- #
#  Workspace path detection                                                     #
# --------------------------------------------------------------------------- #

# Match /workspace/{subdir} where subdir contains at least one letter.
# Covers all known formats:
#   /workspace/sdf-xarray
#   /workspace/PlasmaFAIR__sdf-xarray__unknown
#   /workspace/django__django__5.0
_WORKSPACE_PATH_RE = re.compile(r"/workspace/([A-Za-z0-9_.~-]+(?:__[A-Za-z0-9_.~-]+)*)")


def _detect_workspace_path(ops: list[SandboxOp]) -> str:
    """Extract the workspace path from ops (e.g. /workspace/sdf-xarray).

    Scans file paths first (most reliable), then bash commands.
    Returns empty string if not found.
    """
    # Scan file paths first — they are clean, unambiguous
    for op in ops:
        if op.path:
            m = _WORKSPACE_PATH_RE.match(op.path)
            if m:
                return m.group(0)
    # Fall back to bash commands
    for op in ops:
        if op.command:
            m = _WORKSPACE_PATH_RE.search(op.command)
            if m:
                return m.group(0)
    return ""


# --------------------------------------------------------------------------- #
#  Main parser                                                                  #
# --------------------------------------------------------------------------- #

class TrajectoryParser:
    """Parse raw trajectory rows into ordered SandboxOp lists."""

    def parse(self, row: dict) -> ParsedTrajectory:
        """Parse a single trajectory row from the HuggingFace dataset."""
        instance_id = row.get("instance_id") or row.get("id") or "unknown"
        model_patch = row.get("model_patch") or row.get("git_patch") or ""
        raw_messages = [_clean_message(m) for m in (row.get("trajectory") or [])]

        ops: list[SandboxOp] = []
        n_assistant = 0
        n_tool_calls = 0

        for msg in raw_messages:
            role = msg.get("role")
            if role != "assistant":
                continue
            n_assistant += 1
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tool_name = fn.get("name") or ""
                args = fn.get("arguments") or {}
                call_id = tc.get("id") or ""

                op_type = _tool_name_to_op_type(tool_name)
                extractor = _EXTRACTORS.get(op_type)
                if extractor is None:
                    op = SandboxOp(op_type=OpType.UNKNOWN, raw_args=args, tool_call_id=call_id)
                else:
                    op = extractor(args)
                    op.tool_call_id = call_id
                ops.append(op)
                n_tool_calls += 1

        return ParsedTrajectory(
            instance_id=instance_id,
            ops=ops,
            raw_messages=raw_messages,
            n_assistant_turns=n_assistant,
            n_tool_calls=n_tool_calls,
            model_patch=model_patch,
            workspace_path=_detect_workspace_path(ops),
        )

    def parse_many(self, rows: list[dict]) -> list[ParsedTrajectory]:
        return [self.parse(r) for r in rows]

    def bash_ops_only(self, traj: ParsedTrajectory) -> list[SandboxOp]:
        """Return only BASH ops – the most relevant for sandbox load testing."""
        return [op for op in traj.ops if op.op_type == OpType.BASH]

    def summary(self, traj: ParsedTrajectory) -> dict:
        type_counts: dict[str, int] = {}
        for op in traj.ops:
            type_counts[op.op_type.value] = type_counts.get(op.op_type.value, 0) + 1
        return {
            "instance_id": traj.instance_id,
            "total_ops": len(traj.ops),
            "assistant_turns": traj.n_assistant_turns,
            "tool_calls": traj.n_tool_calls,
            "op_types": type_counts,
        }
