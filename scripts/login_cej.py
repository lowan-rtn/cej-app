#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error, parse, request
import http.cookiejar


DEFAULT_ISSUER = "https://id.pass-emploi.beta.gouv.fr/auth/realms/pass-emploi"
DEFAULT_CLIENT_ID = "pass-emploi-app"
DEFAULT_CLIENT_SECRET = "6541e558-3036-424f-bf7b-5de37d6106f5"
DEFAULT_REDIRECT_URI = "fr.fabrique.social.gouv.passemploi://login-callback"
DEFAULT_SCOPE = "openid profile email"
DEFAULT_SESSION_FILE = Path.home() / ".config" / "pass_emploi" / "session.json"

MODE_PARAMS = {
    "generic": {},
    "similo": {"kc_idp_hint": "similo-jeune"},
    "pole-emploi": {"kc_idp_hint": "ft-beneficiaire"},
}


@dataclass
class HtmlForm:
    action: str
    method: str
    inputs: list[dict[str, str]]


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[HtmlForm] = []
        self._current_form: HtmlForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._current_form = HtmlForm(
                action=attributes.get("action", ""),
                method=attributes.get("method", "GET").upper(),
                inputs=[],
            )
            self.forms.append(self._current_form)
            return

        if tag == "input" and self._current_form is not None:
            self._current_form.inputs.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current_form = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log in to Pass Emploi / CEJ and persist OIDC tokens.")
    parser.add_argument("--username", default=os.getenv("PE_USERNAME"), help="Login username. Defaults to PE_USERNAME.")
    parser.add_argument("--password", default=os.getenv("PE_PASSWORD"), help="Login password. Defaults to PE_PASSWORD.")
    parser.add_argument(
        "--mode",
        choices=tuple(MODE_PARAMS),
        default="similo",
        help="Identity provider hint. 'similo' matches the CEJ i-Milo flow.",
    )
    parser.add_argument("--issuer", default=DEFAULT_ISSUER, help="OIDC issuer")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="OIDC client id")
    parser.add_argument("--client-secret", default=DEFAULT_CLIENT_SECRET, help="OIDC client secret")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI, help="OIDC redirect URI")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help="OIDC scopes")
    parser.add_argument(
        "--session-file",
        default=str(DEFAULT_SESSION_FILE),
        help=f"Where to persist tokens. Defaults to {DEFAULT_SESSION_FILE}",
    )
    parser.add_argument("--refresh-only", action="store_true", help="Refresh the stored session without re-running login.")
    parser.add_argument("--force-login", action="store_true", help="Ignore stored refresh token and force a new interactive login.")
    parser.add_argument("--json", action="store_true", help="Print raw session JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_file = Path(args.session_file).expanduser()

    try:
        well_known = get_json(f"{args.issuer.rstrip('/')}/.well-known/openid-configuration")
        if not isinstance(well_known, dict):
            raise RuntimeError("OIDC discovery returned an unexpected payload.")

        stored_session = load_session(session_file)
        if args.refresh_only:
            if stored_session is None:
                raise RuntimeError(f"No stored session found at {session_file}")
            session = refresh_session(
                token_endpoint=well_known["token_endpoint"],
                client_id=args.client_id,
                client_secret=args.client_secret,
                redirect_uri=args.redirect_uri,
                refresh_token=stored_session["refresh_token"],
            )
            persist_session(session_file, session)
            emit_session(session, args.json)
            return 0

        if stored_session is not None and not args.force_login:
            try:
                session = refresh_session(
                    token_endpoint=well_known["token_endpoint"],
                    client_id=args.client_id,
                    client_secret=args.client_secret,
                    redirect_uri=args.redirect_uri,
                    refresh_token=stored_session["refresh_token"],
                )
                persist_session(session_file, session)
                emit_session(session, args.json)
                return 0
            except RuntimeError:
                pass

        if not args.username or not args.password:
            raise RuntimeError("Username and password are required. Use --username/--password or PE_USERNAME/PE_PASSWORD.")

        session = interactive_login(
            authorization_endpoint=well_known["authorization_endpoint"],
            token_endpoint=well_known["token_endpoint"],
            client_id=args.client_id,
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            scope=args.scope,
            mode=args.mode,
            username=args.username,
            password=args.password,
        )
        persist_session(session_file, session)
        emit_session(session, args.json)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def interactive_login(
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scope: str,
    mode: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    state = random_urlsafe(24)
    nonce = random_urlsafe(24)
    code_verifier = random_urlsafe(64)
    code_challenge = pkce_challenge(code_verifier)

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    query.update(MODE_PARAMS[mode])
    authorize_url = f"{authorization_endpoint}?{parse.urlencode(query)}"

    jar = http.cookiejar.CookieJar()
    opener = request.build_opener(
        request.HTTPCookieProcessor(jar),
        request.HTTPRedirectHandler(),
    )

    response = fetch(opener, authorize_url)
    code = follow_authentication_flow(
        opener=opener,
        first_response=response,
        redirect_uri=redirect_uri,
        username=username,
        password=password,
    )
    session = exchange_code(
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=code,
        code_verifier=code_verifier,
    )
    session["login_mode"] = mode
    return session


def follow_authentication_flow(
    opener: request.OpenerDirector,
    first_response,
    redirect_uri: str,
    username: str,
    password: str,
) -> str:
    response = first_response
    credentials_sent = False

    for _ in range(12):
        current_url = response.geturl()
        code = extract_code_from_redirect(current_url, redirect_uri)
        if code:
            return code

        html = response.read().decode("utf-8", errors="replace")
        form = find_best_form(html)
        if form is None:
            raise RuntimeError(f"Unable to find a login form on {current_url}")

        action_url = parse.urljoin(current_url, form.action)
        payload, consumed_credentials = build_form_payload(form, username, password, credentials_sent)
        credentials_sent = credentials_sent or consumed_credentials

        encoded = parse.urlencode(payload).encode("utf-8")
        req = request.Request(
            action_url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method=form.method,
        )
        try:
            response = opener.open(req)
        except error.HTTPError as exc:
            redirect_code = extract_code_from_http_redirect(exc, redirect_uri)
            if redirect_code:
                return redirect_code
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} while submitting {action_url}: {body}") from exc

    raise RuntimeError("Authentication flow exceeded the supported number of steps.")


def build_form_payload(
    form: HtmlForm,
    username: str,
    password: str,
    credentials_already_sent: bool,
) -> tuple[dict[str, str], bool]:
    payload: dict[str, str] = {}
    password_field = ""
    username_field = ""

    for field in form.inputs:
        name = field.get("name", "")
        if not name:
            continue

        field_type = field.get("type", "text").lower()
        value = field.get("value", "")
        payload[name] = value

        if field_type == "password":
            password_field = name
        elif field_type in ("text", "email") and not username_field:
            username_field = name
        elif name.lower() in ("username", "email", "login", "identifiant") and not username_field:
            username_field = name

    consumed_credentials = False
    if password_field:
        if credentials_already_sent:
            raise RuntimeError("The identity provider requested credentials twice; aborting to avoid looping.")
        if not username_field:
            raise RuntimeError("Unable to identify the username field in the login form.")
        payload[username_field] = username
        payload[password_field] = password
        consumed_credentials = True

    return payload, consumed_credentials


def find_best_form(html: str) -> HtmlForm | None:
    parser = FormParser()
    parser.feed(html)
    if not parser.forms:
        return None

    password_forms = [form for form in parser.forms if any(i.get("type", "").lower() == "password" for i in form.inputs)]
    if password_forms:
        return password_forms[0]
    return parser.forms[0]


def exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    return token_request(token_endpoint, client_id, client_secret, body)


def refresh_session(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    refresh_token: str,
) -> dict[str, Any]:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": redirect_uri,
    }
    return token_request(token_endpoint, client_id, client_secret, body)


