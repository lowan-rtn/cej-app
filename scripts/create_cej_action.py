#!/usr/bin/env python3
import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_APP_VERSION = "4.10.0"
DEFAULT_PLATFORM = "android"
DEFAULT_STATUS = "not_started"
DEFAULT_QUALIFICATION = "EMPLOI"
DEFAULT_SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"
VALID_STATUSES = {"not_started", "in_progress", "canceled", "done"}
VALID_QUALIFICATIONS = {
    "EMPLOI",
    "PROJET_PROFESSIONNEL",
    "CULTURE_SPORT_LOISIRS",
    "CITOYENNETE",
    "FORMATION",
    "LOGEMENT",
    "SANTE",
}


@dataclass
class ActionInput:
    title: str
    due: str
    comment: str | None
    status: str
    qualification: str
    reminder: bool
    duplicate: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create CEJ actions through the Pass Emploi API.",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.pass-emploi.beta.gouv.fr",
        help="API base URL, for example https://api.pass-emploi.beta.gouv.fr",
    )
    parser.add_argument("--token", help="Bearer access token")
    parser.add_argument("--user-id", help="User id. If omitted, the script tries to decode it from the JWT.")
    parser.add_argument("--id-token", help="Optional ID token. Useful to derive userId when the access token does not expose it.")
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help=f"Optional session file produced by login_cej.py. Defaults to {DEFAULT_SESSION_FILE}",
    )
    parser.add_argument("--title", help="Action title/content")
    parser.add_argument("--comment", help="Optional action description")
    parser.add_argument(
        "--due",
        help="Due date/time. Accepts YYYY-MM-DD or full ISO datetime. Date-only values are sent at 12:00 local time.",
    )
    parser.add_argument("--status", default=DEFAULT_STATUS, choices=sorted(VALID_STATUSES))
    parser.add_argument("--qualification", default=DEFAULT_QUALIFICATION, choices=sorted(VALID_QUALIFICATIONS))
    parser.add_argument("--no-reminder", action="store_true", help="Disable reminder")
    parser.add_argument("--duplicate", action="store_true", help="Mark the action as a duplicate")
    parser.add_argument("--bulk", type=Path, help="Path to a JSON array for bulk creation")
    parser.add_argument("--app-version", default=DEFAULT_APP_VERSION, help="Value for X-AppVersion")
    parser.add_argument("--platform", default=DEFAULT_PLATFORM, help="Value for X-Platform")
    parser.add_argument("--dry-run", action="store_true", help="Print requests without sending them")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = load_session(args.session_file)
    token = args.token or value_from_session(session, "access_token")
    id_token = args.id_token or value_from_session(session, "id_token")

    if not token:
        print("error: missing token; pass --token or login first with login_cej.py", file=sys.stderr)
        return 2

    try:
        user_id = resolve_user_id(args.user_id or value_from_session(session, "user_id"), id_token, token)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        actions = load_actions(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for item in actions:
        payload = {
            "content": item.title,
            "status": item.status,
            "dateEcheance": normalize_due(item.due),
            "rappel": item.reminder,
            "codeQualification": item.qualification,
            "estDuplicata": item.duplicate,
        }
        if item.comment:
            payload["comment"] = item.comment

        url = f"{args.base_url.rstrip('/')}/jeunes/{user_id}/action"
        correlation_id = f"{user_id}-{int(datetime.now().timestamp() * 1000)}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "X-UserId": user_id,
            "X-InstallationId": str(uuid.uuid4()),
            "X-CorrelationId": correlation_id,
            "X-AppVersion": args.app_version,
            "X-Platform": args.platform,
        }

        if args.dry_run:
            print(json.dumps({"url": url, "headers": headers, "payload": payload}, ensure_ascii=True, indent=2))
            continue

        response = post_json(url, headers, payload)
        print(json.dumps(response, ensure_ascii=True, indent=2))

    return 0


def load_actions(args: argparse.Namespace) -> list[ActionInput]:
    if args.bulk:
        raw = json.loads(args.bulk.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError("--bulk must point to a non-empty JSON array")
        return [action_from_mapping(item) for item in raw]

    if not args.title or not args.due:
        raise ValueError("--title and --due are required when --bulk is not used")

    return [
        ActionInput(
            title=args.title,
            due=args.due,
            comment=args.comment,
            status=args.status,
            qualification=args.qualification,
            reminder=not args.no_reminder,
            duplicate=args.duplicate,
        )
    ]


def load_session(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser()
    if not resolved.exists():
        return None
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else None


def value_from_session(session: dict[str, Any] | None, key: str) -> str | None:
    if session is None:
        return None
    value = session.get(key)
    return value if isinstance(value, str) and value else None


def action_from_mapping(item: Any) -> ActionInput:
    if not isinstance(item, dict):
        raise ValueError("Each --bulk entry must be an object")

    title = require_string(item, "title")
    due = require_string(item, "due")
    comment = optional_string(item, "comment")
    status = item.get("status", DEFAULT_STATUS)
    qualification = item.get("qualification", DEFAULT_QUALIFICATION)
    reminder = bool(item.get("reminder", True))
    duplicate = bool(item.get("duplicate", False))

    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    if qualification not in VALID_QUALIFICATIONS:
        raise ValueError(f"Invalid qualification: {qualification}")

    return ActionInput(
        title=title,
        due=due,
        comment=comment,
        status=status,
        qualification=qualification,
        reminder=reminder,
        duplicate=duplicate,
    )


def require_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid string field: {key}")
    return value


def optional_string(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid string field: {key}")
    return value


def normalize_due(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("due must not be empty")

    try:
        parsed = date.fromisoformat(raw)
        parsed_dt = datetime.combine(parsed, time(hour=12, minute=0)).astimezone()
        return parsed_dt.isoformat(timespec="seconds")
    except ValueError:
        pass

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed_dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid due value: {raw}") from exc

    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.astimezone()
    return parsed_dt.isoformat(timespec="seconds")


def resolve_user_id(explicit_user_id: str | None, id_token: str | None, access_token: str) -> str:
    if explicit_user_id:
        return explicit_user_id

    for token in [id_token, access_token]:
        if not token:
            continue
        claims = decode_jwt_payload(token)
        for key in ("userId", "uid", "sub"):
            value = claims.get(key)
            if isinstance(value, str) and value.strip():
                return value

    raise ValueError("unable to derive userId from JWT; pass --user-id explicitly")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(
            __import__("base64").urlsafe_b64decode(payload + padding).decode("utf-8")
        )
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"status": resp.status}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise SystemExit(f"Network error: {exc.reason}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
