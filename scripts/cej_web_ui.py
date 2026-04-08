#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
LOGIN_SCRIPT = ROOT / "login_cej.py"
LIST_SCRIPT = ROOT / "list_cej_actions.py"
CREATE_SCRIPT = ROOT / "create_cej_action.py"
UPDATE_SCRIPT = ROOT / "update_cej_action.py"
DELETE_SCRIPT = ROOT / "delete_cej_action.py"
SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"
REMEMBER_FILE = Path.home() / ".config" / "pass_emploi" / "remembered_login.json"
THEME_FILE = Path.home() / ".config" / "pass_emploi" / "theme.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web UI for Pass Emploi / CEJ automation.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Default: 8765")
    return parser.parse_args()


class CejHandler(BaseHTTPRequestHandler):
    server_version = "CEJWebUI/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/session":
            self._send_json(build_client_state())
            return
        if parsed.path == "/api/theme":
            self._send_json({"ok": True, "theme": load_theme_settings()})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/login":
            response = handle_login(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/refresh":
            response = run_script([sys.executable, str(LOGIN_SCRIPT), "--refresh-only", "--json"])
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/logout":
            response = handle_logout()
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/theme":
            response = handle_theme_update(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/list":
            response = handle_list(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/analyze":
            response = handle_analyze(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/create":
            response = handle_create(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/update-action":
            response = handle_update_action(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/delete-action":
            response = handle_delete_action(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/duplicate-action":
            response = handle_duplicate_action(payload)
            self._send_json(response, status=HTTPStatus.OK if response["ok"] else HTTPStatus.BAD_REQUEST)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return {}
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def handle_login(payload: dict) -> dict:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    remember_me = bool(payload.get("remember_me"))
    if not username or not password:
        return {"ok": False, "error": "Identifiant et mot de passe requis."}

    attempts = []
    for mode in ("similo", "generic", "pole-emploi"):
        response = run_script(
            [
                sys.executable,
                str(LOGIN_SCRIPT),
                "--username",
                username,
                "--password",
                password,
                "--mode",
                mode,
                "--json",
            ]
        )
        attempts.append(
            {
                "mode": mode,
                "ok": response["ok"],
                "error": None if response["ok"] else response.get("error", "echec"),
            }
        )
        if response["ok"]:
            persist_remembered_credentials(username, password, remember_me)
            response["attempts"] = attempts
            return response

    persist_remembered_credentials(username, password, remember_me)
    return {"ok": False, "error": "Aucun mode de connexion n'a fonctionne.", "attempts": attempts, "session": build_client_state()}


def handle_list(payload: dict) -> dict:
    from_date = str(payload.get("from_date", "")).strip()
    to_date = str(payload.get("to_date", "")).strip()
    if not from_date or not to_date:
        return {"ok": False, "error": "Periode incomplete."}

    response = run_script(
        [
            sys.executable,
            str(LIST_SCRIPT),
            "--from-date",
            from_date,
            "--to-date",
            to_date,
            "--json",
        ]
    )
    if response["ok"]:
        try:
            parsed = json.loads(response["stdout"])
        except json.JSONDecodeError:
            return {"ok": False, "error": "Reponse JSON invalide du script de listing."}
        response["data"] = parsed
    return response


def handle_logout() -> dict:
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        return {"ok": True, "session": build_client_state(), "message": "Session locale supprimee."}
    except OSError as exc:
        return {"ok": False, "error": f"Impossible de supprimer la session locale: {exc}"}


def handle_create(payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    due = str(payload.get("due", "")).strip()
    comment = str(payload.get("comment", "")).strip()
    qualification = str(payload.get("qualification", "EMPLOI")).strip() or "EMPLOI"
    if not title or not due:
        return {"ok": False, "error": "Titre et date requis."}

    cmd = [
        sys.executable,
        str(CREATE_SCRIPT),
        "--title",
        title,
        "--due",
        due,
        "--qualification",
        qualification,
    ]
    if comment:
        cmd.extend(["--comment", comment])

    response = run_script(cmd)
    if response["ok"]:
        try:
            response["data"] = json.loads(response["stdout"])
        except json.JSONDecodeError:
            return {"ok": False, "error": "Reponse JSON invalide du script de creation."}
    return response


def handle_theme_update(payload: dict) -> dict:
    dark_mode = bool(payload.get("darkMode"))
    primary = payload.get("primary")
    secondary = payload.get("secondary")
    if not isinstance(primary, str) or not isinstance(secondary, str):
        return {"ok": False, "error": "Couleurs invalides."}
    normalized = {
        "darkMode": dark_mode,
        "primary": normalize_hex_color(primary, "#174a7c"),
        "secondary": normalize_hex_color(secondary, "#6e56cf"),
    }
    persist_theme_settings(normalized)
    return {"ok": True, "theme": normalized}


def handle_update_action(payload: dict) -> dict:
    action_id = str(payload.get("id", "")).strip()
    if not action_id:
        return {"ok": False, "error": "Identifiant d'action manquant."}

    cmd = [sys.executable, str(UPDATE_SCRIPT), action_id]
    for key, flag in (
        ("status", "--status"),
        ("title", "--title"),
        ("comment", "--comment"),
        ("due", "--due"),
        ("qualification", "--qualification"),
        ("date_fin_reelle", "--date-fin-reelle"),
    ):
        value = payload.get(key)
        if isinstance(value, str):
            cmd.extend([flag, value])
    return run_script(cmd)


def handle_duplicate_action(payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    due = str(payload.get("due", "")).strip()
    if not title or not due:
        return {"ok": False, "error": "Titre et date requis pour dupliquer."}

    cmd = [
        sys.executable,
        str(CREATE_SCRIPT),
        "--title",
        title,
        "--due",
        due,
        "--qualification",
        str(payload.get("qualification", "EMPLOI")).strip() or "EMPLOI",
        "--status",
        str(payload.get("status", "not_started")).strip() or "not_started",
    ]
    comment = payload.get("comment")
    if isinstance(comment, str):
        cmd.extend(["--comment", comment])
    return run_script(cmd)


def handle_delete_action(payload: dict) -> dict:
    action_id = str(payload.get("id", "")).strip()
    if not action_id:
        return {"ok": False, "error": "Identifiant d'action manquant."}

    response = run_script([sys.executable, str(DELETE_SCRIPT), action_id])
    if response["ok"] and response["stdout"]:
        try:
            response["data"] = json.loads(response["stdout"])
        except json.JSONDecodeError:
            return {"ok": False, "error": "Reponse JSON invalide du script de suppression."}
    return response


def handle_analyze(payload: dict) -> dict:
    from_date = str(payload.get("from_date", "")).strip()
    to_date = str(payload.get("to_date", "")).strip()
    if not from_date or not to_date:
        return {"ok": False, "error": "Periode incomplete."}

    response = handle_list(payload)
    if not response["ok"]:
        return response

    try:
        analysis = analyze_actions(response["data"], from_date, to_date)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    response["analysis"] = analysis
    return response


def run_script(cmd: list[str]) -> dict:
    completed = subprocess.run(cmd, capture_output=True, text=True)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode == 0:
        session = build_client_state()
        return {"ok": True, "stdout": stdout, "stderr": stderr, "session": session}
    return {"ok": False, "error": stderr or stdout or "Commande echouee", "stdout": stdout, "stderr": stderr}


def load_session() -> dict:
    if not SESSION_FILE.exists():
        return {"connected": False}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"connected": False}
    if not isinstance(data, dict):
        return {"connected": False}
    return {
        "connected": True,
        "user_id": data.get("user_id"),
        "expires_at": data.get("access_token_expires_at"),
        "has_refresh_token": bool(data.get("refresh_token")),
    }


def load_remembered_credentials() -> dict:
    if not REMEMBER_FILE.exists():
        return {"remember_me": False}
    try:
        data = json.loads(REMEMBER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"remember_me": False}
    if not isinstance(data, dict):
        return {"remember_me": False}
    return {
        "remember_me": True,
        "username": data.get("username", "") if isinstance(data.get("username"), str) else "",
        "password": data.get("password", "") if isinstance(data.get("password"), str) else "",
    }


def persist_remembered_credentials(username: str, password: str, remember_me: bool) -> None:
    REMEMBER_FILE.parent.mkdir(parents=True, exist_ok=True)
    if remember_me:
        REMEMBER_FILE.write_text(
            json.dumps({"username": username, "password": password}, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return
    if REMEMBER_FILE.exists():
        REMEMBER_FILE.unlink()


def build_client_state() -> dict:
    session = load_session()
    remembered = load_remembered_credentials()
    return {
        **session,
        "remembered_username": remembered.get("username", ""),
        "remembered_password": remembered.get("password", ""),
        "remember_me": bool(remembered.get("remember_me")),
    }


def normalize_hex_color(value: str, fallback: str) -> str:
    candidate = value.strip().lower()
    if len(candidate) == 7 and candidate.startswith("#") and all(ch in "0123456789abcdef" for ch in candidate[1:]):
        return candidate
    return fallback


def load_theme_settings() -> dict:
    if not THEME_FILE.exists():
        return {"darkMode": False, "primary": "#174a7c", "secondary": "#6e56cf"}
    try:
        data = json.loads(THEME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"darkMode": False, "primary": "#174a7c", "secondary": "#6e56cf"}
    if not isinstance(data, dict):
        return {"darkMode": False, "primary": "#174a7c", "secondary": "#6e56cf"}
    return {
        "darkMode": bool(data.get("darkMode")),
        "primary": normalize_hex_color(str(data.get("primary", "#174a7c")), "#174a7c"),
        "secondary": normalize_hex_color(str(data.get("secondary", "#6e56cf")), "#6e56cf"),
    }


def persist_theme_settings(theme: dict) -> None:
    THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
    THEME_FILE.write_text(json.dumps(theme, ensure_ascii=True, indent=2), encoding="utf-8")


def analyze_actions(payload: dict, from_date: str, to_date: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload d'analyse invalide.")
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise ValueError("Aucune liste d'actions a analyser.")

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if end < start:
        raise ValueError("La date de fin doit etre apres la date de debut.")

    weeks: dict[str, dict] = {}
    total_hours = 0
    qualified_hours = 0

    cursor = start - timedelta(days=start.weekday())
    last_week_start = end - timedelta(days=end.weekday())
    while cursor <= last_week_start:
        week_key = cursor.isoformat()
        weeks[week_key] = {
            "week_start": week_key,
            "week_end": (cursor + timedelta(days=6)).isoformat(),
            "count": 0,
            "qualified_hours": 0,
            "titles": [],
        }
        cursor += timedelta(days=7)

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_date = extract_action_date(action)
        if action_date is None:
            continue
        week_start = action_date - timedelta(days=action_date.weekday())
        week_key = week_start.isoformat()
        if week_key not in weeks:
            continue
        weeks[week_key]["count"] += 1
        title = action.get("content")
        if isinstance(title, str) and title:
            weeks[week_key]["titles"].append(title)

        qualification = action.get("qualification")
        if isinstance(qualification, dict):
            hours = qualification.get("heures")
            if isinstance(hours, int):
                weeks[week_key]["qualified_hours"] += hours
                qualified_hours += hours
        total_hours += 0

    ordered_weeks = list(weeks.values())
    missing_weeks = [week for week in ordered_weeks if week["count"] == 0]
    busiest = sorted(ordered_weeks, key=lambda item: (item["count"], item["qualified_hours"]), reverse=True)

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "actions_count": len(actions),
        "qualified_hours_total": qualified_hours,
        "weeks_total": len(ordered_weeks),
        "weeks_missing_actions": len(missing_weeks),
        "missing_weeks": missing_weeks,
        "busiest_weeks": busiest[:6],
        "weeks": ordered_weeks,
    }


def extract_action_date(action: dict) -> date | None:
    for key in ("dateFinReelle", "dateEcheance", "dateCreation"):
        raw = action.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            pass
    return None


INDEX_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tableau de bord CEJ</title>
  <style>
    :root {
      --bg: #edf2f7;
      --sidebar: #12304d;
      --sidebar-soft: #1e456b;
      --sidebar-ink: #f1f6fb;
      --sidebar-muted: rgba(241,246,251,0.72);
      --sidebar-muted-strong: rgba(241,246,251,0.68);
      --sidebar-foot: rgba(241,246,251,0.58);
      --panel: #ffffff;
      --panel-soft: #f9fbfd;
      --panel-soft-2: #f4f7fa;
      --panel-soft-3: #f6f9fc;
      --surface-warn: #fff9ec;
      --surface-info: #eef6fd;
      --surface-danger: #fff1f1;
      --surface-menu: rgba(255,255,255,0.98);
      --ink: #17212b;
      --muted: #617282;
      --line: #d7e0e8;
      --line-strong: #b8c5d1;
      --accent: #174a7c;
      --accent-soft: #edf4fb;
      --accent-contrast: #ffffff;
      --success: #0b6b61;
      --success-soft: #edf7f5;
      --danger: #8c1d18;
      --danger-soft: #fbefef;
      --violet: #6e56cf;
      --violet-soft: #f1edff;
      --violet-line: #d9cffb;
      --secondary-contrast: #2f245f;
      --shadow: 0 12px 34px rgba(20, 41, 61, 0.08);
      --radius: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font: 15px/1.5 "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      min-height: 100vh;
      transition: background-color .22s ease, color .22s ease;
    }
    .app-shell {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      background:
        radial-gradient(circle at top right, rgba(110, 86, 207, 0.32), transparent 32%),
        linear-gradient(180deg, var(--sidebar) 0%, #173858 100%);
      color: var(--sidebar-ink);
      padding: 28px 20px;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 22px;
      border-right: 1px solid rgba(255,255,255,0.08);
    }
    .sidebar-brand {
      display: grid;
      gap: 4px;
    }
    .sidebar-kicker {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--sidebar-muted-strong);
      font-weight: 700;
    }
    .sidebar-title {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .sidebar-subtitle {
      font-size: 13px;
      color: var(--sidebar-muted);
    }
    .sidebar-status {
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
      border-radius: 14px;
      padding: 14px;
      display: grid;
      gap: 10px;
    }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.12);
      border: 1px solid rgba(255,255,255,0.12);
      font-weight: 700;
    }
    .sidebar-nav {
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .nav-btn {
      border: 1px solid transparent;
      background: transparent;
      color: var(--sidebar-ink);
      border-radius: 12px;
      padding: 12px 14px;
      text-align: left;
      cursor: pointer;
      font-weight: 600;
      transition: transform .18s ease, background-color .18s ease, border-color .18s ease, opacity .18s ease;
    }
    .nav-btn:hover, .nav-btn.active {
      background: rgba(110, 86, 207, 0.18);
      border-color: rgba(177, 157, 245, 0.28);
    }
    .nav-btn:hover {
      transform: translateX(2px);
    }
    .sidebar-foot {
      font-size: 12px;
      color: var(--sidebar-foot);
    }
    .main {
      padding: 24px;
      display: grid;
      gap: 18px;
      align-content: start;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      transition: background-color .22s ease, border-color .22s ease, box-shadow .22s ease, transform .22s ease;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .summary-item {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background:
        linear-gradient(180deg, rgba(110, 86, 207, 0.05), transparent 54%),
        var(--panel-soft);
    }
    .summary-item strong {
      display: block;
      font-size: 20px;
      letter-spacing: -0.02em;
    }
    .summary-item span {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
    }
    .summary-item strong.small {
      font-size: 15px;
      line-height: 1.3;
    }
    .content-grid {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    .section {
      display: none;
      gap: 16px;
      opacity: 0;
      transform: translateY(6px);
    }
    .section.active {
      display: grid;
      opacity: 1;
      transform: translateY(0);
      animation: sectionFadeIn .2s ease;
    }
    .hidden-by-access {
      display: none !important;
    }
    .panel {
      padding: 20px;
    }
    .panel h2 {
      margin: 0 0 6px;
      font-size: 21px;
      letter-spacing: -0.02em;
    }
    .panel p {
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 14px;
    }
    label {
      display: block;
      margin: 0 0 8px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      font-weight: 700;
    }
    input, select, textarea, button {
      font: inherit;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line-strong);
      background: var(--panel);
      color: var(--ink);
      border-radius: 10px;
      padding: 11px 12px;
      outline: none;
      transition: border-color .18s ease, box-shadow .18s ease, background-color .18s ease, color .18s ease;
    }
    input:focus, select:focus, textarea:focus {
      border-color: var(--violet);
      box-shadow: 0 0 0 3px rgba(110, 86, 207, 0.12);
    }
    textarea { min-height: 118px; resize: vertical; }
    .row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }
    button {
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 700;
      outline: none;
      transition: transform .18s ease, background-color .18s ease, border-color .18s ease, box-shadow .18s ease, opacity .18s ease;
    }
    button:hover { transform: translateY(-1px); }
    button:focus {
      outline: none;
      box-shadow: none;
    }
    button:focus-visible,
    .nav-btn:focus-visible,
    .mini-btn:focus-visible,
    .icon-btn:focus-visible {
      outline: none;
      box-shadow: 0 0 0 2px var(--panel), 0 0 0 4px rgba(110, 86, 207, 0.22);
    }
    button.primary {
      background: linear-gradient(135deg, var(--accent) 0%, var(--violet) 100%);
      color: var(--accent-contrast);
    }
    button.secondary {
      background: linear-gradient(180deg, var(--violet-soft), var(--panel-soft));
      color: var(--secondary-contrast);
      border-color: var(--violet-line);
    }
    button.ghost {
      background: var(--panel-soft-2);
      color: var(--ink);
      border-color: var(--line);
    }
    .result {
      margin-top: 18px;
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid var(--line);
      background: var(--panel-soft);
    }
    .result-header {
      display: flex;
      justify-content: space-between;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(90deg, rgba(110, 86, 207, 0.08), transparent 70%),
        var(--panel-soft-3);
      font-weight: 700;
    }
    pre {
      margin: 0;
      padding: 14px;
      overflow: auto;
      font-size: 13px;
      line-height: 1.45;
      background: var(--panel);
    }
    .week-toolbar {
      display: grid;
      grid-template-columns: auto 1fr auto auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }
    .week-label {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 11px 12px;
      background:
        linear-gradient(90deg, rgba(110, 86, 207, 0.08), transparent 62%),
        var(--panel-soft);
      font-weight: 700;
    }
    .calendar {
      display: grid;
      grid-template-columns: repeat(7, minmax(180px, 1fr));
      gap: 12px;
      overflow-x: auto;
      padding-bottom: 4px;
    }
    .calendar-day {
      min-height: 260px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      display: grid;
      grid-template-rows: auto 1fr;
      overflow: hidden;
      transition: background-color .22s ease, border-color .22s ease, box-shadow .22s ease;
    }
    .calendar-head {
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background:
        linear-gradient(180deg, rgba(110, 86, 207, 0.07), transparent 70%),
        var(--panel-soft-3);
    }
    .calendar-head strong {
      display: block;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .calendar-head span {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .calendar-head button {
      margin-top: 8px;
      width: 100%;
    }
    .calendar-body {
      padding: 12px;
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .calendar-empty {
      color: var(--muted);
      font-size: 13px;
      padding: 10px;
      border-radius: 10px;
      background: var(--panel-soft);
      border: 1px dashed var(--line);
    }
    .agenda-item {
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 10px;
      padding: 10px;
      background: var(--panel);
      cursor: grab;
      transform-origin: center center;
      transition: transform .16s ease, box-shadow .18s ease, border-color .18s ease, background-color .18s ease, opacity .18s ease, filter .18s ease;
    }
    .agenda-item:hover {
      box-shadow: 0 10px 24px rgba(17, 27, 39, 0.08);
      transform: translateY(-1px);
    }
    .agenda-item.dragging {
      opacity: 0.96;
      transform: scale(1.018) rotate(-0.45deg);
      border-color: rgba(255,255,255,0.28);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.06)),
        linear-gradient(135deg, rgba(255,255,255,0.14), rgba(110, 86, 207, 0.08)),
        color-mix(in srgb, var(--panel) 88%, white 12%);
      box-shadow:
        0 22px 44px rgba(15, 23, 36, 0.2),
        inset 0 1px 0 rgba(255,255,255,0.32);
      backdrop-filter: blur(16px) saturate(1.18);
      -webkit-backdrop-filter: blur(16px) saturate(1.18);
      filter: saturate(1.03);
    }
    .agenda-item.pending {
      border-left-color: #c07a11;
      background: var(--surface-warn);
    }
    .agenda-item.in_progress {
      border-left-color: #0e67a5;
      background: var(--surface-info);
    }
    .agenda-item.canceled {
      border-left-color: #9b2c2c;
      background: var(--surface-danger);
    }
    .agenda-item strong {
      display: block;
      font-size: 14px;
      margin-bottom: 4px;
    }
    .agenda-item p {
      margin: 0 0 6px;
      color: var(--ink);
      font-size: 13px;
      line-height: 1.35;
    }
    .meta, .analysis-summary {
      color: var(--muted);
      font-size: 14px;
    }
    .agenda-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 8px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }
    .pill.status-not_started {
      background: #fff2d9;
      border-color: #f1d08a;
      color: #8c6210;
    }
    .pill.status-in_progress {
      background: #e8f2fd;
      border-color: #bad6f3;
      color: #175588;
    }
    .pill.status-canceled {
      background: #ffecec;
      border-color: #efc1c1;
      color: #8c1d18;
    }
    .pill.category-EMPLOI {
      background: #eaf3ff;
      border-color: #c8dcf4;
      color: #174a7c;
    }
    .pill.category-CITOYENNETE {
      background: #edf7f5;
      border-color: #cce6e0;
      color: #0b6b61;
    }
    .pill.category-FORMATION {
      background: #f4efff;
      border-color: #dbcef7;
      color: #5a3aa0;
    }
    .pill.category-PROJET_PROFESSIONNEL {
      background: #fff1e8;
      border-color: #f3d2ba;
      color: #9a4b12;
    }
    .pill.category-CULTURE_SPORT_LOISIRS {
      background: #fff3fb;
      border-color: #efcde0;
      color: #a63f7a;
    }
    .pill.category-LOGEMENT {
      background: #f2f4f7;
      border-color: #d8dee6;
      color: #4d6276;
    }
    .pill.category-SANTE {
      background: #fff0f0;
      border-color: #efcccc;
      color: #9b2c2c;
    }
    .mini-btn {
      padding: 5px 8px;
      border-radius: 8px;
      background: var(--panel-soft-2);
      border: 1px solid var(--line);
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }
    .danger-btn {
      background: var(--danger-soft);
      border-color: #ebcccc;
      color: var(--danger);
    }
    .context-menu {
      position: fixed;
      z-index: 9999;
      min-width: 210px;
      padding: 8px;
      border-radius: 12px;
      border: 1px solid var(--violet-line);
      background: linear-gradient(180deg, var(--violet-soft), var(--surface-menu));
      box-shadow: 0 18px 42px rgba(20, 41, 61, 0.18);
      display: none;
      gap: 4px;
      backdrop-filter: blur(10px);
      opacity: 0;
      transform: translateY(6px) scale(0.98);
      transition: opacity .16s ease, transform .16s ease;
    }
    .context-menu.open {
      display: grid;
      opacity: 1;
      transform: translateY(0) scale(1);
    }
    .context-menu button {
      width: 100%;
      text-align: left;
      padding: 9px 10px;
      border-radius: 9px;
      background: transparent;
      color: var(--ink);
      border: 1px solid transparent;
      font-size: 13px;
      font-weight: 700;
    }
    .context-menu button:hover {
      background: var(--violet-soft);
      border-color: var(--violet-line);
      transform: none;
    }
    .context-menu button.danger {
      color: var(--danger);
      background: var(--danger-soft);
      border-color: #ebcccc;
    }
    .context-menu button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .drop-target {
      box-shadow: inset 0 0 0 2px #6d97bf;
      background: #f5faff;
      transform: scale(1.01);
      transition: box-shadow .16s ease, background-color .16s ease, transform .16s ease;
    }
    .week-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .notice {
      min-height: 22px;
      color: var(--muted);
      font-size: 14px;
      transition: color .18s ease, opacity .18s ease, transform .18s ease;
    }
    .notice.ok {
      color: var(--success);
    }
    .notice.error {
      color: var(--danger);
    }
    .week-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: var(--panel);
    }
    .week-card.missing {
      background: var(--danger-soft);
      border-color: #ebcccc;
    }
    .theme-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .theme-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background: var(--panel-soft);
      display: grid;
      gap: 10px;
    }
    .theme-preview {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 8px;
    }
    .theme-chip {
      border-radius: 10px;
      padding: 10px 12px;
      font-weight: 700;
      border: 1px solid transparent;
      text-align: center;
    }
    .modal-overlay {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(9, 14, 20, 0.28);
      backdrop-filter: blur(6px);
      z-index: 12000;
      opacity: 0;
      transition: opacity .16s ease;
    }
    .modal-overlay.open {
      display: flex;
      opacity: 1;
    }
    .modal-card {
      width: min(560px, calc(100vw - 32px));
      max-height: calc(100vh - 48px);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel);
      box-shadow: 0 24px 56px rgba(12, 18, 28, 0.28);
      padding: 20px;
      display: grid;
      gap: 14px;
      transform: translateY(8px) scale(0.985);
      transition: transform .18s ease, background-color .22s ease, border-color .22s ease;
    }
    .modal-overlay.open .modal-card {
      transform: translateY(0) scale(1);
    }
    .modal-head {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
    }
    .modal-head h3 {
      margin: 0;
      font-size: 22px;
      letter-spacing: -0.02em;
    }
    .modal-head p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    .icon-btn {
      width: 38px;
      height: 38px;
      padding: 0;
      border-radius: 10px;
      background: var(--panel-soft-2);
      border: 1px solid var(--line);
      color: var(--ink);
    }
    @keyframes sectionFadeIn {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        animation: none !important;
        transition-duration: 0.01ms !important;
        transition-delay: 0ms !important;
        scroll-behavior: auto !important;
      }
      .agenda-item.dragging {
        transform: none;
        backdrop-filter: none;
        -webkit-backdrop-filter: none;
      }
    }
    .ok { color: var(--success); }
    .error { color: var(--danger); }
    @media (max-width: 1100px) {
      .app-shell {
        grid-template-columns: 1fr;
      }
      .sidebar {
        grid-template-rows: auto auto auto auto;
      }
      .content-grid {
        grid-template-columns: 1fr;
      }
      .theme-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 720px) {
      .row, .week-toolbar, .summary-grid {
        grid-template-columns: 1fr;
      }
    }
    body.login-only {
      background: #e9eef5;
    }
    body.login-only .app-shell {
      grid-template-columns: 1fr;
      min-height: 100vh;
    }
    body.login-only .sidebar {
      display: none;
    }
    body.login-only .main {
      min-height: 100vh;
      align-content: center;
      justify-content: center;
      padding: 32px;
    }
    body.login-only #loginSection {
      display: block;
      width: min(480px, calc(100vw - 32px));
      margin: 0 auto;
    }
    body.login-only .content-grid {
      display: block;
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <div class="sidebar-kicker">Pass Emploi</div>
        <div class="sidebar-title">Tableau de bord CEJ</div>
        <div class="sidebar-subtitle">Gestion locale des actions, saisie rapide et controle hebdomadaire.</div>
      </div>

      <nav class="sidebar-nav">
        <button class="nav-btn active" data-section="agendaSection" data-requires-session="true">Agenda hebdomadaire</button>
        <button class="nav-btn" data-section="loginSection">Parametre</button>
      </nav>

      <div class="sidebar-foot">Session locale stockee sur cette machine. Les scripts Python restent la source de verite.</div>
    </aside>

    <main class="main">
      <section id="agendaSection" class="section active" data-requires-session="true">
        <div class="card panel">
          <h2>Agenda hebdomadaire</h2>
          <div class="summary-grid" style="margin-bottom:16px">
            <div class="summary-item"><span>Semaine</span><strong id="summaryWeekCount" class="small">-</strong></div>
            <div class="summary-item"><span>Actions</span><strong id="summaryActions">0</strong></div>
            <div class="summary-item"><span>Heures estimees</span><strong id="summaryHours">0h</strong></div>
            <div class="summary-item"><span>Manque CEJ</span><strong id="summaryMissing">0h</strong></div>
          </div>
          <div class="week-toolbar">
            <button class="ghost" id="prevWeekBtn" type="button">← Semaine precedente</button>
            <div id="weekLabel" class="week-label">Semaine en cours</div>
            <button class="ghost" id="currentWeekBtn" type="button">Semaine actuelle</button>
            <button class="ghost" id="nextWeekBtn" type="button">Semaine suivante →</button>
          </div>
          <div class="row">
            <div>
              <label for="fromDate">Du</label>
              <input id="fromDate" type="date" readonly>
            </div>
            <div>
              <label for="toDate">Au</label>
              <input id="toDate" type="date" readonly>
            </div>
          </div>
          <div class="actions">
            <button class="primary" id="listBtn">Charger la semaine</button>
            <button class="secondary" id="analyzeBtn">Analyser la semaine</button>
            <button class="ghost" id="quickCreateBtn">Creer une action</button>
          </div>
          <div id="agendaNotice" class="notice" style="margin-top:14px"></div>
          <div id="listSummary" class="meta" style="margin-top:8px"></div>
          <div id="calendarGrid" class="calendar" style="margin-top:14px"></div>
          <div id="analysisSummary" class="analysis-summary" style="margin-top:18px"></div>
          <div id="weeksGrid" class="week-grid"></div>
        </div>
      </section>

      <section id="loginSection" class="section">
        <div class="content-grid">
          <div class="card panel">
            <h2>Parametre</h2>
            <div class="sidebar-status" style="margin-bottom:16px;background:var(--panel-soft-3);border-color:var(--line);">
              <div id="statusBadge" class="status-badge">Session inconnue</div>
              <div id="sessionMeta" class="meta">Chargement...</div>
            </div>
            <div>
              <label for="username">Identifiant</label>
              <input id="username" autocomplete="username">
            </div>
            <div style="margin-top:12px">
              <label for="password">Mot de passe</label>
              <input id="password" type="password" autocomplete="current-password">
            </div>
            <div style="margin-top:12px">
              <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
                <input id="rememberMe" type="checkbox" style="width:auto;">
                Se souvenir de moi
              </label>
            </div>
            <div class="actions">
              <button class="primary" id="loginBtn">Se connecter</button>
              <button class="secondary" id="refreshBtn">Rafraichir le token</button>
              <button class="ghost" id="logoutBtn">Se deconnecter</button>
            </div>
            <div id="loginNotice" class="notice" style="margin-top:16px"></div>
          </div>
          <div class="card panel" data-requires-session="true">
            <h2>Apparence</h2>
            <div class="theme-grid">
              <div class="theme-card">
                <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
                  <input id="darkMode" type="checkbox" style="width:auto;">
                  Activer le mode sombre
                </label>
                <div>
                  <label for="primaryColor">Couleur principale</label>
                  <input id="primaryColor" type="color" value="#174a7c">
                </div>
                <div>
                  <label for="secondaryColor">Couleur secondaire</label>
                  <input id="secondaryColor" type="color" value="#6e56cf">
                </div>
                <div class="actions">
                  <button class="secondary" id="applyThemeBtn" type="button">Appliquer</button>
                  <button class="ghost" id="resetThemeBtn" type="button">Reinitialiser</button>
                </div>
              </div>
              <div class="theme-card">
                <div class="meta">Apercu</div>
                <div class="theme-preview">
                  <div id="primaryPreview" class="theme-chip">Principale</div>
                  <div id="secondaryPreview" class="theme-chip">Secondaire</div>
                </div>
                <div id="themeNotice" class="notice"></div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <div id="contextMenu" class="context-menu" aria-hidden="true"></div>
  <div id="editModal" class="modal-overlay" aria-hidden="true">
    <div class="modal-card">
      <div class="modal-head">
        <div>
          <h3>Modifier l’action</h3>
          <p>Tu peux mettre à jour le titre, la description, la catégorie, la date et le statut.</p>
        </div>
        <button id="closeEditModalBtn" class="icon-btn" type="button" aria-label="Fermer">×</button>
      </div>
      <div>
        <label for="editTitle">Titre</label>
        <input id="editTitle" type="text">
      </div>
      <div>
        <label for="editComment">Description</label>
        <textarea id="editComment"></textarea>
      </div>
      <div class="row">
        <div>
          <label for="editDue">Date</label>
          <input id="editDue" type="date">
        </div>
        <div>
          <label for="editQualification">Categorie</label>
          <select id="editQualification">
            <option>EMPLOI</option>
            <option>PROJET_PROFESSIONNEL</option>
            <option>CULTURE_SPORT_LOISIRS</option>
            <option>CITOYENNETE</option>
            <option>FORMATION</option>
            <option>LOGEMENT</option>
            <option>SANTE</option>
          </select>
        </div>
      </div>
      <div>
        <label for="editStatus">Statut</label>
        <select id="editStatus">
          <option value="not_started">not_started</option>
          <option value="in_progress">in_progress</option>
          <option value="canceled">canceled</option>
          <option value="done">done</option>
        </select>
      </div>
      <div id="editNotice" class="notice"></div>
      <div class="actions">
        <button id="saveEditBtn" class="primary" type="button">Enregistrer</button>
        <button id="cancelEditBtn" class="ghost" type="button">Annuler</button>
      </div>
    </div>
  </div>
  <div id="deleteModal" class="modal-overlay" aria-hidden="true">
    <div class="modal-card" style="width:min(460px, calc(100vw - 32px));">
      <div class="modal-head">
        <div>
          <h3>Supprimer l’action</h3>
          <p id="deleteMessage">Confirmation de suppression.</p>
        </div>
        <button id="closeDeleteModalBtn" class="icon-btn" type="button" aria-label="Fermer">×</button>
      </div>
      <div id="deleteNotice" class="notice"></div>
      <div class="actions">
        <button id="confirmDeleteBtn" class="primary" type="button">Supprimer</button>
        <button id="cancelDeleteBtn" class="ghost" type="button">Annuler</button>
      </div>
    </div>
  </div>

  <script>
    const els = {
      statusBadge: document.getElementById('statusBadge'),
      sessionMeta: document.getElementById('sessionMeta'),
      loginNotice: document.getElementById('loginNotice'),
      agendaNotice: document.getElementById('agendaNotice'),
      themeNotice: document.getElementById('themeNotice'),
      listSummary: document.getElementById('listSummary'),
      analysisSummary: document.getElementById('analysisSummary'),
      weeksGrid: document.getElementById('weeksGrid'),
      weekLabel: document.getElementById('weekLabel'),
      calendarGrid: document.getElementById('calendarGrid'),
      summaryWeekCount: document.getElementById('summaryWeekCount'),
      summaryHours: document.getElementById('summaryHours'),
      summaryActions: document.getElementById('summaryActions'),
      summaryMissing: document.getElementById('summaryMissing'),
      contextMenu: document.getElementById('contextMenu'),
      darkMode: document.getElementById('darkMode'),
      primaryColor: document.getElementById('primaryColor'),
      secondaryColor: document.getElementById('secondaryColor'),
      primaryPreview: document.getElementById('primaryPreview'),
      secondaryPreview: document.getElementById('secondaryPreview'),
      editModal: document.getElementById('editModal'),
      editTitle: document.getElementById('editTitle'),
      editComment: document.getElementById('editComment'),
      editDue: document.getElementById('editDue'),
      editQualification: document.getElementById('editQualification'),
      editStatus: document.getElementById('editStatus'),
      editNotice: document.getElementById('editNotice'),
      deleteModal: document.getElementById('deleteModal'),
      deleteMessage: document.getElementById('deleteMessage'),
      deleteNotice: document.getElementById('deleteNotice'),
    };

    const weekdayNames = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche'];
    const weeklyTargetHours = 15;
    const hoursPerAction = 2;
    const defaultTheme = {
      darkMode: false,
      primary: '#174a7c',
      secondary: '#6e56cf',
    };
    let clipboard = null;
    let contextMenuState = null;
    let editingAction = null;
    let deletingAction = null;
    let selectedWeekStart = startOfWeek(new Date());
    syncWeekInputs();

    async function api(path, body) {
      const response = await fetch(path, {
        method: body ? 'POST' : 'GET',
        headers: body ? {'Content-Type': 'application/json'} : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      return response.json();
    }

    function setNotice(el, ok, message) {
      if (!el) return;
      el.textContent = message || '';
      el.className = `notice${message ? (ok ? ' ok' : ' error') : ''}`;
    }

    function normalizeHex(value, fallback) {
      if (typeof value !== 'string') return fallback;
      const cleaned = value.trim().toLowerCase();
      if (/^#[0-9a-f]{6}$/.test(cleaned)) return cleaned;
      return fallback;
    }

    function hexToRgb(hex) {
      const normalized = normalizeHex(hex, '#000000');
      return {
        r: parseInt(normalized.slice(1, 3), 16),
        g: parseInt(normalized.slice(3, 5), 16),
        b: parseInt(normalized.slice(5, 7), 16),
      };
    }

    function rgbToHex({ r, g, b }) {
      const toHex = value => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, '0');
      return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    }

    function mix(hexA, hexB, ratio) {
      const a = hexToRgb(hexA);
      const b = hexToRgb(hexB);
      return rgbToHex({
        r: a.r + (b.r - a.r) * ratio,
        g: a.g + (b.g - a.g) * ratio,
        b: a.b + (b.b - a.b) * ratio,
      });
    }

    function luminance(hex) {
      const { r, g, b } = hexToRgb(hex);
      const channel = value => {
        const normalized = value / 255;
        return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    }

    function contrastText(hex) {
      return luminance(hex) > 0.52 ? '#17212b' : '#f8fbff';
    }

    function rgba(hex, alpha) {
      const { r, g, b } = hexToRgb(hex);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function applyTheme(settings, persist = true) {
      const theme = {
        darkMode: !!settings.darkMode,
        primary: normalizeHex(settings.primary, defaultTheme.primary),
        secondary: normalizeHex(settings.secondary, defaultTheme.secondary),
      };
      const root = document.documentElement;
      const isDark = theme.darkMode;
      const primaryContrast = contrastText(theme.primary);
      const secondaryContrast = contrastText(theme.secondary);
      const bg = isDark ? mix(theme.primary, '#0a1018', 0.88) : '#edf2f7';
      const panel = isDark ? mix(theme.primary, '#0f1722', 0.84) : '#ffffff';
      const panelSoft = isDark ? mix(panel, '#ffffff', 0.04) : '#f9fbfd';
      const panelSoft2 = isDark ? mix(panel, '#ffffff', 0.08) : '#f4f7fa';
      const panelSoft3 = isDark ? mix(panel, '#ffffff', 0.1) : '#f6f9fc';
      const surfaceWarn = isDark ? mix('#c07a11', panel, 0.82) : '#fff9ec';
      const surfaceInfo = isDark ? mix('#0e67a5', panel, 0.82) : '#eef6fd';
      const surfaceDanger = isDark ? mix('#9b2c2c', panel, 0.82) : '#fff1f1';
      const surfaceMenu = isDark ? mix(panel, '#ffffff', 0.06) : '#ffffff';
      const ink = isDark ? '#edf3fb' : '#17212b';
      const muted = isDark ? '#a6b4c6' : '#617282';
      const line = isDark ? mix(panel, '#ffffff', 0.14) : '#d7e0e8';
      const lineStrong = isDark ? mix(panel, '#ffffff', 0.24) : '#b8c5d1';
      const sidebar = isDark ? mix(theme.primary, '#08111b', 0.72) : '#12304d';
      const sidebarSoft = isDark ? mix(theme.secondary, '#122236', 0.68) : '#1e456b';
      const sidebarInk = contrastText(sidebar);

      root.style.setProperty('--bg', bg);
      root.style.setProperty('--panel', panel);
      root.style.setProperty('--panel-soft', panelSoft);
      root.style.setProperty('--panel-soft-2', panelSoft2);
      root.style.setProperty('--panel-soft-3', panelSoft3);
      root.style.setProperty('--surface-warn', surfaceWarn);
      root.style.setProperty('--surface-info', surfaceInfo);
      root.style.setProperty('--surface-danger', surfaceDanger);
      root.style.setProperty('--surface-menu', surfaceMenu);
      root.style.setProperty('--ink', ink);
      root.style.setProperty('--muted', muted);
      root.style.setProperty('--line', line);
      root.style.setProperty('--line-strong', lineStrong);
      root.style.setProperty('--accent', theme.primary);
      root.style.setProperty('--accent-soft', mix(theme.primary, isDark ? panel : '#ffffff', isDark ? 0.84 : 0.9));
      root.style.setProperty('--accent-contrast', primaryContrast);
      root.style.setProperty('--violet', theme.secondary);
      root.style.setProperty('--violet-soft', mix(theme.secondary, isDark ? panel : '#ffffff', isDark ? 0.82 : 0.88));
      root.style.setProperty('--violet-line', mix(theme.secondary, isDark ? '#ffffff' : '#d7e0e8', isDark ? 0.28 : 0.45));
      root.style.setProperty('--secondary-contrast', isDark ? secondaryContrast : mix(theme.secondary, '#111111', 0.55));
      root.style.setProperty('--sidebar', sidebar);
      root.style.setProperty('--sidebar-soft', sidebarSoft);
      root.style.setProperty('--sidebar-ink', sidebarInk);
      root.style.setProperty('--sidebar-muted', rgba(sidebarInk, 0.72));
      root.style.setProperty('--sidebar-muted-strong', rgba(sidebarInk, 0.68));
      root.style.setProperty('--sidebar-foot', rgba(sidebarInk, 0.58));
      root.style.setProperty('--shadow', isDark ? '0 16px 40px rgba(0, 0, 0, 0.34)' : '0 12px 34px rgba(20, 41, 61, 0.08)');

      els.primaryColor.value = theme.primary;
      els.secondaryColor.value = theme.secondary;
      els.darkMode.checked = theme.darkMode;
      els.primaryPreview.style.background = theme.primary;
      els.primaryPreview.style.color = primaryContrast;
      els.primaryPreview.style.borderColor = mix(theme.primary, isDark ? '#ffffff' : '#000000', isDark ? 0.18 : 0.08);
      els.secondaryPreview.style.background = theme.secondary;
      els.secondaryPreview.style.color = secondaryContrast;
      els.secondaryPreview.style.borderColor = mix(theme.secondary, isDark ? '#ffffff' : '#000000', isDark ? 0.18 : 0.08);

      if (persist) {
        api('/api/theme', theme).catch(() => null);
      }
    }

    function startOfWeek(date) {
      const copy = new Date(date);
      const day = copy.getDay();
      const diff = day === 0 ? -6 : 1 - day;
      copy.setDate(copy.getDate() + diff);
      copy.setHours(0, 0, 0, 0);
      return copy;
    }

    function addDays(date, days) {
      const copy = new Date(date);
      copy.setDate(copy.getDate() + days);
      return copy;
    }

    function formatDateInput(date) {
      return date.toISOString().slice(0, 10);
    }

    function formatDateLabel(date) {
      return date.toLocaleDateString('fr-FR', { day: '2-digit', month: 'long', year: 'numeric' });
    }

    function syncWeekInputs() {
      const weekEnd = addDays(selectedWeekStart, 6);
      document.getElementById('fromDate').value = formatDateInput(selectedWeekStart);
      document.getElementById('toDate').value = formatDateInput(weekEnd);
      els.weekLabel.textContent = `Semaine du ${formatDateLabel(selectedWeekStart)} au ${formatDateLabel(weekEnd)}`;
      els.summaryWeekCount.textContent = `${formatDateInput(selectedWeekStart)} → ${formatDateInput(weekEnd)}`;
    }

    function switchSection(sectionId) {
      document.querySelectorAll('.section').forEach(section => {
        section.classList.toggle('active', section.id === sectionId);
      });
      document.querySelectorAll('.nav-btn').forEach(button => {
        button.classList.toggle('active', button.dataset.section === sectionId);
      });
    }

    function setProtectedVisibility(connected) {
      document.querySelectorAll('[data-requires-session="true"]').forEach(node => {
        node.classList.toggle('hidden-by-access', !connected);
      });
    }

    function updateSession(session) {
      if (session && session.connected) {
        document.body.classList.remove('login-only');
        els.statusBadge.textContent = 'Session active';
        els.statusBadge.style.background = 'rgba(11,107,97,0.18)';
        els.statusBadge.style.borderColor = 'rgba(255,255,255,0.16)';
        els.statusBadge.style.color = '#f1fffd';
        els.sessionMeta.textContent = `user_id=${session.user_id || '?'} | expire=${session.expires_at || '?'} | refresh=${session.has_refresh_token ? 'oui' : 'non'}`;
        setProtectedVisibility(true);
      } else {
        document.body.classList.add('login-only');
        els.statusBadge.textContent = 'Non connecte';
        els.statusBadge.style.background = 'rgba(140,29,24,0.18)';
        els.statusBadge.style.borderColor = 'rgba(255,255,255,0.12)';
        els.statusBadge.style.color = '#fff0ef';
        els.sessionMeta.textContent = 'Aucune session locale detectee.';
        setProtectedVisibility(false);
        switchSection('loginSection');
        setNotice(els.agendaNotice, false, '');
      }

      const username = document.getElementById('username');
      const password = document.getElementById('password');
      const rememberMe = document.getElementById('rememberMe');
      if (session && typeof session.remembered_username === 'string') username.value = session.remembered_username;
      if (session && typeof session.remembered_password === 'string') password.value = session.remembered_password;
      rememberMe.checked = !!(session && session.remember_me);
    }

    function looksUnauthorized(result) {
      const text = JSON.stringify(result || {}).toLowerCase();
      return text.includes('missing token') || text.includes('no stored session') || text.includes('http 401') || text.includes('http 403');
    }

    function applyUnauthorizedState(result) {
      if (!looksUnauthorized(result)) return;
      updateSession({ connected: false });
      els.calendarGrid.innerHTML = '';
      els.listSummary.textContent = '';
      renderAnalysis(null);
      els.summaryActions.textContent = '0';
      els.summaryHours.textContent = '0h';
      els.summaryMissing.textContent = `${weeklyTargetHours}h`;
      setNotice(els.loginNotice, false, result?.error || 'Session invalide.');
      switchSection('loginSection');
    }

    function summarizeAttempts(result) {
      if (!Array.isArray(result?.attempts)) return '';
      return result.attempts.map(item => `${item.mode}:${item.ok ? 'ok' : 'miss'}`).join(' | ');
    }

    function setLoginResult(result) {
      const attemptSummary = summarizeAttempts(result);
      const baseMessage = result.ok ? 'Connexion reussie.' : (result?.error || 'Echec de connexion.');
      setNotice(els.loginNotice, result.ok, attemptSummary ? `${baseMessage} ${attemptSummary}` : baseMessage);
    }

    function normalizeActionDate(action) {
      const raw = action.dateFinReelle || action.dateEcheance || action.dateCreation;
      if (!raw) return null;
      const parsed = new Date(raw);
      if (Number.isNaN(parsed.getTime())) return null;
      return parsed.toISOString().slice(0, 10);
    }

    function categoryLabel(action) {
      return action.qualification?.code || 'Sans categorie';
    }

    function statusPill(action) {
      if (!action.status || action.status === 'done') return '';
      return `<span class="pill status-${action.status}">${action.status.replace('_', ' ')}</span>`;
    }

    function categoryPill(action) {
      const code = categoryLabel(action);
      return `<span class="pill category-${code}">${code}</span>`;
    }

    function cloneActionForClipboard(action, mode) {
      clipboard = {
        mode,
        action: {
          id: action.id,
          content: action.content || '',
          comment: action.comment || '',
          status: action.status || 'not_started',
          qualification: action.qualification?.code || 'EMPLOI',
        },
      };
      els.listSummary.textContent = `${els.listSummary.textContent} | Presse "Coller ici" sur un jour pour ${mode === 'cut' ? 'deplacer' : 'copier'} l'action.`;
    }

    function hideContextMenu() {
      contextMenuState = null;
      els.contextMenu.classList.remove('open');
      els.contextMenu.setAttribute('aria-hidden', 'true');
      els.contextMenu.innerHTML = '';
    }

    function openModal(modal) {
      modal.classList.add('open');
      modal.setAttribute('aria-hidden', 'false');
      hideContextMenu();
    }

    function closeModal(modal) {
      modal.classList.remove('open');
      modal.setAttribute('aria-hidden', 'true');
    }

    function showContextMenu(x, y, items) {
      const validItems = items.filter(item => item && item.label);
      if (!validItems.length) {
        hideContextMenu();
        return;
      }

      els.contextMenu.innerHTML = '';
      for (const item of validItems) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = item.label;
        if (item.className) button.className = item.className;
        if (item.disabled) button.disabled = true;
        button.addEventListener('click', async () => {
          hideContextMenu();
          if (!item.disabled && typeof item.onClick === 'function') {
            await item.onClick();
          }
        });
        els.contextMenu.appendChild(button);
      }

      els.contextMenu.style.left = '0px';
      els.contextMenu.style.top = '0px';
      els.contextMenu.classList.add('open');
      els.contextMenu.setAttribute('aria-hidden', 'false');
      contextMenuState = { open: true };

      const { innerWidth, innerHeight } = window;
      const rect = els.contextMenu.getBoundingClientRect();
      const maxLeft = Math.max(12, innerWidth - rect.width - 12);
      const maxTop = Math.max(12, innerHeight - rect.height - 12);
      els.contextMenu.style.left = `${Math.min(x, maxLeft)}px`;
      els.contextMenu.style.top = `${Math.min(y, maxTop)}px`;
    }

    async function pasteToDate(targetDate) {
      if (!clipboard) return;
      if (clipboard.mode === 'cut') {
        const result = await api('/api/update-action', {
          id: clipboard.action.id,
          due: targetDate,
        });
        setNotice(els.agendaNotice, result.ok, result.ok ? 'Action deplacee.' : (result?.error || 'Echec du deplacement.'));
        if (result.ok) {
          clipboard = null;
          await loadCurrentWeek(false);
        }
        return;
      }

      const result = await api('/api/duplicate-action', {
        title: clipboard.action.content,
        comment: clipboard.action.comment,
        due: targetDate,
        qualification: clipboard.action.qualification,
        status: clipboard.action.status,
      });
      setNotice(els.agendaNotice, result.ok, result.ok ? 'Action collee.' : (result?.error || 'Echec du collage.'));
      if (result.ok) {
        clipboard = null;
        await loadCurrentWeek(false);
      }
    }

    function editAction(action) {
      editingAction = action;
      els.editTitle.value = action.content || '';
      els.editComment.value = action.comment || '';
      els.editDue.value = normalizeActionDate(action) || '';
      els.editQualification.value = action.qualification?.code || 'EMPLOI';
      els.editStatus.value = action.status || 'not_started';
      setNotice(els.editNotice, true, '');
      openModal(els.editModal);
    }

    function deleteAction(action) {
      deletingAction = action;
      els.deleteMessage.textContent = `Supprimer definitivement "${action.content || 'cette action'}" ?`;
      setNotice(els.deleteNotice, true, '');
      openModal(els.deleteModal);
    }

    async function quickCreateAction() {
      const title = window.prompt('Titre de l’action', '');
      if (title === null || !title.trim()) return;
      const comment = window.prompt('Commentaire', '');
      if (comment === null) return;
      const due = window.prompt('Date (YYYY-MM-DD)', formatDateInput(selectedWeekStart));
      if (due === null || !due.trim()) return;
      const qualification = window.prompt('Categorie', 'EMPLOI');
      if (qualification === null || !qualification.trim()) return;

      const result = await api('/api/create', {
        title,
        comment,
        due,
        qualification,
      });
      setNotice(els.agendaNotice, result.ok, result.ok ? 'Action creee.' : (result?.error || 'Echec de creation.'));
      if (result.ok) await loadCurrentWeek(false);
      applyUnauthorizedState(result);
    }

    function openActionContextMenu(event, action) {
      event.preventDefault();
      event.stopPropagation();
      showContextMenu(event.clientX, event.clientY, [
        { label: 'Modifier', onClick: () => editAction(action) },
        { label: 'Copier', onClick: () => { cloneActionForClipboard(action, 'copy'); return Promise.resolve(); } },
        { label: 'Couper', onClick: () => { cloneActionForClipboard(action, 'cut'); return Promise.resolve(); } },
        { label: 'Supprimer', className: 'danger', onClick: () => deleteAction(action) },
      ]);
    }

    function openDayContextMenu(event, targetDate) {
      event.preventDefault();
      event.stopPropagation();
      showContextMenu(event.clientX, event.clientY, [
        {
          label: clipboard ? 'Coller ici' : 'Coller ici (vide)',
          disabled: !clipboard,
          onClick: () => pasteToDate(targetDate),
        },
      ]);
    }

    function renderCalendar(data) {
      els.calendarGrid.innerHTML = '';
      const actions = Array.isArray(data?.actions) ? data.actions : [];
      const buckets = new Map();
      for (let i = 0; i < 7; i++) {
        const day = addDays(selectedWeekStart, i);
        buckets.set(formatDateInput(day), []);
      }
      for (const action of actions) {
        const key = normalizeActionDate(action);
        if (key && buckets.has(key)) buckets.get(key).push(action);
      }

      for (let i = 0; i < 7; i++) {
        const day = addDays(selectedWeekStart, i);
        const key = formatDateInput(day);
        const column = document.createElement('div');
        column.className = 'calendar-day';
        column.innerHTML = `
          <div class="calendar-head">
            <strong>${weekdayNames[i]}</strong>
            <span>${day.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' })}</span>
            <button class="ghost mini-btn" type="button">Coller ici</button>
          </div>
          <div class="calendar-body"></div>
        `;
        const body = column.querySelector('.calendar-body');
        column.querySelector('button').addEventListener('click', () => pasteToDate(key));
        column.addEventListener('contextmenu', event => openDayContextMenu(event, key));
        body.addEventListener('contextmenu', event => openDayContextMenu(event, key));
        body.addEventListener('dragover', event => {
          event.preventDefault();
          body.classList.add('drop-target');
        });
        body.addEventListener('dragleave', () => body.classList.remove('drop-target'));
        body.addEventListener('drop', async event => {
          event.preventDefault();
          body.classList.remove('drop-target');
          const raw = event.dataTransfer.getData('application/json');
          if (!raw) return;
          const dragged = JSON.parse(raw);
          const result = await api('/api/update-action', { id: dragged.id, due: key });
          setNotice(els.agendaNotice, result.ok, result.ok ? 'Action deplacee.' : (result?.error || 'Echec du deplacement.'));
          if (result.ok) await loadCurrentWeek(false);
        });
        const items = buckets.get(key) || [];
        if (!items.length) {
          const empty = document.createElement('div');
          empty.className = 'calendar-empty';
          empty.textContent = 'Aucune action';
          body.appendChild(empty);
        } else {
          for (const action of items) {
            const node = document.createElement('article');
            node.className = `agenda-item${action.status && action.status !== 'done' ? ' ' + action.status : ''}${action.status === 'not_started' ? ' pending' : ''}`;
            node.draggable = true;
            node.addEventListener('contextmenu', event => openActionContextMenu(event, action));
            node.addEventListener('dragstart', event => {
              node.classList.add('dragging');
              event.dataTransfer.setData('application/json', JSON.stringify({ id: action.id }));
            });
            node.addEventListener('dragend', () => node.classList.remove('dragging'));
            const qualif = action.qualification?.code || '';
            node.innerHTML = `
              <div class="agenda-tags">
                ${categoryPill(action)}
                ${statusPill(action)}
              </div>
              <strong>${action.content || '(sans titre)'}</strong>
              ${action.comment ? `<p>${action.comment}</p>` : ''}
              <div class="meta">${action.dateFinReelle || action.dateEcheance || ''}${qualif ? ' | ' + qualif : ''}</div>
            `;
            body.appendChild(node);
          }
        }
        els.calendarGrid.appendChild(column);
      }
      els.listSummary.textContent = `${actions.length} action(s) sur la semaine affichee.`;
      const estimatedHours = actions.length * hoursPerAction;
      const missingHours = Math.max(0, weeklyTargetHours - estimatedHours);
      els.summaryActions.textContent = String(actions.length);
      els.summaryHours.textContent = `${estimatedHours}h`;
      els.summaryMissing.textContent = `${missingHours}h`;
    }

    function renderAnalysis(analysis) {
      els.weeksGrid.innerHTML = '';
      if (!analysis) {
        els.analysisSummary.textContent = '';
        els.summaryHours.textContent = '0';
        els.summaryMissing.textContent = '0';
        return;
      }
      els.analysisSummary.textContent = `Semaines analysees: ${analysis.weeks_total} | semaines sans action: ${analysis.weeks_missing_actions} | heures qualifiees: ${analysis.qualified_hours_total}`;

      for (const week of analysis.weeks || []) {
        const div = document.createElement('div');
        div.className = `week-card${week.count === 0 ? ' missing' : ''}`;
        const titles = Array.isArray(week.titles) && week.titles.length ? week.titles.slice(0, 3).join(' | ') : 'Aucune action';
        div.innerHTML = `
          <strong>${week.week_start} → ${week.week_end}</strong>
          <div class="meta" style="margin-top:6px">actions=${week.count} | heures qualifiees=${week.qualified_hours}</div>
          <div style="margin-top:8px">${titles}</div>
        `;
        els.weeksGrid.appendChild(div);
      }
    }

    async function refreshSession() {
      const session = await api('/api/session');
      updateSession(session);
      return session;
    }

    document.querySelectorAll('.nav-btn').forEach(button => {
      button.addEventListener('click', () => switchSection(button.dataset.section));
    });

    document.addEventListener('contextmenu', event => {
      const interactive = event.target.closest('.agenda-item, .calendar-day, .calendar-body');
      if (!interactive) {
        event.preventDefault();
        hideContextMenu();
      }
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('#contextMenu')) hideContextMenu();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        hideContextMenu();
        closeModal(els.editModal);
        closeModal(els.deleteModal);
      }
    });
    window.addEventListener('scroll', hideContextMenu, true);
    window.addEventListener('resize', hideContextMenu);
    els.editModal.addEventListener('click', event => {
      if (event.target === els.editModal) closeModal(els.editModal);
    });
    els.deleteModal.addEventListener('click', event => {
      if (event.target === els.deleteModal) closeModal(els.deleteModal);
    });

    document.getElementById('loginBtn').addEventListener('click', async () => {
      const payload = {
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
        remember_me: document.getElementById('rememberMe').checked,
      };
      const result = await api('/api/login', payload);
      setLoginResult(result);
      if (result.session) updateSession(result.session);
      if (result.ok) {
        switchSection('agendaSection');
        await loadCurrentWeek(false);
      }
    });

    document.getElementById('refreshBtn').addEventListener('click', async () => {
      const result = await api('/api/refresh', {});
      setNotice(els.loginNotice, result.ok, result.ok ? 'Token rafraichi.' : (result?.error || 'Echec du rafraichissement.'));
      if (result.session) updateSession(result.session);
      applyUnauthorizedState(result);
    });

    document.getElementById('logoutBtn').addEventListener('click', async () => {
      const result = await api('/api/logout', {});
      setNotice(els.loginNotice, result.ok, result.ok ? 'Session locale supprimee.' : (result?.error || 'Echec de deconnexion.'));
      if (result.session) updateSession(result.session);
    });
    document.getElementById('closeEditModalBtn').addEventListener('click', () => closeModal(els.editModal));
    document.getElementById('cancelEditBtn').addEventListener('click', () => closeModal(els.editModal));
    document.getElementById('saveEditBtn').addEventListener('click', async () => {
      if (!editingAction) return;
      const result = await api('/api/update-action', {
        id: editingAction.id,
        title: els.editTitle.value,
        comment: els.editComment.value,
        due: els.editDue.value,
        qualification: els.editQualification.value,
        status: els.editStatus.value,
      });
      setNotice(els.editNotice, result.ok, result.ok ? 'Modification enregistree.' : (result?.error || 'Echec de modification.'));
      if (!result.ok) return;
      closeModal(els.editModal);
      setNotice(els.agendaNotice, true, 'Action modifiee.');
      await loadCurrentWeek(false);
    });
    document.getElementById('closeDeleteModalBtn').addEventListener('click', () => closeModal(els.deleteModal));
    document.getElementById('cancelDeleteBtn').addEventListener('click', () => closeModal(els.deleteModal));
    document.getElementById('confirmDeleteBtn').addEventListener('click', async () => {
      if (!deletingAction) return;
      const result = await api('/api/delete-action', { id: deletingAction.id });
      setNotice(els.deleteNotice, result.ok, result.ok ? 'Suppression effectuee.' : (result?.error || 'Echec de suppression.'));
      if (!result.ok) return;
      closeModal(els.deleteModal);
      setNotice(els.agendaNotice, true, 'Action supprimee.');
      await loadCurrentWeek(false);
    });
    document.getElementById('applyThemeBtn').addEventListener('click', () => {
      applyTheme({
        darkMode: els.darkMode.checked,
        primary: els.primaryColor.value,
        secondary: els.secondaryColor.value,
      });
      setNotice(els.themeNotice, true, 'Apparence mise a jour.');
    });
    document.getElementById('resetThemeBtn').addEventListener('click', () => {
      applyTheme(defaultTheme);
      setNotice(els.themeNotice, true, 'Apparence reinitialisee.');
    });

    async function loadCurrentWeek(withAnalysis) {
      const payload = {
        from_date: document.getElementById('fromDate').value,
        to_date: document.getElementById('toDate').value,
      };
      const path = withAnalysis ? '/api/analyze' : '/api/list';
      const result = await api(path, payload);
      if (!result.ok) {
        els.calendarGrid.innerHTML = '';
        els.listSummary.textContent = '';
        if (withAnalysis) renderAnalysis(null);
        setNotice(els.agendaNotice, false, result?.error || 'Impossible de charger la periode.');
        applyUnauthorizedState(result);
        return;
      }
      setNotice(els.agendaNotice, true, withAnalysis ? 'Analyse mise a jour.' : '');
      renderCalendar(result.data);
      if (withAnalysis) renderAnalysis(result.analysis);
      else renderAnalysis(null);
    }

    document.getElementById('listBtn').addEventListener('click', async () => {
      switchSection('agendaSection');
      await loadCurrentWeek(false);
    });

    document.getElementById('analyzeBtn').addEventListener('click', async () => {
      await loadCurrentWeek(true);
    });
    document.getElementById('quickCreateBtn').addEventListener('click', quickCreateAction);

    document.getElementById('prevWeekBtn').addEventListener('click', async () => {
      selectedWeekStart = addDays(selectedWeekStart, -7);
      syncWeekInputs();
      await loadCurrentWeek(false);
    });

    document.getElementById('nextWeekBtn').addEventListener('click', async () => {
      selectedWeekStart = addDays(selectedWeekStart, 7);
      syncWeekInputs();
      await loadCurrentWeek(false);
    });

    document.getElementById('currentWeekBtn').addEventListener('click', async () => {
      selectedWeekStart = startOfWeek(new Date());
      syncWeekInputs();
      await loadCurrentWeek(false);
    });

    api('/api/theme').then(result => {
      if (result && result.ok && result.theme) {
        applyTheme(result.theme, false);
      } else {
        applyTheme(defaultTheme, false);
      }
    }).catch(() => {
      applyTheme(defaultTheme, false);
    });

    refreshSession().then(session => {
      if (session && session.connected) {
        loadCurrentWeek(false);
      } else {
        els.summaryWeekCount.textContent = '-';
        els.summaryActions.textContent = '0';
        els.summaryHours.textContent = '0h';
        els.summaryMissing.textContent = `${weeklyTargetHours}h`;
      }
    });
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), CejHandler)
    print(f"CEJ web UI listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
