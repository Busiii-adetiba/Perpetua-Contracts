#!/usr/bin/env python3
"""Check security-relevant changes in contract snapshot JSON files."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Security-relevant fields in snapshot JSON files
SECURITY_FIELDS = {
    "auth", "auths", "require_auth", "signatures", "events", "topic",
    "topics", "data", "error", "error_code", "ContractError", "storage",
    "state", "DataKey",
}

# Frozen event-name set from docs/ABI.md (issue #286).
FROZEN_EVENT_NAMES = {
    "stream_created",
    "withdrawn",
    "cancelled",
    "paused",
    "resumed",
    "topped_up",
    "recipient_transferred",
    "delegate_granted",
    "delegate_revoked",
    "ttl_extended",
}

# Required payload fields per frozen event name.
REQUIRED_PAYLOAD_FIELDS = {
    "stream_created": {"stream_id", "sender", "recipient", "amount"},
    "withdrawn": {"stream_id", "recipient", "amount"},
    "cancelled": {"stream_id", "sender", "recipient"},
    "paused": {"stream_id"},
    "resumed": {"stream_id"},
    "topped_up": {"stream_id", "amount"},
    "recipient_transferred": {"stream_id", "old_recipient", "new_recipient"},
    "delegate_granted": {"stream_id", "delegate"},
    "delegate_revoked": {"stream_id", "delegate"},
    "ttl_extended": {"stream_id", "extended_to"},
}

EVENT_SNAPSHOT_DIR = REPO_ROOT / "tests" / "fixtures" / "event_snapshots"
=======
REPO_ROOT = Path(__file__).resolve().parent.parent

# Security-relevant fields in snapshot JSON files
SECURITY_FIELDS = {
    "auth", "auths", "require_auth", "signatures", "events", "topic",
    "topics", "data", "error", "error_code", "ContractError", "storage",
    "state", "DataKey",
}

# Frozen event-name set from docs/ABI.md (issue #286).
FROZEN_EVENT_NAMES = {
    "stream_created",
    "withdrawn",
    "cancelled",
    "paused",
    "resumed",
    "topped_up",
    "recipient_transferred",
    "delegate_granted",
    "delegate_revoked",
    "ttl_extended",
}

# Required payload fields per frozen event name.
REQUIRED_PAYLOAD_FIELDS = {
    "stream_created": {"stream_id", "sender", "recipient", "amount"},
    "withdrawn": {"stream_id", "recipient", "amount"},
    "cancelled": {"stream_id", "sender", "recipient"},
    "paused": {"stream_id"},
    "resumed": {"stream_id"},
    "topped_up": {"stream_id", "amount"},
    "recipient_transferred": {"stream_id", "old_recipient", "new_recipient"},
    "delegate_granted": {"stream_id", "delegate"},
    "delegate_revoked": {"stream_id", "delegate"},
    "ttl_extended": {"stream_id", "extended_to"},
}

EVENT_SNAPSHOT_DIR = REPO_ROOT / "tests" / "fixtures" / "event_snapshots"


def get_changed_files(base: str, head: str | None = None) -> list[str]:
    """Return changed snapshot JSON paths between two git refs."""
    args = ["git", "diff", "--name-only", base]
    if head is not None:
        args.append(head)

    try:
        output = subprocess.check_output(args)
    except subprocess.CalledProcessError:
        return []

    return [
        path for path in output.decode("utf-8").splitlines()
        if path.endswith(".json") and "test_snapshots" in path
    ]


def get_changed_snapshots(base: str) -> list[Path]:
    """Compatibility wrapper returning changed snapshot paths as ``Path`` objects."""
    return [REPO_ROOT / path for path in get_changed_files(base)]


def get_file_content(commit: str | None, path: str) -> str | None:
    """Read a file from git, or from the working tree when no commit is given."""
    if commit is not None:
        try:
            return subprocess.check_output(["git", "show", f"{commit}:{path}"]).decode(
                "utf-8"
            )
        except subprocess.CalledProcessError:
            return None

    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as snapshot_file:
        return snapshot_file.read()


def get_diff_paths(old, new, prefix: str = "") -> list[str]:
    """Return JSON paths whose values differ between two decoded documents."""
    if isinstance(old, dict) and isinstance(new, dict):
        paths = []
        for key in old.keys() | new.keys():
            child = f"{prefix}.{key}" if prefix else key
            if key not in old or key not in new:
                paths.append(child)
            else:
                paths.extend(get_diff_paths(old[key], new[key], child))
        return paths

    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return [prefix]
        paths = []
        for index, (old_item, new_item) in enumerate(zip(old, new)):
            child = f"{prefix}[{index}]"
            paths.extend(get_diff_paths(old_item, new_item, child))
        return paths

    return [] if old == new else [prefix]


def is_security_relevant(path: str) -> bool:
    """Return whether a changed JSON path contains a security-relevant member."""
    members = path.replace("[", ".").replace("]", "").split(".")
    return any(member in SECURITY_FIELDS for member in members)


def check_snapshot_security_fields(snapshot_path: Path, base: str) -> list[str]:
    """Compatibility helper for checking a working-tree snapshot against git."""
    try:
        relative_path = snapshot_path.relative_to(REPO_ROOT)
    except ValueError:
        return [f"{snapshot_path.name}: path is not under repository root"]

    old_content = get_file_content(base, str(relative_path))
    new_content = snapshot_path.read_text(encoding="utf-8")
    try:
        old_data = json.loads(old_content) if old_content is not None else {}
        new_data = json.loads(new_content)
    except (json.JSONDecodeError, OSError) as error:
        return [f"{snapshot_path.name}: error: {error}"]

    return [
        f"{relative_path}: field '{path}' changed"
        for path in get_diff_paths(old_data, new_data)
        if is_security_relevant(path)
    ]


def _extract_topic_name(topic) -> str | None:
    """Return the event name from a topic entry (string or list of segments)."""
    if isinstance(topic, str):
        return topic
    if isinstance(topic, list):
        for segment in topic:
            if isinstance(segment, str) and segment in FROZEN_EVENT_NAMES:
                return segment
        for segment in topic:
            if isinstance(segment, str):
                return segment
    return None


def validate_event_snapshot(snapshot_path: Path) -> list[str]:
    """Validate a single event snapshot fixture against the frozen ABI schema."""
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return [f"{snapshot_path.name}: error: {error}"]

    issues: list[str] = []
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return [f"{snapshot_path.name}: missing 'events' array"]

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            issues.append(f"{snapshot_path.name}: events[{index}] is not an object")
            continue

        topic_name = _extract_topic_name(event.get("topic"))
        if topic_name is None:
            issues.append(f"{snapshot_path.name}: events[{index}] missing 'topic'")
            continue
        if topic_name not in FROZEN_EVENT_NAMES:
            issues.append(
                f"{snapshot_path.name}: events[{index}] unknown topic '{topic_name}'"
            )
            continue

        payload = event.get("data", event.get("payload"))
        if not isinstance(payload, dict):
            issues.append(
                f"{snapshot_path.name}: events[{index}] missing payload object"
            )
            continue

        missing = REQUIRED_PAYLOAD_FIELDS[topic_name] - set(payload.keys())
        for field in sorted(missing):
            issues.append(
                f"{snapshot_path.name}: events[{index}] ({topic_name}) "
                f"missing required field '{field}'"
            )

    return issues


def run_schema_check() -> int:
    """Validate all event snapshot fixtures against the frozen ABI schema."""
    if not EVENT_SNAPSHOT_DIR.is_dir():
        print(f"No event snapshot directory at {EVENT_SNAPSHOT_DIR}.")
        raise SystemExit(0)

    fixtures = sorted(EVENT_SNAPSHOT_DIR.glob("*.json"))
    if not fixtures:
        print("No event snapshot fixtures found.")
        raise SystemExit(0)

    all_issues: list[str] = []
    for fixture in fixtures:
        all_issues.extend(validate_event_snapshot(fixture))

    if all_issues:
        print("Event snapshot schema violations:")
        for issue in all_issues:
            print(f"  - {issue}")
        raise SystemExit(1)

    print(f"Validated {len(fixtures)} event snapshot fixture(s) against frozen ABI.")
    raise SystemExit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check snapshot security diffs")
    parser.add_argument("--base", default="HEAD", help="Base ref to compare against")
    parser.add_argument("--head", default=None, help="Head ref to compare against")
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Validate event snapshot fixtures against the frozen ABI schema",
    )
    args = parser.parse_args()

    if args.schema:
        return run_schema_check()

    changed_files = get_changed_files(args.base, args.head)
    if not changed_files:
        print("No snapshot JSON files changed.")
        raise SystemExit(0)

    all_issues = []
    for path in changed_files:
        old_content = get_file_content(args.base, path)
        new_content = get_file_content(args.head, path)
        try:
            old_data = json.loads(old_content) if old_content is not None else {}
        except json.JSONDecodeError:
            old_data = {}
        try:
            new_data = json.loads(new_content) if new_content is not None else {}
        except json.JSONDecodeError:
            new_data = {}

        security_paths = [
            path for path in get_diff_paths(old_data, new_data)
            if is_security_relevant(path)
        ]
        if not security_paths:
            print(f"[INFO] Changes in {path}: none are security-relevant")
        all_issues.extend(f"{path}: {diff_path}" for diff_path in security_paths)

    if all_issues:
        print("Security-relevant fields changed:")
        for issue in all_issues:
            print(f"  - {issue}")
        print("Mandatory extra review required.")
        raise SystemExit(1)

    print("No security-relevant snapshot changes detected.")
    raise SystemExit(0)


if __name__ == "__main__":
    sys.exit(main())
