#!/usr/bin/env python3
import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "https://api.pass-emploi.beta.gouv.fr"
DEFAULT_APP_VERSION = "4.10.0"
DEFAULT_PLATFORM = "android"
DEFAULT_SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete one or more CEJ actions.")
    parser.add_argument("action_ids", nargs="+", help="One or more action ids to delete")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--token", help="Bearer access token")
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help=f"Session file. Defaults to {DEFAULT_SESSION_FILE}",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print requests without sending them")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = load_session(args.session_file)
    token = args.token or value_from_session(session, "access_token")
    if not token:
        print("error: missing token; pass --token or login first with login_cej.py", file=sys.stderr)
        return 2

    for action_id in args.action_ids:
        try:
            url = f"{args.base_url.rstrip('/')}/actions/{action_id}"
            headers = make_headers(token, session, action_id)
            if args.dry_run:
                print(json.dumps({"action_id": action_id, "url": url, "headers": headers}, ensure_ascii=True, indent=2))
                continue

            response = delete_with_reopen_if_needed(url, headers, token, session, action_id)
            print(json.dumps({"action_id": action_id, "status": "deleted", "response": response}, ensure_ascii=True))
        except RuntimeError as exc:
            print(f"{action_id}: {exc}", file=sys.stderr)
            return 1
    return 0


def make_headers(token: str, session: dict[str, Any] | None, action_id: str) -> dict[str, str]:
    user_id = value_from_session(session, "user_id") or "unknown"
    correlation_id = f"{action_id}-{int(datetime.now().timestamp() * 1000)}"
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
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


def delete_request(url: str, headers: dict[str, str]) -> Any:
    req = request.Request(url, headers=headers, method="DELETE")
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"status": resp.status}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def delete_with_reopen_if_needed(
    url: str,
    headers: dict[str, str],
    token: str,
    session: dict[str, Any] | None,
    action_id: str,
) -> Any:
    try:
        return delete_request(url, headers)
    except RuntimeError as exc:
        message = str(exc)
        if "HTTP 400" not in message or "Impossible de supprimer une action terminée" not in message:
            raise

    action = get_json(url, {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    payload = build_reopen_payload(action)
    put_json(url, make_put_headers(token, session, action_id), payload)
    return delete_request(url, headers)


def build_reopen_payload(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise RuntimeError("unexpected action payload")

    due = action.get("dateEcheance") or action.get("dateFinReelle")
    if not isinstance(due, str) or not due:
        raise RuntimeError("missing due date on action")

    payload: dict[str, Any] = {
        "status": "not_started",
        "contenu": action.get("content", ""),
        "dateEcheance": due,
    }

    comment = action.get("comment")
    if isinstance(comment, str) and comment:
        payload["description"] = comment

    qualification = action.get("qualification")
    if isinstance(qualification, dict):
        code = qualification.get("code")
        if isinstance(code, str) and code:
            payload["codeQualification"] = code

    return payload


def make_put_headers(token: str, session: dict[str, Any] | None, action_id: str) -> dict[str, str]:
    headers = make_headers(token, session, action_id)
    headers["Content-Type"] = "application/json; charset=utf-8"
    return headers


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
