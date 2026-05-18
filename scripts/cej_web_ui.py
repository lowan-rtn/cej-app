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
    status = str(payload.get("status", "not_started")).strip()
    if status:
        cmd.extend(["--status", status])

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
    estimated_hours_per_action = 2
    weekly_target_hours = 15
    total_estimated_hours = 0
    qualified_hours = 0
    categories: dict[str, dict] = {}

    cursor = start - timedelta(days=start.weekday())
    last_week_start = end - timedelta(days=end.weekday())
    while cursor <= last_week_start:
        week_key = cursor.isoformat()
        weeks[week_key] = {
            "week_start": max(cursor, start).isoformat(),
            "week_end": min(cursor + timedelta(days=6), end).isoformat(),
            "count": 0,
            "estimated_hours": 0,
            "qualified_hours": 0,
            "target_hours": round(((min(cursor + timedelta(days=6), end) - max(cursor, start)).days + 1) / 7 * weekly_target_hours),
            "balance_hours": 0,
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
        weeks[week_key]["estimated_hours"] += estimated_hours_per_action
        weeks[week_key]["balance_hours"] = weeks[week_key]["estimated_hours"] - weekly_target_hours
        title = action.get("content")
        if isinstance(title, str) and title:
            weeks[week_key]["titles"].append(title)

        qualification = action.get("qualification")
        qualification_code = "Sans categorie"
        if isinstance(qualification, dict):
            code = qualification.get("code")
            if isinstance(code, str) and code:
                qualification_code = code
            hours = qualification.get("heures")
            if isinstance(hours, int):
                weeks[week_key]["qualified_hours"] += hours
                qualified_hours += hours
        category = categories.setdefault(qualification_code, {"code": qualification_code, "count": 0, "estimated_hours": 0})
        category["count"] += 1
        category["estimated_hours"] += estimated_hours_per_action
        total_estimated_hours += estimated_hours_per_action

    ordered_weeks = list(weeks.values())
    for week in ordered_weeks:
        week["balance_hours"] = week["estimated_hours"] - week["target_hours"]
    missing_weeks = [week for week in ordered_weeks if week["count"] == 0]
    busiest = sorted(ordered_weeks, key=lambda item: (item["count"], item["qualified_hours"]), reverse=True)
    target_total = len(ordered_weeks) * weekly_target_hours
    average_hours = round(total_estimated_hours / len(ordered_weeks), 1) if ordered_weeks else 0
    average_ok = average_hours >= weekly_target_hours
    balance_total = total_estimated_hours - target_total
    period_days = (end - start).days + 1
    period_target = round((period_days / 7) * weekly_target_hours)
    period_balance = total_estimated_hours - period_target

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "actions_count": len(actions),
        "estimated_hours_total": total_estimated_hours,
        "weekly_target_hours": weekly_target_hours,
        "target_hours_total": target_total,
        "period_days": period_days,
        "period_target_hours": period_target,
        "period_balance_hours": period_balance,
        "average_hours_per_week": average_hours,
        "average_ok": average_ok,
        "balance_hours_total": balance_total,
        "categories": sorted(categories.values(), key=lambda item: item["estimated_hours"], reverse=True),
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
    .recap-button {
      width: 100%;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      background:
        linear-gradient(135deg, rgba(110, 86, 207, 0.09), transparent 68%),
        var(--panel-soft);
      color: var(--ink);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }
    .recap-button:hover {
      transform: translateY(-1px);
      border-color: var(--violet-line);
    }
    .recap-cell {
      display: grid;
      gap: 4px;
      min-width: 0;
    }
    .recap-cell span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .recap-cell strong {
      font-size: 22px;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .recap-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin: 12px 0;
    }
    .recap-card {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-soft);
      padding: 12px;
      display: grid;
      gap: 4px;
    }
    .recap-card span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .recap-card strong {
      font-size: 20px;
    }
    .recap-categories {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .category-stat-row {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) 110px 130px;
      gap: 10px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-soft);
      padding: 10px 12px;
    }
    .category-stat-row strong,
    .category-stat-row span {
      overflow-wrap: anywhere;
    }
    .recap-message {
      border: 1px solid var(--violet-line);
      border-radius: 10px;
      background: var(--violet-soft);
      padding: 12px;
      font-weight: 800;
      line-height: 1.35;
    }
    .calendar {
      display: grid;
      grid-template-columns: repeat(7, minmax(180px, 1fr));
      gap: 12px;
      overflow-x: auto;
      padding-bottom: 4px;
    }
    .view-tabs, .filter-tabs, .date-choice-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .view-tabs {
      margin: 4px 0 14px;
    }
    .tab-btn, .date-choice {
      border: 1px solid var(--line);
      background: var(--panel-soft-2);
      color: var(--ink);
      border-radius: 10px;
      padding: 8px 11px;
      font-size: 13px;
      font-weight: 800;
    }
    .tab-btn.active, .date-choice.active {
      background: var(--accent);
      color: var(--accent-contrast);
      border-color: transparent;
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
    .agenda-item.syncing {
      opacity: 0.78;
      border-style: dashed;
    }
    .agenda-item.sync-error {
      border-left-color: var(--danger);
      border-color: #e5aaaa;
      background: var(--danger-soft);
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
    .sync-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .sync-row.error {
      color: var(--danger);
    }
    .sync-row .mini-btn {
      padding: 4px 7px;
    }
    .list-panel {
      display: none;
      margin-top: 14px;
    }
    .list-panel.active {
      display: grid;
      gap: 10px;
    }
    .calendar.hidden-view {
      display: none;
    }
    .action-list {
      display: grid;
      gap: 8px;
    }
    .action-row {
      display: grid;
      grid-template-columns: 120px minmax(220px, 1fr) 170px 120px;
      gap: 12px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      padding: 11px 12px;
    }
    .action-row.sync-error {
      border-color: #e5aaaa;
      background: var(--danger-soft);
    }
    .action-row.syncing {
      opacity: 0.78;
      border-style: dashed;
    }
    .create-days {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
      gap: 8px;
      margin-top: 8px;
    }
    .day-check {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-soft);
      font-size: 13px;
      font-weight: 700;
    }
    .day-check input {
      width: auto;
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
    .week-card {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      color: var(--ink);
      padding: 14px;
    }
    .week-card.missing {
      background: color-mix(in srgb, var(--danger-soft) 58%, var(--panel) 42%);
      border-color: color-mix(in srgb, var(--danger) 38%, var(--line) 62%);
      color: var(--ink);
    }
    .week-card.missing .meta {
      color: color-mix(in srgb, var(--danger) 55%, var(--muted) 45%);
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
    .yoki-widget {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 11000;
      display: grid;
      justify-items: end;
      gap: 12px;
    }
    body.yoki-disabled .yoki-widget {
      display: none;
    }
    .yoki-bubble {
      width: 62px;
      height: 62px;
      border-radius: 999px;
      border: 1px solid var(--violet-line);
      background: linear-gradient(135deg, var(--accent), var(--violet));
      color: var(--accent-contrast);
      box-shadow: 0 18px 42px rgba(20, 41, 61, 0.22);
      display: grid;
      place-items: center;
      font-size: 22px;
      font-weight: 900;
    }
    .yoki-bubble.has-alert::after {
      content: "";
      position: absolute;
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: #f59e0b;
      border: 2px solid var(--panel);
      right: 4px;
      top: 4px;
    }
    .agent-panel {
      width: min(560px, calc(100vw - 28px));
      max-height: min(720px, calc(100vh - 112px));
      border: 1px solid var(--violet-line);
      border-radius: 18px;
      padding: 14px;
      background:
        linear-gradient(135deg, rgba(110, 86, 207, 0.12), transparent 62%),
        var(--panel-soft);
      box-shadow: 0 24px 56px rgba(12, 18, 28, 0.22);
      display: none;
      grid-template-rows: auto minmax(0, 1fr) auto auto auto;
      overflow: hidden;
    }
    .agent-raw {
      display: none;
      max-height: 180px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid var(--violet-line);
      border-radius: 12px;
      background: color-mix(in srgb, var(--panel) 88%, #000 12%);
      color: var(--ink);
      padding: 10px;
      font-family: "JetBrains Mono", "Fira Code", monospace;
      font-size: 11px;
      line-height: 1.45;
      margin: 0 0 10px;
    }
    .agent-raw.open {
      display: block;
    }
    .dev-only {
      display: none !important;
    }
    body.dev-mode .dev-only {
      display: inline-grid !important;
    }
    .agent-panel.open {
      display: grid;
    }
    .agent-head {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .agent-head-actions {
      display: flex;
      gap: 8px;
      flex-shrink: 0;
    }
    .agent-head-actions .dev-only {
      width: auto;
      min-width: 72px;
      padding: 0 12px;
    }
    .agent-panel.inline-legacy {
      position: static;
      width: auto;
      max-height: none;
      display: grid;
      border: 1px solid var(--violet-line);
      border-radius: 14px;
      padding: 14px;
      background:
        linear-gradient(135deg, rgba(110, 86, 207, 0.12), transparent 62%),
        var(--panel-soft);
    }
    .agent-panel h3 {
      margin: 0;
      font-size: 16px;
      letter-spacing: -0.01em;
    }
    .agent-panel p {
      margin: 4px 0 12px;
      font-size: 13px;
      color: var(--muted);
    }
    .agent-input-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
    }
    .agent-input-row textarea {
      min-height: 72px;
    }
    .agent-chat {
      display: grid;
      gap: 8px;
      max-height: 260px;
      overflow: auto;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      margin-bottom: 10px;
    }
    .agent-message {
      width: auto;
      max-width: 100%;
      padding: 9px 11px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--ink);
      font-size: 13px;
      overflow-wrap: anywhere;
      line-height: 1.45;
    }
    @media (max-width: 720px) {
      .yoki-widget {
        inset: auto 14px 14px auto;
      }
      .agent-panel.open {
        position: fixed;
        inset: 0;
        width: auto;
        max-height: none;
        border-radius: 0;
      }
      .yoki-bubble {
        width: 58px;
        height: 58px;
      }
    }
    .agent-message.user {
      justify-self: end;
      background: var(--accent);
      color: var(--accent-contrast);
      border-color: transparent;
    }
    .agent-message.yoki {
      justify-self: start;
      border-color: var(--violet-line);
      background: linear-gradient(135deg, var(--violet-soft), var(--panel-soft));
    }
    .agent-proposal {
      display: none;
      gap: 10px;
      margin: 10px 0;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--violet-line);
      background: var(--panel);
    }
    .agent-proposal.open {
      display: grid;
    }
    .agent-proposal-title {
      font-weight: 800;
      color: var(--ink);
    }
    .agent-proposal-command {
      padding: 10px;
      border-radius: 10px;
      background: var(--panel-soft-2);
      color: var(--muted);
      font-size: 12px;
      overflow: auto;
      white-space: pre-wrap;
    }
    .agent-log {
      margin-top: 10px;
      min-height: 22px;
      color: var(--muted);
      font-size: 13px;
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
          <button id="monthlyRecapBtn" class="recap-button" type="button" aria-label="Ouvrir le recapitulatif CEJ">
            <div class="recap-cell"><span>Semaine</span><strong id="summaryWeekCount" class="small">-</strong></div>
            <div class="recap-cell"><span>Actions</span><strong id="summaryActions">0</strong></div>
            <div class="recap-cell"><span>Heures estimees</span><strong id="summaryHours">0h</strong></div>
            <div class="recap-cell"><span>Manque CEJ</span><strong id="summaryMissing">0h</strong></div>
          </button>
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
          <div id="agendaNotice" class="notice" style="margin-top:14px"></div>
          <div id="listSummary" class="meta" style="margin-top:8px"></div>
          <div class="view-tabs" role="tablist" aria-label="Vue actions">
            <button class="tab-btn active" id="agendaViewBtn" type="button">Agenda</button>
            <button class="tab-btn" id="listViewBtn" type="button">Liste</button>
          </div>
          <div id="calendarGrid" class="calendar" style="margin-top:14px"></div>
          <div id="actionListPanel" class="list-panel" aria-hidden="true">
            <div class="filter-tabs">
              <button class="tab-btn active" data-list-filter="all" type="button">Tout</button>
              <button class="tab-btn" data-list-filter="errors" type="button">Erreurs</button>
              <button class="tab-btn" data-list-filter="future" type="button">A venir</button>
              <button class="tab-btn" data-list-filter="multi" type="button">Multi-jours</button>
            </div>
            <div id="actionList" class="action-list"></div>
          </div>
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
          <div class="card panel" data-requires-session="true">
            <h2>Yoki</h2>
            <div class="theme-grid">
              <div class="theme-card">
                <div>
                  <label for="yokiAutonomyMode">Mode autonomie</label>
                  <select id="yokiAutonomyMode">
                    <option value="strict">Strict - tout valider</option>
                    <option value="prudent">Prudent - creation simple autonome</option>
                    <option value="normal">Normal - creer, modifier, deplacer si clair</option>
                  </select>
                </div>
                <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
                  <input id="yokiEnabled" type="checkbox" style="width:auto;">
                  Activer Yoki
                </label>
                <div>
                  <label for="yokiConfidence">Seuil de confiance</label>
                  <input id="yokiConfidence" type="number" min="0.5" max="0.95" step="0.05" value="0.75">
                </div>
                <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
                  <input id="yokiCostGuard" type="checkbox" style="width:auto;">
                  Bloquer Yoki avant cout payant
                </label>
                <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
                  <input id="yokiDevMode" type="checkbox" style="width:auto;">
                  Mode dev Yoki
                </label>
                <div>
                  <label for="yokiNeuronLimit">Quota neurones/jour</label>
                  <input id="yokiNeuronLimit" type="number" min="100" step="100" value="10000">
                </div>
                <div class="actions">
                  <button class="secondary" id="saveYokiSettingsBtn" type="button">Enregistrer Yoki</button>
                  <button class="ghost" id="clearYokiHistoryBtn" type="button">Effacer conversation</button>
                </div>
              </div>
              <div class="theme-card">
                <div class="meta">Etat IA</div>
                <div id="yokiUsageMeta" class="meta">Chargement...</div>
                <div id="yokiLogMeta" class="meta"></div>
                <div class="actions">
                  <button class="ghost" id="undoYokiBtn" type="button">Annuler derniere action Yoki</button>
                </div>
                <div id="yokiSettingsNotice" class="notice"></div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <div class="yoki-widget" data-requires-session="true">
    <div id="agentPanel" class="agent-panel" aria-hidden="true">
      <div class="agent-head">
        <div>
          <h3>Yoki</h3>
          <p>Assistant CEJ. Il peut discuter, agir dans l’agenda et noter ce qu’il fait.</p>
        </div>
        <div class="agent-head-actions">
          <button id="agentRawBtn" class="icon-btn dev-only" type="button" aria-label="Details Yoki">Detail</button>
          <button id="agentMinimizeBtn" class="icon-btn" type="button" aria-label="Reduire">−</button>
          <button id="agentCloseBtn" class="icon-btn" type="button" aria-label="Fermer">×</button>
        </div>
      </div>
      <pre id="agentRawOutput" class="agent-raw" aria-hidden="true"></pre>
      <div id="agentChat" class="agent-chat" aria-live="polite"></div>
      <div id="agentProposal" class="agent-proposal" aria-hidden="true">
        <div class="agent-proposal-title" id="agentProposalTitle">Proposition de Yoki</div>
        <div id="agentProposalText" class="meta"></div>
        <div id="agentProposalCommand" class="agent-proposal-command"></div>
        <div class="actions" style="margin-top:0">
          <button class="primary" id="agentConfirmBtn" type="button">Executer</button>
          <button class="ghost" id="agentCancelBtn" type="button">Annuler</button>
        </div>
      </div>
      <div class="agent-input-row">
        <textarea id="agentCommand" placeholder="Parle a Yoki..."></textarea>
        <button class="secondary" id="agentRunBtn" type="button">Envoyer</button>
      </div>
      <div id="agentNotice" class="agent-log"></div>
    </div>
    <button id="yokiBubbleBtn" class="yoki-bubble" type="button" aria-label="Ouvrir Yoki">Y</button>
  </div>
  <div id="contextMenu" class="context-menu" aria-hidden="true"></div>
  <div id="recapModal" class="modal-overlay" aria-hidden="true">
    <div class="modal-card" style="width:min(780px, calc(100vw - 32px));">
      <div class="modal-head">
        <div>
          <h3>Recapitulatif CEJ</h3>
          <p id="recapPeriod">Mois courant</p>
        </div>
        <button id="closeRecapModalBtn" class="icon-btn" type="button" aria-label="Fermer">×</button>
      </div>
      <div id="recapMessage" class="recap-message">Calcul en cours...</div>
      <div class="recap-grid">
        <div class="recap-card"><span>Ce mois-ci</span><strong id="recapMonthAverage">-</strong><div id="recapMonthDetail" class="meta"></div></div>
        <div class="recap-card"><span>Mois precedents</span><strong id="recapPastMonthsAverage">-</strong><div id="recapPastMonthsDetail" class="meta"></div></div>
        <div class="recap-card"><span>Autres semaines</span><strong id="recapOtherWeeksAverage">-</strong><div id="recapOtherWeeksDetail" class="meta"></div></div>
        <div class="recap-card"><span>Reste a faire</span><strong id="recapRemaining">-</strong><div id="recapRemainingDetail" class="meta"></div></div>
      </div>
      <div class="recap-card">
        <span>Categories</span>
        <div id="recapCategories" class="recap-categories"></div>
      </div>
      <div id="recapWeeksGrid" class="week-grid"></div>
      <div id="recapNotice" class="notice"></div>
    </div>
  </div>
  <div id="createModal" class="modal-overlay" aria-hidden="true">
    <div class="modal-card">
      <div class="modal-head">
        <div>
          <h3>Nouvelle action</h3>
          <p>Creation rapide depuis l'agenda.</p>
        </div>
        <button id="closeCreateModalBtn" class="icon-btn" type="button" aria-label="Fermer">×</button>
      </div>
      <div>
        <label for="createTitle">Titre</label>
        <input id="createTitle" type="text">
      </div>
      <div>
        <label for="createComment">Description</label>
        <textarea id="createComment"></textarea>
      </div>
      <div class="row">
        <div>
          <label for="createQualification">Categorie</label>
          <select id="createQualification">
            <option>EMPLOI</option>
            <option>PROJET_PROFESSIONNEL</option>
            <option>CULTURE_SPORT_LOISIRS</option>
            <option>CITOYENNETE</option>
            <option>FORMATION</option>
            <option>LOGEMENT</option>
            <option>SANTE</option>
          </select>
        </div>
        <div>
          <label for="createStatus">Statut</label>
          <select id="createStatus">
            <option value="done">done</option>
            <option value="not_started">not_started</option>
            <option value="in_progress">in_progress</option>
            <option value="canceled">canceled</option>
          </select>
        </div>
      </div>
      <div>
        <label>Jour</label>
        <div id="createDateChoices" class="date-choice-grid"></div>
      </div>
      <div style="margin-top:10px">
        <label style="display:flex;align-items:center;gap:10px;margin:0;text-transform:none;letter-spacing:0;color:var(--ink);font-size:14px;font-weight:600;">
          <input id="createMultiDay" type="checkbox" style="width:auto;">
          Creer sur plusieurs jours
        </label>
        <div id="createMultiDays" class="create-days"></div>
      </div>
      <div style="margin-top:10px">
        <label for="createOtherDate">Autre date</label>
        <input id="createOtherDate" type="date">
      </div>
      <div id="createNotice" class="notice"></div>
      <div class="actions">
        <button id="saveCreateBtn" class="primary" type="button">Creer</button>
        <button id="cancelCreateBtn" class="ghost" type="button">Annuler</button>
      </div>
    </div>
  </div>
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
      agentPanel: document.getElementById('agentPanel'),
      yokiBubbleBtn: document.getElementById('yokiBubbleBtn'),
      agentNotice: document.getElementById('agentNotice'),
      agentChat: document.getElementById('agentChat'),
      agentRawBtn: document.getElementById('agentRawBtn'),
      agentRawOutput: document.getElementById('agentRawOutput'),
      agentProposal: document.getElementById('agentProposal'),
      agentProposalText: document.getElementById('agentProposalText'),
      agentProposalCommand: document.getElementById('agentProposalCommand'),
      yokiEnabled: document.getElementById('yokiEnabled'),
      yokiAutonomyMode: document.getElementById('yokiAutonomyMode'),
      yokiConfidence: document.getElementById('yokiConfidence'),
      yokiCostGuard: document.getElementById('yokiCostGuard'),
      yokiDevMode: document.getElementById('yokiDevMode'),
      yokiNeuronLimit: document.getElementById('yokiNeuronLimit'),
      yokiUsageMeta: document.getElementById('yokiUsageMeta'),
      yokiLogMeta: document.getElementById('yokiLogMeta'),
      yokiSettingsNotice: document.getElementById('yokiSettingsNotice'),
      themeNotice: document.getElementById('themeNotice'),
      listSummary: document.getElementById('listSummary'),
      analysisSummary: document.getElementById('analysisSummary'),
      weeksGrid: document.getElementById('weeksGrid'),
      weekLabel: document.getElementById('weekLabel'),
      calendarGrid: document.getElementById('calendarGrid'),
      monthlyRecapBtn: document.getElementById('monthlyRecapBtn'),
      recapModal: document.getElementById('recapModal'),
      recapPeriod: document.getElementById('recapPeriod'),
      recapMessage: document.getElementById('recapMessage'),
      recapMonthAverage: document.getElementById('recapMonthAverage'),
      recapMonthDetail: document.getElementById('recapMonthDetail'),
      recapPastMonthsAverage: document.getElementById('recapPastMonthsAverage'),
      recapPastMonthsDetail: document.getElementById('recapPastMonthsDetail'),
      recapOtherWeeksAverage: document.getElementById('recapOtherWeeksAverage'),
      recapOtherWeeksDetail: document.getElementById('recapOtherWeeksDetail'),
      recapRemaining: document.getElementById('recapRemaining'),
      recapRemainingDetail: document.getElementById('recapRemainingDetail'),
      recapCategories: document.getElementById('recapCategories'),
      recapWeeksGrid: document.getElementById('recapWeeksGrid'),
      recapNotice: document.getElementById('recapNotice'),
      agendaViewBtn: document.getElementById('agendaViewBtn'),
      listViewBtn: document.getElementById('listViewBtn'),
      actionListPanel: document.getElementById('actionListPanel'),
      actionList: document.getElementById('actionList'),
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
      createModal: document.getElementById('createModal'),
      createTitle: document.getElementById('createTitle'),
      createComment: document.getElementById('createComment'),
      createQualification: document.getElementById('createQualification'),
      createStatus: document.getElementById('createStatus'),
      createDateChoices: document.getElementById('createDateChoices'),
      createMultiDay: document.getElementById('createMultiDay'),
      createMultiDays: document.getElementById('createMultiDays'),
      createOtherDate: document.getElementById('createOtherDate'),
      createNotice: document.getElementById('createNotice'),
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
    const aiBaseEndpoint = 'https://cej-app-ai.lowan-rabille64.workers.dev';
    const aiChatEndpoint = `${aiBaseEndpoint}/chat`;
    const aiUsageEndpoint = `${aiBaseEndpoint}/usage`;
    const yokiHistoryKey = 'cej.yoki.history.v1';
    const yokiSettingsKey = 'cej.yoki.settings.v1';
    const yokiLogKey = 'cej.yoki.actionLog.v1';
    const yokiUsageKey = 'cej.yoki.usage.v1';
    const defaultTheme = {
      darkMode: false,
      primary: '#174a7c',
      secondary: '#6e56cf',
    };
    let clipboard = null;
    let contextMenuState = null;
    let activeView = 'agenda';
    let activeListFilter = 'all';
    let creatingDate = null;
    let editingAction = null;
    let deletingAction = null;
    let pendingAgentSuggestion = null;
    let pendingAgentDraft = null;
    let conversationHistory = [];
    let yokiActionLog = [];
    let yokiSettings = {
      enabled: false,
      autonomyMode: 'normal',
      confidenceThreshold: 0.75,
      costGuard: true,
      neuronDailyLimit: 10000,
      devMode: false,
    };
    let yokiUsage = {
      day: new Date().toISOString().slice(0, 10),
      requests: 0,
      neurons: 0,
    };
    let lastYokiRaw = '';
    let currentActions = [];
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

    function setAgentNotice(ok, message) {
      setNotice(els.agentNotice, ok, message);
    }

    function loadJsonStorage(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
      } catch {
        return fallback;
      }
    }

    function saveJsonStorage(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch {
        return;
      }
    }

    function loadYokiState() {
      conversationHistory = loadJsonStorage(yokiHistoryKey, []);
      yokiActionLog = loadJsonStorage(yokiLogKey, []);
      yokiSettings = { ...yokiSettings, ...loadJsonStorage(yokiSettingsKey, {}) };
      yokiUsage = { ...yokiUsage, ...loadJsonStorage(yokiUsageKey, {}) };
      const today = new Date().toISOString().slice(0, 10);
      if (yokiUsage.day !== today) {
        yokiUsage = { day: today, requests: 0, neurons: 0 };
      }
      syncYokiSettingsControls();
      renderYokiHistory();
      renderYokiUsage();
    }

    function syncYokiSettingsControls() {
      if (!els.yokiAutonomyMode) return;
      els.yokiEnabled.checked = !!yokiSettings.enabled;
      els.yokiAutonomyMode.value = yokiSettings.autonomyMode;
      els.yokiConfidence.value = String(yokiSettings.confidenceThreshold);
      els.yokiCostGuard.checked = !!yokiSettings.costGuard;
      els.yokiDevMode.checked = !!yokiSettings.devMode;
      els.yokiNeuronLimit.value = String(yokiSettings.neuronDailyLimit);
      document.body.classList.toggle('yoki-disabled', !yokiSettings.enabled);
      document.body.classList.toggle('dev-mode', !!yokiSettings.devMode);
    }

    function persistYokiSettingsFromControls() {
      yokiSettings = {
        enabled: els.yokiEnabled.checked,
        autonomyMode: els.yokiAutonomyMode.value,
        confidenceThreshold: Number(els.yokiConfidence.value || 0.75),
        costGuard: els.yokiCostGuard.checked,
        devMode: els.yokiDevMode.checked,
        neuronDailyLimit: Number(els.yokiNeuronLimit.value || 10000),
      };
      saveJsonStorage(yokiSettingsKey, yokiSettings);
      document.body.classList.toggle('yoki-disabled', !yokiSettings.enabled);
      document.body.classList.toggle('dev-mode', !!yokiSettings.devMode);
      renderYokiUsage();
    }

    function setYokiEnabled(enabled) {
      yokiSettings.enabled = Boolean(enabled);
      if (els.yokiEnabled) els.yokiEnabled.checked = yokiSettings.enabled;
      document.body.classList.toggle('yoki-disabled', !yokiSettings.enabled);
      if (!yokiSettings.enabled) {
        els.agentPanel?.classList.remove('open');
        els.agentPanel?.setAttribute('aria-hidden', 'true');
      }
      saveJsonStorage(yokiSettingsKey, yokiSettings);
      renderYokiUsage();
    }

    function setYokiDevMode(enabled) {
      yokiSettings.devMode = Boolean(enabled);
      if (els.yokiDevMode) els.yokiDevMode.checked = yokiSettings.devMode;
      document.body.classList.toggle('dev-mode', yokiSettings.devMode);
      saveJsonStorage(yokiSettingsKey, yokiSettings);
      renderYokiUsage();
    }

    function addConversation(role, content) {
      conversationHistory.push({ role, content: String(content || ''), at: new Date().toISOString() });
      conversationHistory = conversationHistory.slice(-30);
      saveJsonStorage(yokiHistoryKey, conversationHistory);
    }

    function renderYokiHistory() {
      if (!els.agentChat) return;
      els.agentChat.innerHTML = '';
      for (const item of conversationHistory) {
        const bubble = document.createElement('div');
        bubble.className = `agent-message ${item.role === 'user' ? 'user' : 'yoki'}`;
        bubble.textContent = item.content;
        els.agentChat.appendChild(bubble);
      }
      els.agentChat.scrollTop = els.agentChat.scrollHeight;
    }

    function renderYokiUsage(workerUsage) {
      if (!els.yokiUsageMeta) return;
      const blocked = yokiSettings.costGuard && yokiUsage.neurons >= yokiSettings.neuronDailyLimit;
      els.yokiUsageMeta.textContent = `${yokiSettings.enabled ? 'Yoki actif' : 'Yoki desactive'} | Mode ${yokiSettings.autonomyMode} | confiance ${yokiSettings.confidenceThreshold} | ${yokiUsage.neurons}/${yokiSettings.neuronDailyLimit} neurones/jour${blocked ? ' | bloque' : ''}${workerUsage ? ' | Worker OK' : ''}${yokiSettings.devMode ? ' | dev' : ''}`;
      els.yokiLogMeta.textContent = yokiActionLog.length ? `${yokiActionLog.length} action(s) journalisee(s). Derniere: ${yokiActionLog[yokiActionLog.length - 1].summary}` : 'Aucune action Yoki journalisee.';
      els.yokiBubbleBtn?.classList.toggle('has-alert', Boolean(pendingAgentSuggestion));
    }

    function setYokiRaw(raw) {
      lastYokiRaw = raw ? String(raw) : '';
      if (!els.agentRawOutput) return;
      els.agentRawOutput.textContent = lastYokiRaw || 'Aucune sortie brute disponible.';
    }

    async function refreshYokiUsage() {
      try {
        const response = await fetchWithTimeout(aiUsageEndpoint, {}, 5000);
        const result = await response.json();
        if (result?.ok) renderYokiUsage(result.usage);
      } catch {
        renderYokiUsage();
      }
    }

    function estimateNeurons(message) {
      return Math.max(1, Math.ceil(String(message || '').length / 20));
    }

    function addYokiMessage(role, message) {
      if (!els.agentChat) return;
      addConversation(role === 'user' ? 'user' : 'assistant', message);
      renderYokiHistory();
    }

    function clearAgentProposal() {
      pendingAgentSuggestion = null;
      els.agentProposal.classList.remove('open');
      els.agentProposal.setAttribute('aria-hidden', 'true');
      els.agentProposalText.textContent = '';
      els.agentProposalCommand.textContent = '';
    }

    function showAgentProposal(suggestion) {
      pendingAgentSuggestion = suggestion;
      els.agentProposal.classList.add('open');
      els.agentProposal.setAttribute('aria-hidden', 'false');
      els.agentProposalText.textContent = suggestion.explanation || 'Yoki propose une action.';
      const toolCall = suggestion.tool_call || suggestion.command || {};
      els.agentProposalCommand.textContent = humanToolSummary(toolCall);
      renderYokiUsage();
    }

    function humanToolSummary(toolCall) {
      const name = toolCall?.name || toolCall?.type || 'action';
      const args = toolCall?.arguments || toolCall || {};
      if (name === 'create_action' || name === 'propose_action') return `Creer: ${args.title || 'Action CEJ'} | ${args.due || 'date a definir'} | ${args.qualification || 'categorie a definir'}`;
      if (name === 'move_action') return `Deplacer: ${args.query || args.id || 'action'} vers ${args.due || args.date || 'date a definir'}`;
      if (name === 'update_action') return `Modifier: ${args.query || args.id || 'action'}`;
      if (name === 'delete_action') return `Supprimer: ${args.query || args.id || 'action'}`;
      if (name === 'change_week') return 'Changer la semaine affichee';
      if (name === 'analyze_week') return 'Analyser la semaine affichee';
      return 'Action proposee par Yoki';
    }

    function showCreateProposal(title, due, comment = '', qualification = 'EMPLOI') {
      const suggestion = {
        ok: true,
        needs_confirmation: true,
        explanation: `Je te propose de creer l'action "${title}" pour le ${due}.`,
        tool_call: {
          name: 'create_action',
          arguments: { title, comment, due, qualification, status: 'done' },
        },
      };
      addYokiMessage('yoki', suggestion.explanation);
      showAgentProposal(suggestion);
      return { ok: true, message: 'Yoki attend ta validation.' };
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

    function startOfMonth(date) {
      return new Date(date.getFullYear(), date.getMonth(), 1);
    }

    function endOfMonth(date) {
      return new Date(date.getFullYear(), date.getMonth() + 1, 0);
    }

    function addMonths(date, months) {
      return new Date(date.getFullYear(), date.getMonth() + months, 1);
    }

    function formatDateInput(date) {
      return date.toISOString().slice(0, 10);
    }

    function formatDateLabel(date) {
      return date.toLocaleDateString('fr-FR', { day: '2-digit', month: 'long', year: 'numeric' });
    }

    function formatMonthLabel(date) {
      return date.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
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
      currentActions = [];
      els.calendarGrid.innerHTML = '';
      els.actionList.innerHTML = '';
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

    function normalizeSearchText(value) {
      return String(value || '')
        .normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '')
        .toLowerCase()
        .trim();
    }

    function escapeHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function makeClientId() {
      return `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function actionDateValue(action) {
      return normalizeActionDate(action) || action.localDue || '';
    }

    function makeLocalAction(payload, due, extra = {}) {
      return {
        id: extra.id || makeClientId(),
        clientId: extra.clientId || makeClientId(),
        content: payload.title || '',
        comment: payload.comment || '',
        status: payload.status || 'done',
        dateEcheance: `${due}T12:00:00.000Z`,
        localDue: due,
        qualification: { code: payload.qualification || 'EMPLOI' },
        syncState: extra.syncState || 'syncing',
        syncError: extra.syncError || '',
        syncAction: extra.syncAction || 'create',
        syncPayload: { ...payload, due },
        previousDue: extra.previousDue || '',
        multiGroupId: extra.multiGroupId || '',
        multiCount: extra.multiCount || 0,
      };
    }

    function replaceAction(localId, replacement) {
      const index = currentActions.findIndex(action => action.id === localId || action.clientId === localId);
      if (index >= 0) currentActions[index] = replacement;
    }

    function findActionByAnyId(id) {
      return currentActions.find(action => action.id === id || action.clientId === id);
    }

    function updateLiveViews() {
      renderCalendarFromActions();
      renderActionList();
      updateSummaryFromActions();
    }

    function updateSummaryFromActions() {
      const visibleActions = currentActions.filter(action => action.syncState !== 'deleted');
      const estimatedHours = visibleActions.length * hoursPerAction;
      const missingHours = Math.max(0, weeklyTargetHours - estimatedHours);
      els.listSummary.textContent = `${visibleActions.length} action(s) sur la semaine affichee.`;
      els.summaryActions.textContent = String(visibleActions.length);
      els.summaryHours.textContent = `${estimatedHours}h`;
      els.summaryMissing.textContent = `${missingHours}h`;
    }

    function findActionCandidates(query) {
      const needle = normalizeSearchText(query);
      if (!needle) return [];
      return currentActions.filter(action => {
        const haystack = normalizeSearchText(`${action.content || ''} ${action.comment || ''} ${action.id || ''}`);
        return haystack.includes(needle);
      });
    }

    function requireSingleAction(query) {
      const matches = findActionCandidates(query);
      if (matches.length === 1) return { ok: true, action: matches[0] };
      if (matches.length === 0) return { ok: false, error: `Aucune action ne correspond a "${query}".` };
      return { ok: false, error: `${matches.length} actions correspondent a "${query}". Precise le titre ou l'identifiant.` };
    }

    function firstDateInText(text) {
      const value = String(text || '');
      const iso = value.match(/\\b(20\\d{2}-\\d{2}-\\d{2})\\b/);
      if (iso) return iso[1];
      const french = value.match(/\\b(\\d{1,2})[\\/.-](\\d{1,2})[\\/.-](\\d{2,4})\\b/);
      if (!french) return null;
      const day = french[1].padStart(2, '0');
      const month = french[2].padStart(2, '0');
      const year = french[3].length === 2 ? `20${french[3]}` : french[3];
      return `${year}-${month}-${day}`;
    }

    function relativeDateInText(text) {
      const normalized = normalizeSearchText(text);
      const weekdays = [
        ['lundi', 0],
        ['mardi', 1],
        ['mercredi', 2],
        ['jeudi', 3],
        ['vendredi', 4],
        ['samedi', 5],
        ['dimanche', 6],
      ];
      for (const [label, offset] of weekdays) {
        if (normalized.includes(label)) return formatDateInput(addDays(selectedWeekStart, offset));
      }
      if (normalized.includes('aujourd hui') || normalized.includes("aujourd'hui")) return formatDateInput(new Date());
      if (normalized.includes('demain')) return formatDateInput(addDays(new Date(), 1));
      if (normalized.includes('hier')) return formatDateInput(addDays(new Date(), -1));
      return null;
    }

    function dateFromText(text) {
      return firstDateInText(text) || dayMonthDateInText(text) || relativeDateInText(text);
    }

    function dayMonthDateInText(text) {
      const value = String(text || '');
      const match = value.match(/\\b(?:le\\s+)?(\\d{1,2})(?:\\s+|$)/i);
      if (!match) return null;
      const day = Number(match[1]);
      if (!Number.isInteger(day) || day < 1 || day > 31) return null;
      const base = new Date(selectedWeekStart);
      const candidate = new Date(base.getFullYear(), base.getMonth(), day);
      if (Math.abs(candidate.getTime() - base.getTime()) > 21 * 24 * 3600 * 1000) {
        candidate.setMonth(candidate.getMonth() + (candidate < base ? 1 : -1));
      }
      return formatDateInput(candidate);
    }

    function cleanCreateTitle(text) {
      const cleaned = String(text || '')
        .replace(/\\b(20\\d{2}-\\d{2}-\\d{2})\\b/g, '')
        .replace(/\\b\\d{1,2}[\\/.-]\\d{1,2}[\\/.-]\\d{2,4}\\b/g, '')
        .replace(/\\b(?:le\\s+)?\\d{1,2}\\b/g, ' ')
        .replace(/creer|cree|crée|creation|action|pour|stp|svp|dimanche|lundi|mardi|mercredi|jeudi|vendredi|samedi|aujourd'hui|aujourd hui|demain|hier|\\bon\\b|\\bnon\\b|\\bl'\\b|\\ble\\b|\\bla\\b|\\bles\\b|\\bun\\b|\\bune\\b|\\bde\\b|\\bdu\\b|\\bdes\\b|\\bd'\\b|\\bque\\b|\\bj'ai\\b|\\bjai\\b|\\bfaite?\\b/gi, ' ')
        .replace(/\\s+/g, ' ')
        .trim();
      return cleaned;
    }

    function isCreateIntent(text) {
      const normalized = normalizeSearchText(text);
      return normalized.includes('creer') || normalized.includes('cree') || normalized.includes('creation') || normalized.includes('ajoute') || normalized.includes('nouvelle action');
    }

    function isYes(text) {
      const normalized = normalizeSearchText(text);
      return /\\b(oui|ok|vas y|d accord|cree|creer|ajoute|fait|go)\\b/.test(normalized);
    }

    function wantsGeneration(text) {
      const normalized = normalizeSearchText(text);
      return normalized.includes('genere') || normalized.includes('genere un titre') || normalized.includes('description adapte') || normalized.includes('titre et une description');
    }

    function inferQualification(text) {
      const normalized = normalizeSearchText(text);
      if (/\\b(cinema|film|musee|theatre|sport|lecture|bibliotheque|culture)\\b/.test(normalized)) return 'CULTURE_SPORT_LOISIRS';
      if (/\\b(cv|lettre|emploi|travail|candidature|entretien|recrutement|job)\\b/.test(normalized)) return 'EMPLOI';
      if (/\\b(formation|cours|code|auto ecole|permis|atelier)\\b/.test(normalized)) return 'FORMATION';
      if (/\\b(citoyen|administratif|mairie|impot|caf|aide|demarche)\\b/.test(normalized)) return 'CITOYENNETE';
      if (/\\b(logement|appartement|bail|loyer)\\b/.test(normalized)) return 'LOGEMENT';
      if (/\\b(sante|medecin|rdv medical|psychologue)\\b/.test(normalized)) return 'SANTE';
      return 'EMPLOI';
    }

    function qualificationHumanLabel(code) {
      const labels = {
        EMPLOI: 'emploi',
        PROJET_PROFESSIONNEL: 'projet professionnel',
        CULTURE_SPORT_LOISIRS: 'culture / sport / loisirs',
        CITOYENNETE: 'citoyennete',
        FORMATION: 'formation',
        LOGEMENT: 'logement',
        SANTE: 'sante',
      };
      return labels[code] || code.toLowerCase();
    }

    function looksLikeActivityQuestion(text) {
      const normalized = normalizeSearchText(text);
      return /(ca passe|ça passe|est ce que|peux|peut|mettre|action cej)/.test(normalized)
        && /\\b(cinema|film|musee|sport|cv|lettre|emploi|formation|cours|code|demarche|aide|rdv|atelier|permis)\\b/.test(normalized);
    }

    function extractActivityDetails(text) {
      const raw = String(text || '').trim();
      const normalized = normalizeSearchText(raw);
      let subject = raw;
      const voirMatch = raw.match(/(?:voir|vu)\\s+(.+?)(?:\\s+(?:au|a|à|dans|chez)\\s+.+)?$/i);
      if (voirMatch) subject = voirMatch[1].trim();
      subject = subject
        .replace(/^(j'ai|jai|je suis|j’etais|j'etais|été|ete|voir|vu)\\s+/i, '')
        .replace(/\\s+/g, ' ')
        .trim();
      const locationMatch = raw.match(/\\b(?:au|a|à|dans|chez)\\s+([^,.]+)$/i);
      const location = locationMatch && !/je ne sais pas|sais pas|aucun/i.test(raw) ? locationMatch[1].trim() : '';
      return {
        subject,
        location,
        unknownLocation: normalized.includes('je ne sais pas') || normalized.includes('sais pas'),
      };
    }

    function buildActivityAction(draft) {
      const qualification = draft.qualification || inferQualification(draft.activityText || draft.details || '');
      const details = extractActivityDetails(draft.details || draft.activityText || '');
      const subject = details.subject || draft.activityText || 'activite realisee';
      let title = subject;
      let comment = '';
      if (qualification === 'CULTURE_SPORT_LOISIRS' && /cinema|film|voir|vu/i.test(`${draft.activityText} ${draft.details}`)) {
        title = `Sortie cinema - ${subject}`;
        comment = `Sortie culturelle au cinema pour voir "${subject}".`;
        if (details.location) comment += ` Lieu: ${details.location}.`;
        if (details.unknownLocation) comment += ' Lieu non precise.';
      } else {
        title = subject.length > 52 ? subject.slice(0, 49) + '...' : subject;
        comment = draft.details || draft.activityText || subject;
      }
      return {
        title,
        comment,
        due: draft.due || formatDateInput(new Date()),
        qualification,
      };
    }

    function buildGenericAction(draft) {
      const raw = `${draft.title || ''} ${draft.details || ''}`.trim();
      const qualification = draft.qualification || inferQualification(raw);
      const normalized = normalizeSearchText(raw);
      if (normalized.includes('lettre') && normalized.includes('motivation')) {
        const target = raw.match(/(?:pour|chez)\\s+(.+)$/i)?.[1]?.trim() || '';
        const suffix = target ? ` pour ${target}` : '';
        return {
          title: `Redaction lettre de motivation${suffix}`,
          comment: `Redaction et preparation d'une lettre de motivation${suffix}.`,
          due: draft.due || formatDateInput(new Date()),
          qualification: 'EMPLOI',
        };
      }
      return {
        title: draft.title || raw || 'Action CEJ',
        comment: draft.details || draft.title || raw || '',
        due: draft.due || formatDateInput(new Date()),
        qualification,
      };
    }

    function titleLooksUsable(title) {
      const normalized = normalizeSearchText(title);
      if (normalized.length < 3) return false;
      const weakTitles = new Set(['test', 'tes t', 'tache', 'truc', 'action', 'un', 'une']);
      return !weakTitles.has(normalized);
    }

    function mergePendingCreateDraft(message) {
      const trimmed = String(message || '').trim();
      const due = dateFromText(trimmed);
      const title = cleanCreateTitle(trimmed);

      if (pendingAgentDraft?.type === 'activity_action') {
        if (pendingAgentDraft.step === 'ask_create') {
          if (due && !isYes(trimmed)) {
            pendingAgentDraft.due = due;
            addYokiMessage('yoki', `Ok, je note la date ${due}. Tu veux que je prepare l'action ?`);
            return { ok: true, message: 'Yoki attend ta confirmation.' };
          }
          if (!isYes(trimmed)) {
            pendingAgentDraft = null;
            addYokiMessage('yoki', 'Ok, je ne cree rien. Si tu veux la noter plus tard, redemande-moi.');
            return { ok: true, message: '' };
          }
          pendingAgentDraft.step = 'ask_details';
          addYokiMessage('yoki', 'Ok. Donne-moi juste le detail principal: quoi exactement, et le lieu si tu l’as.');
          return { ok: true, message: 'Yoki attend les details.' };
        }
        if (pendingAgentDraft.step === 'ask_details') {
          pendingAgentDraft.details = trimmed;
          const action = buildActivityAction(pendingAgentDraft);
          pendingAgentDraft = null;
          return showCreateProposal(action.title, action.due, action.comment, action.qualification);
        }
      }

      if (pendingAgentDraft?.type === 'create_action') {
        if (wantsGeneration(trimmed)) {
          const action = buildGenericAction(pendingAgentDraft);
          pendingAgentDraft = null;
          return showCreateProposal(action.title, action.due, action.comment, action.qualification);
        }
        if (due && !titleLooksUsable(title)) {
          pendingAgentDraft.due = due;
          addYokiMessage('yoki', `Ok, je mets la date au ${due}.`);
          if (pendingAgentDraft.title) {
            return showCreateProposal(
              pendingAgentDraft.title,
              pendingAgentDraft.due,
              pendingAgentDraft.comment || '',
              pendingAgentDraft.qualification || inferQualification(pendingAgentDraft.title)
            );
          }
          addYokiMessage('yoki', 'Il me manque encore le titre de l’action.');
          return { ok: true, message: 'Yoki attend le titre.' };
        }
      }

      if (looksLikeActivityQuestion(trimmed)) {
        const qualification = inferQualification(trimmed);
        const dueFromText = due || formatDateInput(new Date());
        pendingAgentDraft = {
          type: 'activity_action',
          step: 'ask_create',
          activityText: trimmed,
          due: dueFromText,
          qualification,
        };
        addYokiMessage('yoki', `Oui, ca peut passer en action CEJ dans "${qualificationHumanLabel(qualification)}". Tu veux que je prepare une action pour le ${dueFromText} ?`);
        return { ok: true, message: 'Yoki attend ta confirmation.' };
      }

      if (pendingAgentDraft?.type === 'create_action' && !isCreateIntent(trimmed)) {
        const mergedTitle = titleLooksUsable(title) ? title : trimmed;
        const mergedDue = due || pendingAgentDraft.due;
        if (titleLooksUsable(mergedTitle) && mergedDue) {
          pendingAgentDraft = null;
          return showCreateProposal(mergedTitle, mergedDue, '', inferQualification(mergedTitle));
        }
        if (titleLooksUsable(mergedTitle)) {
          pendingAgentDraft.title = mergedTitle;
          pendingAgentDraft.details = mergedTitle;
          pendingAgentDraft.qualification = inferQualification(mergedTitle);
          addYokiMessage('yoki', `J'ai le titre "${mergedTitle}". Pour quelle date ?`);
          return { ok: true, message: 'Yoki attend la date.' };
        }
      }

      if (!isCreateIntent(trimmed)) return null;

      if (titleLooksUsable(title) && due) {
        pendingAgentDraft = null;
        return showCreateProposal(title, due, '', inferQualification(title));
      }

      pendingAgentDraft = {
        type: 'create_action',
        title: titleLooksUsable(title) ? title : '',
        due: due || '',
        details: titleLooksUsable(title) ? title : '',
        qualification: inferQualification(trimmed),
      };

      if (!pendingAgentDraft.title && pendingAgentDraft.due) {
        addYokiMessage('yoki', `J'ai la date (${pendingAgentDraft.due}). Quel titre veux-tu donner a l'action ?`);
        return { ok: true, message: 'Yoki attend le titre.' };
      }
      if (pendingAgentDraft.title && !pendingAgentDraft.due) {
        addYokiMessage('yoki', `J'ai le titre "${pendingAgentDraft.title}". Pour quelle date ?`);
        return { ok: true, message: 'Yoki attend la date.' };
      }

      addYokiMessage('yoki', 'Je peux creer une action. Donne-moi au moins un titre et une date.');
      return { ok: true, message: 'Yoki attend les details.' };
    }

    function extractQuotedText(text) {
      const match = String(text || '').match(/["“”'‘’]([^"“”'‘’]{2,})["“”'‘’]/);
      return match ? match[1].trim() : '';
    }

    function actionToClient(action) {
      return {
        id: action.id,
        title: action.content || '',
        comment: action.comment || '',
        date: normalizeActionDate(action),
        status: action.status || 'not_started',
        qualification: action.qualification?.code || 'EMPLOI',
      };
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
      if (action.syncState === 'syncing' || action.syncState === 'error') return;
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

    function syncStatusPill(action) {
      if (action.syncState === 'syncing') return '<span class="pill">Synchronisation...</span>';
      if (action.syncState === 'error') return '<span class="pill status-canceled">Non synchronise</span>';
      return '';
    }

    function multiDayPill(action) {
      if (!action.multiGroupId || !action.multiCount || action.multiCount < 2) return '';
      return `<span class="pill">${action.multiCount} jours</span>`;
    }

    function syncControls(action) {
      if (action.syncState !== 'error') return '';
      const message = escapeHtml(action.syncError || 'Erreur de synchronisation.');
      return `
        <div class="sync-row error">
          <span>${message}</span>
          <button class="mini-btn retry-sync" type="button" data-action-id="${escapeHtml(action.id)}">Reessayer</button>
          <button class="mini-btn cancel-sync" type="button" data-action-id="${escapeHtml(action.id)}">Annuler</button>
        </div>
      `;
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
        await moveActionOptimistic(clipboard.action.id, targetDate);
        clipboard = null;
        return;
      }

      const payload = {
        title: clipboard.action.content,
        comment: clipboard.action.comment,
        due: targetDate,
        qualification: clipboard.action.qualification,
        status: clipboard.action.status,
      };
      clipboard = null;
      await createActionsOptimistic([payload]);
    }

    async function moveActionByQuery(query, targetDate) {
      if (!targetDate) return { ok: false, error: 'Date cible manquante.' };
      const match = requireSingleAction(query);
      if (!match.ok) return match;
      const result = await api('/api/update-action', { id: match.action.id, due: targetDate });
      if (!result.ok) return { ok: false, error: result?.error || 'Echec du deplacement.' };
      await loadCurrentWeek(false);
      return { ok: true, message: `Action "${match.action.content || match.action.id}" deplacee au ${targetDate}.` };
    }

    async function updateActionByQuery(query, changes) {
      const match = requireSingleAction(query);
      if (!match.ok) return match;
      const payload = { id: match.action.id };
      const allowedKeys = new Set(['status', 'title', 'comment', 'due', 'qualification', 'date_fin_reelle']);
      for (const [key, value] of Object.entries(changes || {})) {
        if (allowedKeys.has(key) && typeof value === 'string' && value.trim()) payload[key] = value.trim();
      }
      if (Object.keys(payload).length === 1) return { ok: false, error: 'Aucune modification exploitable.' };
      const result = await api('/api/update-action', payload);
      if (!result.ok) return { ok: false, error: result?.error || 'Echec de modification.' };
      await loadCurrentWeek(false);
      return { ok: true, message: `Action "${match.action.content || match.action.id}" modifiee.` };
    }

    async function createActionFromAgent(action) {
      await createActionsOptimistic([{
        title: action?.title || action?.content || '',
        comment: action?.comment || '',
        due: action?.due || action?.date || formatDateInput(selectedWeekStart),
        qualification: action?.qualification || 'EMPLOI',
        status: action?.status || 'done',
      }]);
      return { ok: true, message: 'Action creee.' };
    }

    async function createActionsOptimistic(payloads) {
      const groupId = payloads.length > 1 ? makeClientId() : '';
      const locals = payloads.map(payload => makeLocalAction(payload, payload.due, {
        multiGroupId: groupId,
        multiCount: payloads.length,
      }));
      currentActions = [...currentActions, ...locals];
      updateLiveViews();
      setNotice(els.agendaNotice, true, payloads.length > 1 ? 'Actions ajoutees, synchronisation en cours.' : 'Action ajoutee, synchronisation en cours.');
      const results = await Promise.all(locals.map(action => syncCreateAction(action)));
      if (results.every(Boolean)) await loadCurrentWeek(false);
    }

    async function syncCreateAction(localAction) {
      const result = await api('/api/create', localAction.syncPayload);
      applyUnauthorizedState(result);
      if (!result.ok) {
        const action = findActionByAnyId(localAction.id);
        if (action) {
          action.syncState = 'error';
          action.syncError = result?.error || 'Creation impossible.';
        }
        updateLiveViews();
        return false;
      }
      const action = findActionByAnyId(localAction.id);
      if (action) {
        action.syncState = '';
        action.syncError = '';
      }
      setNotice(els.agendaNotice, true, 'Synchronisation terminee.');
      updateLiveViews();
      return true;
    }

    async function moveActionOptimistic(actionId, targetDate) {
      const action = findActionByAnyId(actionId);
      if (!action || action.syncState === 'syncing') return;
      const previousDue = actionDateValue(action);
      action.previousDue = previousDue;
      action.localDue = targetDate;
      action.dateEcheance = `${targetDate}T12:00:00.000Z`;
      action.syncState = 'syncing';
      action.syncError = '';
      action.syncAction = 'move';
      action.syncPayload = { id: action.id, due: targetDate };
      updateLiveViews();

      const result = await api('/api/update-action', { id: action.id, due: targetDate });
      applyUnauthorizedState(result);
      const live = findActionByAnyId(action.id);
      if (!live) return;
      if (!result.ok) {
        live.syncState = 'error';
        live.syncError = result?.error || 'Deplacement impossible.';
        live.syncAction = 'move';
        live.previousDue = previousDue;
        updateLiveViews();
        return;
      }
      live.syncState = '';
      live.syncError = '';
      setNotice(els.agendaNotice, true, 'Action deplacee.');
      updateLiveViews();
    }

    async function retrySyncAction(actionId) {
      const action = findActionByAnyId(actionId);
      if (!action) return;
      action.syncState = 'syncing';
      action.syncError = '';
      updateLiveViews();
      if (action.syncAction === 'move') {
        const targetDate = action.localDue || actionDateValue(action);
        const result = await api('/api/update-action', { id: action.id, due: targetDate });
        applyUnauthorizedState(result);
        const live = findActionByAnyId(action.id);
        if (!live) return;
        if (!result.ok) {
          live.syncState = 'error';
          live.syncError = result?.error || 'Deplacement impossible.';
        } else {
          live.syncState = '';
          live.syncError = '';
          setNotice(els.agendaNotice, true, 'Action synchronisee.');
        }
        updateLiveViews();
        return;
      }
      const ok = await syncCreateAction(action);
      if (ok) await loadCurrentWeek(false);
    }

    function cancelSyncAction(actionId) {
      const action = findActionByAnyId(actionId);
      if (!action) return;
      if (action.syncAction === 'move' && action.previousDue) {
        action.localDue = action.previousDue;
        action.dateEcheance = `${action.previousDue}T12:00:00.000Z`;
        action.syncState = '';
        action.syncError = '';
      } else {
        currentActions = currentActions.filter(item => item.id !== action.id && item.clientId !== action.clientId);
      }
      updateLiveViews();
      setNotice(els.agendaNotice, true, 'Action locale annulee.');
    }

    async function deleteActionByQuery(query) {
      const match = requireSingleAction(query);
      if (!match.ok) return match;
      const result = await api('/api/delete-action', { id: match.action.id });
      if (!result.ok) return { ok: false, error: result?.error || 'Echec de suppression.' };
      await loadCurrentWeek(false);
      return { ok: true, message: `Action "${match.action.content || match.action.id}" supprimee.` };
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

    function weekDateChoices() {
      return Array.from({ length: 7 }, (_, index) => {
        const date = addDays(selectedWeekStart, index);
        return { label: weekdayNames[index], value: formatDateInput(date), short: date.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' }) };
      });
    }

    function renderCreateDateControls(selectedDate) {
      const choices = weekDateChoices();
      els.createDateChoices.innerHTML = '';
      els.createMultiDays.innerHTML = '';
      for (const choice of choices) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `date-choice${choice.value === selectedDate ? ' active' : ''}`;
        button.textContent = `${choice.label} ${choice.short}`;
        button.dataset.date = choice.value;
        button.addEventListener('click', () => {
          creatingDate = choice.value;
          els.createOtherDate.value = choice.value;
          renderCreateDateControls(choice.value);
        });
        els.createDateChoices.appendChild(button);

        const label = document.createElement('label');
        label.className = 'day-check';
        label.innerHTML = `<input type="checkbox" value="${choice.value}"> ${choice.label}`;
        const input = label.querySelector('input');
        input.checked = choice.value === selectedDate;
        els.createMultiDays.appendChild(label);
      }
    }

    function openCreateModal(targetDate = formatDateInput(selectedWeekStart)) {
      creatingDate = targetDate;
      els.createTitle.value = '';
      els.createComment.value = '';
      els.createQualification.value = 'EMPLOI';
      els.createStatus.value = 'done';
      els.createOtherDate.value = targetDate;
      els.createMultiDay.checked = false;
      renderCreateDateControls(targetDate);
      els.createMultiDays.style.display = 'none';
      setNotice(els.createNotice, true, '');
      openModal(els.createModal);
      setTimeout(() => els.createTitle.focus(), 0);
    }

    function selectedCreateDates() {
      if (els.createMultiDay.checked) {
        const values = Array.from(els.createMultiDays.querySelectorAll('input:checked')).map(input => input.value);
        return values.length ? values : [creatingDate || els.createOtherDate.value];
      }
      return [els.createOtherDate.value || creatingDate || formatDateInput(selectedWeekStart)];
    }

    async function submitCreateModal() {
      const title = els.createTitle.value.trim();
      if (!title) {
        setNotice(els.createNotice, false, 'Titre requis.');
        return;
      }
      const basePayload = {
        title,
        comment: els.createComment.value.trim(),
        qualification: els.createQualification.value,
        status: els.createStatus.value || 'done',
      };
      const dates = selectedCreateDates().filter(Boolean);
      if (!dates.length) {
        setNotice(els.createNotice, false, 'Date requise.');
        return;
      }
      closeModal(els.createModal);
      await createActionsOptimistic(dates.map(due => ({ ...basePayload, due })));
    }

    async function quickCreateAction() {
      openCreateModal(formatDateInput(selectedWeekStart));
    }

    async function executeAgentCommand(command) {
      if (typeof command === 'string') return executeAgentText(command);
      if (!command || typeof command !== 'object') return { ok: false, error: 'Commande agent invalide.' };

      const type = command.type || command.action;
      if (type === 'show_agenda') {
        switchSection('agendaSection');
        return { ok: true, message: 'Agenda affiche.' };
      }
      if (type === 'show_settings') {
        switchSection('loginSection');
        return { ok: true, message: 'Parametres affiches.' };
      }
      if (type === 'load_week') {
        switchSection('agendaSection');
        await loadCurrentWeek(false);
        return { ok: true, message: 'Semaine chargee.' };
      }
      if (type === 'analyze_week') {
        switchSection('agendaSection');
        await loadCurrentWeek(true);
        return { ok: true, message: 'Analyse lancee.' };
      }
      if (type === 'week_offset') {
        selectedWeekStart = addDays(selectedWeekStart, Number(command.offset || 0) * 7);
        syncWeekInputs();
        await loadCurrentWeek(false);
        return { ok: true, message: 'Semaine changee.' };
      }
      if (type === 'current_week') {
        selectedWeekStart = startOfWeek(new Date());
        syncWeekInputs();
        await loadCurrentWeek(false);
        return { ok: true, message: 'Semaine actuelle affichee.' };
      }
      if (type === 'move_action') return moveActionByQuery(command.query || command.title || command.id, command.due || command.date);
      if (type === 'update_action') return updateActionByQuery(command.query || command.title || command.id, command.changes || command);
      if (type === 'create_action') return createActionFromAgent(command);
      if (type === 'delete_action') return deleteActionByQuery(command.query || command.title || command.id);
      return { ok: false, error: `Commande agent inconnue: ${type || 'vide'}.` };
    }

    function shouldConfirmToolCall(toolCall, confidence = 0.65, requested = false) {
      const name = toolCall?.name;
      if (!name) return { confirm: false };
      if (name === 'delete_action') return { confirm: true, reason: 'Yoki veut supprimer une action. Validation obligatoire.' };
      if (requested) return { confirm: true };
      if (confidence < yokiSettings.confidenceThreshold) return { confirm: true, reason: 'Yoki manque de confiance, validation requise.' };
      if (yokiSettings.autonomyMode === 'strict') return { confirm: true, reason: 'Mode strict: validation requise.' };
      if (yokiSettings.autonomyMode === 'prudent' && !['create_action', 'change_week', 'analyze_week'].includes(name)) {
        return { confirm: true, reason: 'Mode prudent: validation requise.' };
      }
      return { confirm: false };
    }

    function addYokiActionLog(entry) {
      yokiActionLog.push({ ...entry, at: new Date().toISOString() });
      yokiActionLog = yokiActionLog.slice(-50);
      saveJsonStorage(yokiLogKey, yokiActionLog);
      renderYokiUsage();
    }

    function actionSnapshot(action) {
      if (!action) return null;
      return {
        id: action.id,
        title: action.content || '',
        comment: action.comment || '',
        due: normalizeActionDate(action),
        status: action.status || 'not_started',
        qualification: action.qualification?.code || 'EMPLOI',
      };
    }

    async function executeYokiToolCall(toolCall) {
      const name = toolCall?.name;
      const args = toolCall?.arguments || {};
      if (name === 'propose_action') {
        return showCreateProposal(args.title || 'Action CEJ', args.due, args.comment || '', args.qualification || 'EMPLOI');
      }
      if (name === 'create_action') {
        const result = await api('/api/create', {
          title: args.title || '',
          comment: args.comment || '',
          due: args.due || '',
          qualification: args.qualification || 'EMPLOI',
        });
        if (result.ok) {
          addYokiActionLog({
            type: 'create_action',
            summary: `Creation "${args.title}"`,
            undo: { type: 'delete_created', id: result.data?.id || result.data?.action?.id || '' },
          });
          await loadCurrentWeek(false);
        }
        return { ok: result.ok, message: result.ok ? `Yoki a cree: ${args.title}` : (result.error || 'Creation impossible.') };
      }
      if (name === 'move_action') {
        const match = args.id ? { ok: true, action: currentActions.find(action => action.id === args.id) } : requireSingleAction(args.query || args.title || '');
        if (!match.ok || !match.action) return { ok: false, error: match.error || 'Action introuvable.' };
        const before = actionSnapshot(match.action);
        const result = await api('/api/update-action', { id: match.action.id, due: args.due });
        if (result.ok) {
          addYokiActionLog({ type: 'move_action', summary: `Deplacement "${before.title}"`, undo: { type: 'restore_action', before } });
          await loadCurrentWeek(false);
        }
        return { ok: result.ok, message: result.ok ? `Yoki a deplace: ${before.title}` : (result.error || 'Deplacement impossible.') };
      }
      if (name === 'update_action') {
        const match = args.id ? { ok: true, action: currentActions.find(action => action.id === args.id) } : requireSingleAction(args.query || args.title || '');
        if (!match.ok || !match.action) return { ok: false, error: match.error || 'Action introuvable.' };
        const before = actionSnapshot(match.action);
        const changes = args.changes || {};
        const result = await api('/api/update-action', { id: match.action.id, ...changes });
        if (result.ok) {
          addYokiActionLog({ type: 'update_action', summary: `Modification "${before.title}"`, undo: { type: 'restore_action', before } });
          await loadCurrentWeek(false);
        }
        return { ok: result.ok, message: result.ok ? `Yoki a modifie: ${before.title}` : (result.error || 'Modification impossible.') };
      }
      if (name === 'delete_action') {
        const match = args.id ? { ok: true, action: currentActions.find(action => action.id === args.id) } : requireSingleAction(args.query || args.title || '');
        if (!match.ok || !match.action) return { ok: false, error: match.error || 'Action introuvable.' };
        const before = actionSnapshot(match.action);
        const result = await api('/api/delete-action', { id: match.action.id });
        if (result.ok) {
          addYokiActionLog({ type: 'delete_action', summary: `Suppression "${before.title}"`, undo: { type: 'none' } });
          await loadCurrentWeek(false);
        }
        return { ok: result.ok, message: result.ok ? `Yoki a supprime: ${before.title}` : (result.error || 'Suppression impossible.') };
      }
      if (name === 'change_week') {
        selectedWeekStart = args.week_start ? startOfWeek(new Date(args.week_start)) : addDays(selectedWeekStart, Number(args.offset || 0) * 7);
        syncWeekInputs();
        await loadCurrentWeek(false);
        return { ok: true, message: 'Yoki a change la semaine.' };
      }
      if (name === 'analyze_week') {
        await loadCurrentWeek(true);
        return { ok: true, message: 'Yoki a analyse la semaine.' };
      }
      if (name === 'ask_user') return { ok: true, message: args.question || '' };
      return { ok: false, error: 'Outil Yoki inconnu.' };
    }

    async function undoLastYokiAction() {
      const last = yokiActionLog[yokiActionLog.length - 1];
      if (!last?.undo) return { ok: false, error: 'Aucune action Yoki annulable.' };
      let result = { ok: false, error: 'Action non annulable.' };
      if (last.undo.type === 'delete_created' && last.undo.id) result = await api('/api/delete-action', { id: last.undo.id });
      if (last.undo.type === 'restore_action' && last.undo.before) {
        const before = last.undo.before;
        result = await api('/api/update-action', {
          id: before.id,
          title: before.title,
          comment: before.comment,
          due: before.due,
          qualification: before.qualification,
          status: before.status,
        });
      }
      if (result.ok) {
        yokiActionLog.pop();
        saveJsonStorage(yokiLogKey, yokiActionLog);
        renderYokiUsage();
        await loadCurrentWeek(false);
      }
      return result;
    }

    async function executeAgentText(text) {
      const raw = String(text || '').trim();
      const normalized = normalizeSearchText(raw);
      if (!normalized) return { ok: false, error: 'Commande vide.' };
      if (normalized.includes('parametre') || normalized.includes('reglage')) return executeAgentCommand({ type: 'show_settings' });
      if (normalized.includes('agenda') || normalized.includes('calendrier')) return executeAgentCommand({ type: 'show_agenda' });
      if (normalized.includes('semaine actuelle') || normalized.includes('cette semaine')) return executeAgentCommand({ type: 'current_week' });
      if (normalized.includes('semaine suivante') || normalized.includes('prochaine semaine')) return executeAgentCommand({ type: 'week_offset', offset: 1 });
      if (normalized.includes('semaine precedente') || normalized.includes('semaine avant')) return executeAgentCommand({ type: 'week_offset', offset: -1 });
      if (normalized.includes('analyse')) return executeAgentCommand({ type: 'analyze_week' });
      if (normalized.includes('charger') || normalized.includes('afficher')) return executeAgentCommand({ type: 'load_week' });

      const date = firstDateInText(raw);
      const quoted = extractQuotedText(raw);
      const looseQuery = quoted || raw
        .replace(/\\b(20\\d{2}-\\d{2}-\\d{2})\\b/g, '')
        .replace(/\\b\\d{1,2}[\\/.-]\\d{1,2}[\\/.-]\\d{2,4}\\b/g, '')
        .replace(/deplace|deplacer|deplacee|action|au|le|la|l'|vers|pour|passe|passer|mettre|met|en|statut|supprime|supprimer/gi, ' ')
        .trim();

      if ((normalized.includes('deplace') || normalized.includes('deplacer')) && date) {
        return moveActionByQuery(looseQuery, date);
      }

      const statusMatch = normalized.match(/\\b(done|termine|terminee|not_started|pas commence|in_progress|en cours|canceled|annule|annulee)\\b/);
      if ((normalized.includes('passe') || normalized.includes('statut') || normalized.includes('mettre')) && statusMatch) {
        const statusMap = {
          termine: 'done',
          terminee: 'done',
          'pas commence': 'not_started',
          'en cours': 'in_progress',
          annule: 'canceled',
          annulee: 'canceled',
        };
        const status = statusMap[statusMatch[1]] || statusMatch[1];
        return updateActionByQuery(looseQuery, { status });
      }

      if (normalized.includes('supprime') || normalized.includes('supprimer')) {
        return deleteActionByQuery(looseQuery);
      }

      return { ok: false, error: 'Commande pas assez claire. Utilise une phrase du type: deplace "CV" au 2026-04-03, ou une commande JSON via window.cejAgent.execute().' };
    }

    async function fetchWithTimeout(url, options, timeoutMs = 12000) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, { ...options, signal: controller.signal });
      } finally {
        clearTimeout(timeout);
      }
    }

    async function chatWithYoki(message) {
      const neurons = estimateNeurons(message) + Math.ceil(JSON.stringify(currentActions.map(actionToClient)).length / 200);
      if (yokiSettings.costGuard && yokiUsage.neurons + neurons > yokiSettings.neuronDailyLimit) {
        return {
          ok: false,
          message: 'Yoki est bloque par le garde-fou quota IA local. Augmente le quota dans Parametre si tu veux continuer.',
          error: 'quota_guard',
        };
      }
      const response = await fetchWithTimeout(aiChatEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          history: conversationHistory.slice(-10).map(item => ({ role: item.role, content: item.content })),
          state: {
            today: new Date().toISOString().slice(0, 10),
            weekStart: formatDateInput(selectedWeekStart),
            weekEnd: formatDateInput(addDays(selectedWeekStart, 6)),
            actions: currentActions.map(actionToClient),
          },
          settings: yokiSettings,
        }),
      });
      const result = await response.json();
      yokiUsage.requests += 1;
      yokiUsage.neurons += neurons;
      saveJsonStorage(yokiUsageKey, yokiUsage);
      renderYokiUsage(result.usage);
      setYokiRaw(result.raw || JSON.stringify(result, null, 2));
      if (!response.ok || !result.ok) return { ok: false, message: result?.message || 'Yoki n’arrive pas a reflechir pour l’instant.', error: result?.error || 'ia_error' };
      return result;
    }

    async function runAgentFromInput(message) {
      const trimmed = String(message || '').trim();
      if (!trimmed) return { ok: false, error: 'Commande vide.' };

      try {
        const answer = await chatWithYoki(trimmed);
        addYokiMessage('yoki', answer.message || (answer.ok ? 'Commande recue.' : 'Je n’arrive pas a reflechir pour l’instant.'));
        if (!answer.ok || !answer.tool_call) return { ok: answer.ok, message: '', error: answer.error };
        const decision = shouldConfirmToolCall(answer.tool_call, answer.confidence, answer.needs_confirmation);
        if (decision.confirm) {
          showAgentProposal({
            explanation: decision.reason || answer.message || 'Yoki propose une action.',
            command: answer.tool_call,
            tool_call: answer.tool_call,
          });
          return { ok: true, message: 'Yoki attend ta validation.' };
        }
        clearAgentProposal();
        return executeYokiToolCall(answer.tool_call);
      } catch (error) {
        addYokiMessage('yoki', 'Yoki n’arrive pas a reflechir pour l’instant. Reessaie ou fais l’action manuellement.');
        clearAgentProposal();
        return { ok: false, error: 'Timeout ou erreur IA.' };
      }
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
          label: 'Creer une action ici',
          onClick: () => openCreateModal(targetDate),
        },
        {
          label: clipboard ? 'Coller ici' : 'Coller ici (vide)',
          disabled: !clipboard,
          onClick: () => pasteToDate(targetDate),
        },
      ]);
    }

    function renderCalendar(data) {
      currentActions = Array.isArray(data?.actions) ? data.actions : [];
      updateLiveViews();
    }

    function renderCalendarFromActions() {
      els.calendarGrid.innerHTML = '';
      const actions = currentActions.filter(action => action.syncState !== 'deleted');
      const buckets = new Map();
      for (let i = 0; i < 7; i++) {
        const day = addDays(selectedWeekStart, i);
        buckets.set(formatDateInput(day), []);
      }
      for (const action of actions) {
        const key = actionDateValue(action);
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
            <button class="ghost mini-btn create-day-btn" type="button">Creer</button>
          </div>
          <div class="calendar-body"></div>
        `;
        const body = column.querySelector('.calendar-body');
        column.querySelector('button').addEventListener('click', () => openCreateModal(key));
        column.addEventListener('dblclick', event => {
          if (!event.target.closest('.agenda-item, button')) openCreateModal(key);
        });
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
          await moveActionOptimistic(dragged.id, key);
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
            node.className = `agenda-item${action.status && action.status !== 'done' ? ' ' + action.status : ''}${action.status === 'not_started' ? ' pending' : ''}${action.syncState === 'syncing' ? ' syncing' : ''}${action.syncState === 'error' ? ' sync-error' : ''}`;
            node.draggable = action.syncState !== 'syncing';
            node.addEventListener('contextmenu', event => openActionContextMenu(event, action));
            node.addEventListener('dragstart', event => {
              if (action.syncState === 'syncing') {
                event.preventDefault();
                return;
              }
              node.classList.add('dragging');
              event.dataTransfer.setData('application/json', JSON.stringify({ id: action.id }));
            });
            node.addEventListener('dragend', () => node.classList.remove('dragging'));
            const qualif = action.qualification?.code || '';
            node.innerHTML = `
              <div class="agenda-tags">
                ${categoryPill(action)}
                ${statusPill(action)}
                ${syncStatusPill(action)}
                ${multiDayPill(action)}
              </div>
              <strong>${escapeHtml(action.content || '(sans titre)')}</strong>
              ${action.comment ? `<p>${escapeHtml(action.comment)}</p>` : ''}
              <div class="meta">${escapeHtml(actionDateValue(action) || action.dateFinReelle || action.dateEcheance || '')}${qualif ? ' | ' + escapeHtml(qualif) : ''}</div>
              ${syncControls(action)}
            `;
            body.appendChild(node);
          }
        }
        els.calendarGrid.appendChild(column);
      }
    }

    function renderActionList() {
      const today = new Date().toISOString().slice(0, 10);
      let actions = currentActions.filter(action => action.syncState !== 'deleted');
      if (activeListFilter === 'errors') actions = actions.filter(action => action.syncState === 'error');
      if (activeListFilter === 'future') actions = actions.filter(action => actionDateValue(action) >= today);
      if (activeListFilter === 'multi') actions = actions.filter(action => action.multiGroupId);

      els.actionList.innerHTML = '';
      if (!actions.length) {
        const empty = document.createElement('div');
        empty.className = 'calendar-empty';
        empty.textContent = 'Aucune action dans cette vue.';
        els.actionList.appendChild(empty);
        return;
      }

      for (const action of actions.slice().sort((a, b) => actionDateValue(a).localeCompare(actionDateValue(b)))) {
        const row = document.createElement('div');
        row.className = `action-row${action.syncState === 'syncing' ? ' syncing' : ''}${action.syncState === 'error' ? ' sync-error' : ''}`;
        row.innerHTML = `
          <div><strong>${escapeHtml(actionDateValue(action) || '-')}</strong></div>
          <div>
            <strong>${escapeHtml(action.content || '(sans titre)')}</strong>
            ${action.comment ? `<div class="meta">${escapeHtml(action.comment)}</div>` : ''}
            ${syncControls(action)}
          </div>
          <div class="agenda-tags">
            ${categoryPill(action)}
            ${multiDayPill(action)}
          </div>
          <div class="agenda-tags">
            ${statusPill(action) || '<span class="pill">done</span>'}
            ${syncStatusPill(action)}
          </div>
        `;
        els.actionList.appendChild(row);
      }
    }

    function setActiveView(view) {
      activeView = view;
      els.agendaViewBtn.classList.toggle('active', view === 'agenda');
      els.listViewBtn.classList.toggle('active', view === 'list');
      els.calendarGrid.classList.toggle('hidden-view', view !== 'agenda');
      els.actionListPanel.classList.toggle('active', view === 'list');
      els.actionListPanel.setAttribute('aria-hidden', view === 'list' ? 'false' : 'true');
      renderActionList();
    }

    function renderAnalysis(analysis) {
      els.weeksGrid.innerHTML = '';
      if (!analysis) {
        els.analysisSummary.textContent = '';
        return;
      }
      const balance = Number(analysis.balance_hours_total || 0);
      const balanceText = balance >= 0 ? `+${balance}h` : `${balance}h`;
      const averageStatus = analysis.average_ok ? 'moyenne OK' : 'moyenne insuffisante';
      const periodTarget = analysis.period_target_hours || analysis.target_hours_total;
      const periodBalance = Number(analysis.period_balance_hours ?? balance);
      const periodBalanceText = periodBalance >= 0 ? `+${periodBalance}h` : `${periodBalance}h`;
      els.analysisSummary.textContent = `Periode: ${analysis.period_days || '-'} jour(s) | moyenne ${analysis.average_hours_per_week}h/semaine (${averageStatus}) | total estime ${analysis.estimated_hours_total}/${periodTarget}h | balance ${periodBalanceText}`;

      for (const week of analysis.weeks || []) {
        const div = document.createElement('div');
        const weekBalance = Number(week.balance_hours || 0);
        div.className = `week-card${weekBalance < 0 ? ' missing' : ''}`;
        const titles = Array.isArray(week.titles) && week.titles.length ? week.titles.slice(0, 3).join(' | ') : 'Aucune action';
        const weekBalanceText = weekBalance >= 0 ? `+${weekBalance}h` : `${weekBalance}h`;
        div.innerHTML = `
          <strong>${week.week_start} → ${week.week_end}</strong>
          <div class="meta" style="margin-top:6px">actions=${week.count} | estime=${week.estimated_hours}/${week.target_hours}h | balance=${weekBalanceText}</div>
          <div style="margin-top:8px">${titles}</div>
        `;
        els.weeksGrid.appendChild(div);
      }
    }

    async function fetchAnalysisRange(fromDate, toDate) {
      const result = await api('/api/analyze', {
        from_date: fromDate,
        to_date: toDate,
      });
      applyUnauthorizedState(result);
      if (!result.ok) throw new Error(result?.error || 'Analyse impossible.');
      return result.analysis;
    }

    function completedWeeksInMonth(analysis) {
      const today = new Date().toISOString().slice(0, 10);
      return (analysis?.weeks || []).filter(week => week.week_end < today);
    }

    function currentWeekInAnalysis(analysis) {
      const today = new Date().toISOString().slice(0, 10);
      return (analysis?.weeks || []).find(week => week.week_start <= today && week.week_end >= today) || null;
    }

    function averageFromAnalyses(analyses) {
      const valid = analyses.filter(Boolean);
      if (!valid.length) return 0;
      const total = valid.reduce((sum, item) => sum + Number(item.average_hours_per_week || 0), 0);
      return Math.round((total / valid.length) * 10) / 10;
    }

    function categoryAverageFromAnalyses(analyses) {
      const totals = new Map();
      const valid = analyses.filter(Boolean);
      for (const analysis of valid) {
        for (const category of analysis.categories || []) {
          const code = category.code || 'Sans categorie';
          totals.set(code, (totals.get(code) || 0) + Number(category.estimated_hours || 0));
        }
      }
      const divisor = Math.max(1, valid.length);
      return totals;
    }

    function renderRecapCategories(currentCategories, previousAnalyses) {
      const previousTotals = categoryAverageFromAnalyses(previousAnalyses);
      const codes = new Set([
        ...(currentCategories || []).map(item => item.code || 'Sans categorie'),
        ...previousTotals.keys(),
      ]);
      els.recapCategories.innerHTML = '';
      if (!codes.size) {
        els.recapCategories.innerHTML = '<div class="meta">Aucune categorie disponible.</div>';
        return;
      }
      const currentByCode = new Map((currentCategories || []).map(item => [item.code || 'Sans categorie', item]));
      for (const code of Array.from(codes).sort()) {
        const current = currentByCode.get(code);
        const currentHours = Number(current?.estimated_hours || 0);
        const currentCount = Number(current?.count || 0);
        const previousAverage = Math.round((Number(previousTotals.get(code) || 0) / Math.max(1, previousAnalyses.filter(Boolean).length)) * 10) / 10;
        const row = document.createElement('div');
        row.className = 'category-stat-row';
        row.innerHTML = `
          <strong>${escapeHtml(code)}</strong>
          <span>${currentHours}h ce mois</span>
          <span>${previousAverage}h/mois avant</span>
          <span class="meta">${currentCount} action(s)</span>
        `;
        els.recapCategories.appendChild(row);
      }
    }

    function renderRecapWeeks(weeks) {
      els.recapWeeksGrid.innerHTML = '';
      for (const week of weeks || []) {
        const balance = Number(week.balance_hours || 0);
        const div = document.createElement('div');
        div.className = `week-card${balance < 0 ? ' missing' : ''}`;
        div.innerHTML = `
          <strong>${week.week_start} → ${week.week_end}</strong>
          <div class="meta" style="margin-top:6px">estime=${week.estimated_hours}/${week.target_hours}h | balance=${balance >= 0 ? '+' : ''}${balance}h</div>
          <div style="margin-top:8px">${balance >= 0 ? 'Semaine au-dessus du rythme.' : 'Semaine sous le rythme.'}</div>
        `;
        els.recapWeeksGrid.appendChild(div);
      }
    }

    async function openMonthlyRecap() {
      openModal(els.recapModal);
      setNotice(els.recapNotice, true, '');
      els.recapMessage.textContent = 'Calcul en cours...';
      els.recapWeeksGrid.innerHTML = '';

      const now = new Date();
      const monthStart = startOfMonth(now);
      const monthEnd = endOfMonth(now);
      const nextMonthStart = addMonths(monthStart, 1);
      els.recapPeriod.textContent = `Mois CEJ: ${formatDateInput(monthStart)} → ${formatDateInput(nextMonthStart)}`;

      try {
        const monthAnalysis = await fetchAnalysisRange(formatDateInput(monthStart), formatDateInput(monthEnd));
        const previousMonths = await Promise.all([1, 2, 3].map(offset => {
          const month = addMonths(monthStart, -offset);
          return fetchAnalysisRange(formatDateInput(startOfMonth(month)), formatDateInput(endOfMonth(month))).catch(() => null);
        }));

        const monthTarget = Number(monthAnalysis.period_target_hours || monthAnalysis.target_hours_total || 0);
        const remaining = Math.max(0, Math.ceil(monthTarget - Number(monthAnalysis.estimated_hours_total || 0)));
        const balance = Number(monthAnalysis.period_balance_hours ?? monthAnalysis.balance_hours_total ?? 0);
        const pastAverage = averageFromAnalyses(previousMonths);
        const completedWeeks = completedWeeksInMonth(monthAnalysis);
        const completedAverage = completedWeeks.length
          ? Math.round((completedWeeks.reduce((sum, week) => sum + Number(week.estimated_hours || 0), 0) / completedWeeks.length) * 10) / 10
          : 0;
        const currentWeek = currentWeekInAnalysis(monthAnalysis);
        const weeksLeft = (monthAnalysis.weeks || []).filter(week => week.week_end >= new Date().toISOString().slice(0, 10)).length;

        els.recapMonthAverage.textContent = `${monthAnalysis.average_hours_per_week}h/semaine`;
        els.recapMonthDetail.textContent = `${monthAnalysis.estimated_hours_total}/${monthTarget}h ce mois-ci du 1 au 1`;
        els.recapPastMonthsAverage.textContent = pastAverage ? `${pastAverage}h/semaine` : '-';
        els.recapPastMonthsDetail.textContent = 'Moyenne des 3 mois precedents disponibles';
        els.recapOtherWeeksAverage.textContent = completedWeeks.length ? `${completedAverage}h/semaine` : '-';
        els.recapOtherWeeksDetail.textContent = completedWeeks.length ? 'Semaines deja terminees ce mois-ci' : 'Pas encore de semaine terminee ce mois-ci';
        els.recapRemaining.textContent = remaining ? `${remaining}h` : '0h';
        els.recapRemainingDetail.textContent = `${weeksLeft} semaine(s) restante(s) dans ce mois CEJ`;
        renderRecapCategories(monthAnalysis.categories || [], previousMonths);

        if (balance >= 0) {
          els.recapMessage.textContent = `Continue, tu es dans les clous ce mois-ci avec ${balance >= 0 ? '+' : ''}${balance}h d'avance estimee.`;
        } else if (currentWeek && weeksLeft <= 1) {
          els.recapMessage.textContent = `Il te manque ${remaining}h pour etre dans les clous ce mois-ci. Il reste cette derniere semaine du mois pour les faire.`;
        } else {
          els.recapMessage.textContent = `Il te manque ${remaining}h pour etre dans les clous ce mois-ci. Tu peux compenser sur les ${weeksLeft} semaine(s) restante(s).`;
        }
        renderRecapWeeks(monthAnalysis.weeks || []);
      } catch (error) {
        els.recapMessage.textContent = 'Recapitulatif indisponible.';
        els.recapCategories.innerHTML = '';
        setNotice(els.recapNotice, false, error.message || 'Impossible de calculer le recapitulatif.');
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
        closeModal(els.recapModal);
        closeModal(els.createModal);
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
    els.createModal.addEventListener('click', event => {
      if (event.target === els.createModal) closeModal(els.createModal);
    });
    els.recapModal.addEventListener('click', event => {
      if (event.target === els.recapModal) closeModal(els.recapModal);
    });
    els.calendarGrid.addEventListener('click', event => {
      const retry = event.target.closest('.retry-sync');
      const cancel = event.target.closest('.cancel-sync');
      if (retry) retrySyncAction(retry.dataset.actionId);
      if (cancel) cancelSyncAction(cancel.dataset.actionId);
    });
    els.actionList.addEventListener('click', event => {
      const retry = event.target.closest('.retry-sync');
      const cancel = event.target.closest('.cancel-sync');
      if (retry) retrySyncAction(retry.dataset.actionId);
      if (cancel) cancelSyncAction(cancel.dataset.actionId);
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
    els.monthlyRecapBtn.addEventListener('click', openMonthlyRecap);
    document.getElementById('closeRecapModalBtn').addEventListener('click', () => closeModal(els.recapModal));
    document.getElementById('closeCreateModalBtn').addEventListener('click', () => closeModal(els.createModal));
    document.getElementById('cancelCreateBtn').addEventListener('click', () => closeModal(els.createModal));
    document.getElementById('saveCreateBtn').addEventListener('click', submitCreateModal);
    els.createOtherDate.addEventListener('change', () => {
      creatingDate = els.createOtherDate.value || creatingDate;
      renderCreateDateControls(creatingDate);
    });
    els.createMultiDay.addEventListener('change', () => {
      els.createMultiDays.style.display = els.createMultiDay.checked ? 'grid' : 'none';
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

    els.agendaViewBtn.addEventListener('click', () => setActiveView('agenda'));
    els.listViewBtn.addEventListener('click', () => setActiveView('list'));
    document.querySelectorAll('[data-list-filter]').forEach(button => {
      button.addEventListener('click', () => {
        activeListFilter = button.dataset.listFilter;
        document.querySelectorAll('[data-list-filter]').forEach(item => item.classList.toggle('active', item === button));
        renderActionList();
      });
    });

    document.getElementById('agentRunBtn').addEventListener('click', async () => {
      const input = document.getElementById('agentCommand');
      const message = input.value.trim();
      if (!message) return;
      addYokiMessage('user', message);
      input.value = '';
      clearAgentProposal();
      setAgentNotice(true, 'Yoki reflechit...');
      const result = await runAgentFromInput(message);
      setAgentNotice(result.ok, result.ok ? (result.message || '') : (result.error || 'Commande refusee.'));
    });

    document.getElementById('agentCommand').addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        document.getElementById('agentRunBtn').click();
      }
    });

    document.getElementById('agentConfirmBtn').addEventListener('click', async () => {
      const toolCall = pendingAgentSuggestion?.tool_call || pendingAgentSuggestion?.command;
      if (!toolCall) return;
      clearAgentProposal();
      setAgentNotice(true, 'Execution en cours...');
      const result = toolCall.name ? await executeYokiToolCall(toolCall) : await executeAgentCommand(toolCall);
      addYokiMessage('yoki', result.ok ? (result.message || 'Action executee.') : (result.error || 'Execution impossible.'));
      setAgentNotice(result.ok, result.ok ? '' : (result.error || 'Execution impossible.'));
    });

    document.getElementById('agentCancelBtn').addEventListener('click', () => {
      clearAgentProposal();
      addYokiMessage('yoki', 'Action annulee. Je ne modifie rien.');
      setAgentNotice(true, '');
    });

    document.getElementById('yokiBubbleBtn').addEventListener('click', () => {
      els.agentPanel.classList.toggle('open');
      els.agentPanel.setAttribute('aria-hidden', els.agentPanel.classList.contains('open') ? 'false' : 'true');
      renderYokiUsage();
    });
    document.getElementById('agentRawBtn').addEventListener('click', () => {
      const open = !els.agentRawOutput.classList.contains('open');
      els.agentRawOutput.classList.toggle('open', open);
      els.agentRawOutput.setAttribute('aria-hidden', open ? 'false' : 'true');
      setYokiRaw(lastYokiRaw);
    });
    document.getElementById('agentCloseBtn').addEventListener('click', () => {
      els.agentPanel.classList.remove('open');
      els.agentPanel.setAttribute('aria-hidden', 'true');
    });
    document.getElementById('agentMinimizeBtn').addEventListener('click', () => {
      els.agentPanel.classList.remove('open');
      els.agentPanel.setAttribute('aria-hidden', 'true');
    });
    document.getElementById('saveYokiSettingsBtn').addEventListener('click', () => {
      persistYokiSettingsFromControls();
      setNotice(els.yokiSettingsNotice, true, 'Parametres Yoki enregistres.');
    });
    els.yokiEnabled.addEventListener('change', () => {
      setYokiEnabled(els.yokiEnabled.checked);
      setNotice(els.yokiSettingsNotice, true, els.yokiEnabled.checked ? 'Yoki active.' : 'Yoki desactive.');
    });
    els.yokiDevMode.addEventListener('change', () => {
      setYokiDevMode(els.yokiDevMode.checked);
      setNotice(els.yokiSettingsNotice, true, els.yokiDevMode.checked ? 'Mode dev active: bouton Detail visible dans Yoki.' : 'Mode dev desactive.');
    });
    document.getElementById('clearYokiHistoryBtn').addEventListener('click', () => {
      conversationHistory = [];
      saveJsonStorage(yokiHistoryKey, conversationHistory);
      renderYokiHistory();
      setNotice(els.yokiSettingsNotice, true, 'Conversation Yoki effacee.');
    });
    document.getElementById('undoYokiBtn').addEventListener('click', async () => {
      const result = await undoLastYokiAction();
      setNotice(els.yokiSettingsNotice, result.ok, result.ok ? 'Derniere action Yoki annulee.' : (result.error || 'Annulation impossible.'));
    });

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

    loadYokiState();
    refreshYokiUsage();

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

    window.cejAgent = {
      execute: async command => {
        const result = await executeAgentCommand(command);
        setAgentNotice(result.ok, result.ok ? (result.message || 'Commande executee.') : (result.error || 'Commande refusee.'));
        return result;
      },
      state: () => ({
        weekStart: formatDateInput(selectedWeekStart),
        weekEnd: formatDateInput(addDays(selectedWeekStart, 6)),
        actions: currentActions.map(actionToClient),
      }),
      findActions: query => findActionCandidates(query).map(actionToClient),
    };
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