def token_request(
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    body: dict[str, str],
) -> dict[str, Any]:
    encoded = parse.urlencode(body).encode("utf-8")
    req = request.Request(
        token_endpoint,
        data=encoded,
        headers={
            "Authorization": "Basic " + basic_auth(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token request failed with HTTP {exc.code}: {body_text}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error during token request: {exc.reason}") from exc

    if not isinstance(payload, dict) or "access_token" not in payload:
        raise RuntimeError("Token endpoint returned an unexpected payload.")

    session = {
        "access_token": payload["access_token"],
        "id_token": payload.get("id_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
    }

    access_claims = decode_jwt_claims(session["access_token"])
    id_claims = decode_jwt_claims(session["id_token"]) if session.get("id_token") else {}
    session["user_id"] = access_claims.get("userId") or id_claims.get("userId")
    if "exp" in access_claims:
        session["access_token_expires_at"] = iso_from_epoch(access_claims["exp"])
    return session


def get_json(url: str) -> Any:
    try:
        with request.urlopen(url) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def fetch(opener: request.OpenerDirector, url: str):
    req = request.Request(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    try:
        return opener.open(req)
    except error.HTTPError as exc:
        redirect_code = extract_code_from_http_redirect(exc, DEFAULT_REDIRECT_URI)
        if redirect_code:
            raise RuntimeError("Unexpected direct redirect to callback before login form.")
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} while opening {url}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Network error while opening {url}: {exc.reason}") from exc


def extract_code_from_redirect(url: str, redirect_uri: str) -> str | None:
    if not url.startswith(redirect_uri):
        return None
    parsed = parse.urlparse(url)
    values = parse.parse_qs(parsed.query)
    code = values.get("code")
    if code:
        return code[0]
    error_value = values.get("error")
    if error_value:
        description = values.get("error_description", [""])[0]
        raise RuntimeError(f"OIDC provider returned {error_value[0]} {description}".strip())
    return None


def extract_code_from_http_redirect(exc: error.HTTPError, redirect_uri: str) -> str | None:
    if exc.code not in (301, 302, 303, 307, 308):
        return None
    location = exc.headers.get("Location", "")
    if not location:
        return None
    return extract_code_from_redirect(location, redirect_uri)


def load_session(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def persist_session(path: Path, session: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=True, indent=2) + "\n")
    os.chmod(path, 0o600)


def emit_session(session: dict[str, Any], raw_json: bool) -> None:
    if raw_json:
        print(json.dumps(session, ensure_ascii=True, indent=2))
        return

    expires_at = session.get("access_token_expires_at", "?")
    user_id = session.get("user_id", "?")
    print(f"user_id={user_id}")
    print(f"expires_at={expires_at}")
    print("session stored successfully")


def decode_jwt_claims(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def iso_from_epoch(value: Any) -> str:
    dt = datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone()
    return dt.isoformat()


def basic_auth(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def random_urlsafe(size: int) -> str:
    return secrets.token_urlsafe(size).rstrip("=")


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


if __name__ == "__main__":
    raise SystemExit(main())
