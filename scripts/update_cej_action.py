#!/usr/bin/env python3
import argparse
import json
import sys
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "https://api.pass-emploi.beta.gouv.fr"
DEFAULT_APP_VERSION = "4.10.0"
DEFAULT_PLATFORM = "android"
DEFAULT_SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"
VALID_STATUSES = ("not_started", "in_progress", "canceled", "done")
VALID_QUALIFICATIONS = (
    "EMPLOI",
    "PROJET_PROFESSIONNEL",
    "CULTURE_SPORT_LOISIRS",
    "CITOYENNETE",
    "FORMATION",
    "LOGEMENT",
    "SANTE",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update an existing CEJ action.")
    parser.add_argument("action_ids", nargs="+", help="One or more action ids")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--token", help="Bearer access token")
    parser.add_argument("--session-file", type=Path, default=DEFAULT_SESSION_FILE, help=f"Session file. Defaults to {DEFAULT_SESSION_FILE}")
    parser.add_argument("--status", choices=VALID_STATUSES, help="New status")
    parser.add_argument("--title", help="Replace action title/content")
    parser.add_argument("--comment", help="Replace action description/comment")
    parser.add_argument("--due", help="Replace due date. Accepts YYYY-MM-DD or full ISO datetime.")
    parser.add_argument("--qualification", choices=VALID_QUALIFICATIONS, help="Replace action category")
    parser.add_argument("--date-fin-reelle", help="Optional completion datetime. Accepts full ISO datetime.")
    parser.add_argument("--dry-run", action="store_true", help="Print request bodies without sending them")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = load_session(args.session_file)
    token = args.token or value_from_session(session, "access_token")
    if not token:
        print("error: missing token; pass --token or login first with login_cej.py", file=sys.stderr)
        return 2
    if not any([args.status, args.title, args.comment is not None, args.due, args.qualification, args.date_fin_reelle]):
        print("error: no update requested; pass at least one of --status, --title, --comment, --due, --qualification, --date-fin-reelle", file=sys.stderr)
        return 2

    for action_id in args.action_ids:
        try:
            action = get_json(
                f"{args.base_url.rstrip('/')}/actions/{action_id}",
                {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            payload = build_update_payload(action, args)
            if args.dry_run:
                print(json.dumps({"action_id": action_id, "payload": payload}, ensure_ascii=True, indent=2))
                continue

            response = put_json(
                f"{args.base_url.rstrip('/')}/actions/{action_id}",
                make_headers(token, session, action_id),
                payload,
            )
            print(json.dumps({"action_id": action_id, "status": "updated", "response": response}, ensure_ascii=True))
        except RuntimeError as exc:
            print(f"{action_id}: {exc}", file=sys.stderr)
            return 1
    return 0


def build_update_payload(action: Any, args: argparse.Namespace) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise RuntimeError("unexpected action payload")

    effective_due = normalize_due(args.due) if args.due else action.get("dateFinReelle") or action.get("dateEcheance")
    effective_status = args.status or action.get("status", "not_started")

    payload = {
        "status": effective_status,
        "contenu": args.title if args.title is not None else action.get("content", ""),
        "dateEcheance": effective_due,
    }
    comment = args.comment if args.comment is not None else action.get("comment")
    if isinstance(comment, str) and comment:
        payload["description"] = comment
    qualification = action.get("qualification")
    if isinstance(qualification, dict):
        code = qualification.get("code")
        if isinstance(code, str) and code:
            payload["codeQualification"] = code
    if args.qualification:
        payload["codeQualification"] = args.qualification

    if effective_status == "done":
        payload["dateFinReelle"] = normalize_due(args.date_fin_reelle) if args.date_fin_reelle else payload["dateEcheance"]
    elif args.date_fin_reelle:
        payload["dateFinReelle"] = normalize_due(args.date_fin_reelle)

    return payload


def normalize_due(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise RuntimeError("empty date value")
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
        raise RuntimeError(f"invalid date value: {raw}") from exc

    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.astimezone()
    return parsed_dt.isoformat(timespec="seconds")


def make_headers(token: str, session: dict[str, Any] | None, action_id: str) -> dict[str, str]:
    user_id = value_from_session(session, "user_id") or "unknown"
    correlation_id = f"{action_id}-{int(datetime.now().timestamp() * 1000)}"
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "X-UserId": user_id,
        "X-InstallationId": str(uuid.uuid4()),
        "X-CorrelationId": correlation_id,
        "X-AppVersion": DEFAULT_APP_VERSION,
        "X-Platform": DEFAULT_PLATFORM,
    }


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


def get_json(url: str, headers: dict[str, str]) -> Any:
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def put_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"status": resp.status}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
