#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List CEJ actions through the Pass Emploi API.")
    parser.add_argument(
        "--base-url",
        default="https://api.pass-emploi.beta.gouv.fr",
        help="API base URL, for example https://api.pass-emploi.beta.gouv.fr",
    )
    parser.add_argument("--token", help="Bearer access token")
    parser.add_argument("--user-id", help="User id")
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help=f"Optional session file produced by login_cej.py. Defaults to {DEFAULT_SESSION_FILE}",
    )
    parser.add_argument("--from-date", required=True, help="Start date, YYYY-MM-DD or full ISO datetime")
    parser.add_argument("--to-date", required=True, help="End date, YYYY-MM-DD or full ISO datetime")
    parser.add_argument(
        "--mode",
        choices=("mon-suivi", "actions"),
        default="mon-suivi",
        help="Listing source. 'mon-suivi' matches the beneficiary app flow; 'actions' hits the direct endpoint reserved to counsellors.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = load_session(args.session_file)
    token = args.token or value_from_session(session, "access_token")
    user_id = args.user_id or value_from_session(session, "user_id")

    if not token:
        print("error: missing token; pass --token or login first with login_cej.py", file=sys.stderr)
        return 2
    if not user_id:
        print("error: missing user id; pass --user-id or login first with login_cej.py", file=sys.stderr)
        return 2

    date_debut = normalize_date(args.from_date, start_of_day=True)
    date_fin = normalize_date(args.to_date, start_of_day=False)

    query = parse.urlencode({"dateDebut": date_debut, "dateFin": date_fin})
    url = build_url(args.base_url, user_id, args.mode, query)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        response_payload = get_json(url, headers)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(response_payload, ensure_ascii=True, indent=2))
        return 0

    actions = extract_actions(response_payload, args.mode)
    if not actions:
        print("Aucune action trouvee sur cette periode.")
        return 0

    for action in actions:
        print(format_action(action))

    return 0


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


def build_url(base_url: str, user_id: str, mode: str, query: str) -> str:
    root = base_url.rstrip("/")
    if mode == "actions":
        return f"{root}/jeunes/{user_id}/actions?{query}"
    return f"{root}/jeunes/milo/{user_id}/mon-suivi?{query}"


def extract_actions(payload: Any, mode: str) -> list[dict[str, Any]]:
    if mode == "actions":
        return payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        actions = payload.get("actions")
        if isinstance(actions, list):
            return actions
    return []


def normalize_date(raw: str, start_of_day: bool) -> str:
    raw = raw.strip()
    try:
        parsed = date.fromisoformat(raw)
        parsed_dt = datetime.combine(parsed, time.min if start_of_day else time.max).astimezone()
        if not start_of_day:
            parsed_dt = parsed_dt.replace(microsecond=0)
        return parsed_dt.isoformat()
    except ValueError:
        pass

    normalized = raw.replace("Z", "+00:00")
    parsed_dt = datetime.fromisoformat(normalized)
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.astimezone()
    return parsed_dt.isoformat()


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


def format_action(action: dict[str, Any]) -> str:
    action_id = action.get("id", "?")
    content = action.get("content", "")
    status = action.get("status", "")
    due = action.get("dateFinReelle") or action.get("dateEcheance") or ""
    comment = action.get("comment", "") or ""
    qualification = ""
    qualification_status = action.get("etat", "") or ""
    qualification_raw = action.get("qualification")
    if isinstance(qualification_raw, dict):
        qualification = qualification_raw.get("code", "")
    creator_type = action.get("creatorType", "")
    creator_name = action.get("creator", "") or ""

    creator = creator_type
    if creator_name:
        creator = f"{creator_type}:{creator_name}" if creator_type else creator_name

    lines = [
        f"[{action_id}] {content}",
        f"  status={status} due={due} qualification={qualification} etat={qualification_status} creator={creator}",
    ]
    if comment:
        lines.append(f"  comment={comment}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
