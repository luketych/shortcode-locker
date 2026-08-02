#!/usr/bin/env python3
"""Tiny 3-character code resolver for a home LAN.

Run:
  uv run shortcode-locker serve --host 0.0.0.0 --port 8765

Manage entries:
  uv run shortcode-locker add ABC https://example.com --label "Example"
  uv run shortcode-locker lookup ABC --json
  uv run shortcode-locker list --json
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import secrets
import sys
import tempfile
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

APP_NAME = "shortcode-locker"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_PATH = APP_DIR / "data" / "codes.json"

# Human-safe-ish alphabet:
# - digits avoid 0/1
# - uppercase avoids O but keeps I
# - lowercase avoids l/i/o plus letters that are especially case-similar by default (c/z)
# Edit data/config.json or set SHORTCODE_LOCKER_ALPHABET if you want a different policy.
DEFAULT_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZabdefghjmnqrtuy"
CODE_RE = re.compile(r"^[A-Za-z0-9]{3}$")
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
UNSAFE_OPEN_SCHEMES = {"javascript", "data", "vbscript"}
URL_SCHEMES = {"http", "https"}
MAX_BODY_BYTES = 64 * 1024
DATA_LOCK = threading.Lock()


@dataclass(frozen=True)
class Entry:
    code: str
    value: str
    label: str = ""
    kind: str = "auto"

    def resolved_kind(self) -> str:
        return classify_value(self.value, self.kind)

    def to_json(self) -> dict[str, Any]:
        kind = self.resolved_kind()
        parsed = urlparse(self.value)
        scheme = parsed.scheme.lower()
        openable = kind in {"url", "uri"} and scheme not in UNSAFE_OPEN_SCHEMES
        return {
            "code": self.code,
            "label": self.label,
            "value": self.value,
            "kind": kind,
            "scheme": scheme or None,
            "openable": openable,
        }


def classify_value(value: str, requested_kind: str = "auto") -> str:
    requested_kind = (requested_kind or "auto").lower()
    if requested_kind in {"url", "uri", "text"}:
        return requested_kind

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme in URL_SCHEMES and parsed.netloc:
        return "url"
    if URI_SCHEME_RE.match(value):
        return "uri"
    return "text"


def load_alphabet(data_path: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env_alphabet = os.environ.get("SHORTCODE_LOCKER_ALPHABET") or os.environ.get("CODEBOOK_ALPHABET")
    if env_alphabet:
        return env_alphabet

    config_path = data_path.parent / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            alphabet = str(config.get("alphabet", ""))
            if alphabet:
                return alphabet
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_ALPHABET


def validate_alphabet(alphabet: str) -> None:
    if not alphabet:
        raise ValueError("Alphabet must not be empty")
    duplicates = sorted({ch for ch in alphabet if alphabet.count(ch) > 1})
    if duplicates:
        raise ValueError(f"Alphabet contains duplicate characters: {''.join(duplicates)!r}")
    bad = [ch for ch in alphabet if len(ch) != 1 or not ch.isalnum()]
    if bad:
        raise ValueError("Alphabet may contain only single alphanumeric characters")


def validate_code(code: str, alphabet: str) -> str:
    if not isinstance(code, str):
        raise ValueError("Code must be a string")
    code = code.strip()
    if not CODE_RE.match(code):
        raise ValueError("Code must be exactly 3 letters/numbers")
    invalid = [ch for ch in code if ch not in alphabet]
    if invalid:
        allowed = "".join(alphabet)
        raise ValueError(
            f"Code contains disallowed character(s): {''.join(invalid)!r}. "
            f"Allowed alphabet: {allowed}"
        )
    return code


def parse_entry(code: str, raw: Any) -> Entry:
    if isinstance(raw, str):
        return Entry(code=code, value=raw)
    if isinstance(raw, dict):
        return Entry(
            code=code,
            value=str(raw.get("value", "")),
            label=str(raw.get("label", "")),
            kind=str(raw.get("kind", "auto")),
        )
    return Entry(code=code, value=str(raw))


def serialize_entry(entry: Entry) -> str | dict[str, str]:
    if not entry.label and entry.kind in {"", "auto"}:
        return entry.value
    payload: dict[str, str] = {"value": entry.value}
    if entry.label:
        payload["label"] = entry.label
    if entry.kind and entry.kind != "auto":
        payload["kind"] = entry.kind
    return payload


def load_entries(data_path: Path, alphabet: str) -> dict[str, Entry]:
    if not data_path.exists():
        return {}
    data = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{data_path} must contain a JSON object")

    entries: dict[str, Entry] = {}
    for code, raw in data.items():
        valid_code = validate_code(str(code), alphabet)
        entries[valid_code] = parse_entry(valid_code, raw)
    return entries


def save_entries(data_path: Path, entries: dict[str, Entry]) -> None:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {code: serialize_entry(entries[code]) for code in sorted(entries)}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(data_path.parent), delete=False
    ) as tmp:
        tmp.write(text)
        temp_name = tmp.name
    os.replace(temp_name, data_path)


def lookup_entry(code: str, data_path: Path, alphabet: str) -> Entry | None:
    code = validate_code(code, alphabet)
    return load_entries(data_path, alphabet).get(code)


def generate_codes(count: int, entries: dict[str, Entry], alphabet: str) -> list[str]:
    validate_alphabet(alphabet)
    max_codes = len(alphabet) ** 3
    if count < 1:
        raise ValueError("count must be >= 1")
    if len(entries) + count > max_codes:
        raise ValueError("Not enough unused codes remain")

    existing = set(entries)
    generated: list[str] = []
    while len(generated) < count:
        code = "".join(secrets.choice(alphabet) for _ in range(3))
        if code not in existing and code not in generated:
            generated.append(code)
    return generated


def normalize_kind(kind: str) -> str:
    kind = (kind or "auto").lower()
    if kind not in {"auto", "url", "uri", "text"}:
        raise ValueError("kind must be auto, url, uri, or text")
    return kind


def create_generated_entry(
    *,
    value: str,
    label: str = "",
    kind: str = "auto",
    data_path: Path,
    alphabet: str,
) -> Entry:
    value = str(value or "").strip()
    if not value:
        raise ValueError("String is required")
    label = str(label or "").strip()
    kind = normalize_kind(kind)

    with DATA_LOCK:
        entries = load_entries(data_path, alphabet)
        code = generate_codes(1, entries, alphabet)[0]
        entry = Entry(code=code, value=value, label=label, kind=kind)
        entries[code] = entry
        save_entries(data_path, entries)
        return entry


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def is_authorized(handler: BaseHTTPRequestHandler, token: str | None, query: dict[str, list[str]]) -> bool:
    if not token:
        return True
    supplied = ""
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    supplied = supplied or handler.headers.get("X-Shortcode-Locker-Token", "").strip()
    supplied = supplied or (query.get("token", [""])[0]).strip()
    return bool(supplied) and secrets.compare_digest(supplied, token)


def render_page(
    *,
    code: str = "",
    token_required: bool = False,
    authorized: bool = True,
    token_value: str = "",
    error: str = "",
    entry: Entry | None = None,
    created_entry: Entry | None = None,
    add_error: str = "",
    new_value: str = "",
    new_label: str = "",
    new_kind: str = "auto",
    alphabet: str,
) -> str:
    escaped_code = html.escape(code, quote=True)
    escaped_new_value = html.escape(new_value, quote=False)
    escaped_new_label = html.escape(new_label, quote=True)
    try:
        new_kind = normalize_kind(new_kind) if new_kind else "auto"
    except ValueError:
        new_kind = "auto"

    token_input = ""
    if token_required:
        token_input = f"""
          <label>
            Access token
            <input name="token" type="password" value="{html.escape(token_value, quote=True)}" autocomplete="current-password" />
          </label>
        """

    result_html = ""
    if error:
        result_html = f'<section class="card error"><strong>{html.escape(error)}</strong></section>'
    elif code and not authorized:
        result_html = '<section class="card error"><strong>Unauthorized.</strong> Check the access token.</section>'
    elif code and entry is None:
        result_html = f'<section class="card muted">No entry found for <code>{html.escape(code)}</code>.</section>'
    elif entry:
        result_html = render_entry(entry)

    add_result_html = ""
    if add_error:
        add_result_html = f'<section class="card error"><strong>{html.escape(add_error)}</strong></section>'
    elif created_entry:
        add_result_html = render_created_entry(created_entry)

    def selected(kind: str) -> str:
        return " selected" if new_kind == kind else ""

    sample = "".join(random.choice(alphabet) for _ in range(3)) if alphabet else "ABC"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{APP_NAME}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: start center; background: #111827; color: #f9fafb; }}
    main {{ width: min(760px, calc(100vw - 32px)); margin: 6vh 0; }}
    h1 {{ margin-bottom: .25rem; letter-spacing: -.04em; }}
    h2 {{ margin-top: 0; }}
    p {{ color: #cbd5e1; line-height: 1.5; }}
    form {{ display: grid; gap: 1rem; grid-template-columns: 1fr; margin: 1rem 0 0; }}
    label {{ display: grid; gap: .35rem; color: #cbd5e1; font-size: .95rem; }}
    input, textarea, select {{ font: inherit; font-size: 1.1rem; padding: .8rem 1rem; border-radius: .8rem; border: 1px solid #475569; background: #0f172a; color: #fff; }}
    textarea {{ min-height: 7rem; resize: vertical; }}
    input[name="code"] {{ text-transform: none; letter-spacing: .18em; font-weight: 700; font-size: 1.25rem; }}
    button, .button {{ display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: .75rem; padding: .8rem 1rem; background: #38bdf8; color: #082f49; font: inherit; font-weight: 700; text-decoration: none; cursor: pointer; }}
    .secondary {{ background: #334155; color: #f8fafc; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: .75rem; margin-top: 1rem; }}
    .card {{ border: 1px solid #334155; border-radius: 1rem; padding: 1rem; margin: 1rem 0; background: #0f172a; box-shadow: 0 20px 80px rgba(0,0,0,.25); }}
    .error {{ border-color: #f87171; background: #3b0d0d; }}
    .success {{ border-color: #34d399; background: #052e2b; }}
    .muted {{ color: #cbd5e1; }}
    .value {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: 1.1rem; }}
    .code-big {{ display: inline-block; margin: .25rem 0 .75rem; padding: .25rem .65rem; border-radius: .6rem; background: #e0f2fe; color: #082f49; font-weight: 900; font-size: clamp(2rem, 8vw, 4rem); letter-spacing: .16em; }}
    code {{ background: #1f2937; padding: .1rem .35rem; border-radius: .35rem; }}
    .success code {{ background: #064e3b; }}
    .kind {{ display: inline-block; margin-bottom: .75rem; color: #93c5fd; text-transform: uppercase; letter-spacing: .08em; font-size: .8rem; font-weight: 800; }}
    .hint {{ font-size: .9rem; color: #94a3b8; }}
  </style>
</head>
<body>
  <main>
    <h1>{APP_NAME}</h1>
    <p>Enter a 3-character code to resolve it, or save a new URL/URI/note and get a generated code back.</p>

    <section class="card">
      <h2>Look up a code</h2>
      <form method="get" action="/">
        <label>
          Code
          <input name="code" value="{escaped_code}" maxlength="3" minlength="3" pattern="[A-Za-z0-9]{{3}}" placeholder="{html.escape(sample)}" autofocus />
        </label>
        {token_input}
        <button type="submit">Look up</button>
      </form>
    </section>
    {result_html}

    <section class="card">
      <h2>Add a new string</h2>
      <p>Paste a URL, URI, file/folder URI, or apartment note. {APP_NAME} will generate a 3-character code for it.</p>
      {add_result_html}
      <form method="post" action="/add">
        <label>
          String to store
          <textarea name="value" required placeholder="https://example.com or Spare batteries are in the top-left drawer">{escaped_new_value}</textarea>
        </label>
        <label>
          Optional label
          <input name="label" value="{escaped_new_label}" placeholder="Example website, Manuals folder, Battery stash" />
        </label>
        <label>
          Kind
          <select name="kind">
            <option value="auto"{selected("auto")}>Auto-detect</option>
            <option value="url"{selected("url")}>URL</option>
            <option value="uri"{selected("uri")}>URI</option>
            <option value="text"{selected("text")}>Plain text</option>
          </select>
        </label>
        {token_input}
        <button type="submit">Save and generate code</button>
      </form>
    </section>

    <p class="hint">Allowed alphabet is configurable. This server never lists all codes from the web UI.</p>
  </main>
  <script>
    async function copyValue(value) {{
      try {{
        await navigator.clipboard.writeText(value);
        toast('Copied');
      }} catch (err) {{
        const box = document.createElement('textarea');
        box.value = value;
        document.body.appendChild(box);
        box.select();
        document.execCommand('copy');
        box.remove();
        toast('Copied');
      }}
    }}
    function toast(message) {{
      const existing = document.querySelector('.toast');
      if (existing) existing.remove();
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = message;
      el.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#22c55e;color:#052e16;padding:.7rem 1rem;border-radius:.75rem;font-weight:800;';
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 1500);
    }}
  </script>
</body>
</html>"""


def render_created_entry(entry: Entry) -> str:
    payload = entry.to_json()
    code = payload["code"]
    value = payload["value"]
    kind = payload["kind"]
    label = payload["label"] or "New string"
    code_for_attr = html.escape(code, quote=True)
    value_for_attr = html.escape(value, quote=True)
    lookup_href = f"/?code={quote(code)}"

    return f"""
<section class="card success">
  <span class="kind">saved</span>
  <h2>{html.escape(label)} saved. Your code is:</h2>
  <div class="code-big">{html.escape(code)}</div>
  <div class="value">{html.escape(value)}</div>
  <p class="hint">Detected kind: <strong>{html.escape(kind)}</strong></p>
  <div class="actions">
    <button type="button" data-copy="{code_for_attr}" onclick="copyValue(this.dataset.copy)">Copy code</button>
    <button class="secondary" type="button" data-copy="{value_for_attr}" onclick="copyValue(this.dataset.copy)">Copy string</button>
    <a class="button secondary" href="{lookup_href}">Look up this code</a>
  </div>
</section>"""


def render_entry(entry: Entry) -> str:
    payload = entry.to_json()
    value = payload["value"]
    kind = payload["kind"]
    label = payload["label"] or entry.code
    value_for_attr = html.escape(value, quote=True)

    open_button = ""
    if payload["openable"]:
        href = html.escape(value, quote=True)
        if kind == "url":
            open_button = f'<a class="button" href="{href}" target="_blank" rel="noreferrer noopener">Open URL</a>'
        elif kind == "uri":
            open_button = f'<a class="button" href="{href}">Try opening URI</a>'

    caution = ""
    if kind == "uri":
        caution = '<p class="hint">Browsers may block some URI schemes such as <code>file://</code>. If opening fails, copy it.</p>'
    elif kind == "text":
        caution = '<p class="hint">Plain text result. Copy it if useful.</p>'

    return f"""
<section class="card">
  <span class="kind">{html.escape(kind)}</span>
  <h2>{html.escape(label)}</h2>
  <div class="value">{html.escape(value)}</div>
  <div class="actions">
    {open_button}
    <button class="secondary" type="button" data-copy="{value_for_attr}" onclick="copyValue(this.dataset.copy)">Copy</button>
  </div>
  {caution}
</section>"""


def make_handler(data_path: Path, alphabet: str, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/1.0"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if parsed.path == "/healthz":
                json_response(self, HTTPStatus.OK, {"ok": True, "app": APP_NAME})
                return

            if parsed.path == "/api/lookup":
                self.handle_api_lookup(query)
                return

            if parsed.path.startswith("/api/lookup/"):
                code = unquote(parsed.path.rsplit("/", 1)[-1])
                self.handle_api_lookup({**query, "code": [code]})
                return

            if parsed.path not in {"/", ""}:
                text_response(self, HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")
                return

            code = (query.get("code", [""])[0] or "").strip()
            supplied_token = (query.get("token", [""])[0] or "").strip()
            authorized = is_authorized(self, token, query)
            error = ""
            entry = None
            if code and authorized:
                try:
                    entry = lookup_entry(code, data_path, alphabet)
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    error = str(exc)

            text_response(
                self,
                HTTPStatus.OK,
                render_page(
                    code=code,
                    token_required=bool(token),
                    authorized=authorized,
                    token_value=supplied_token,
                    error=error,
                    entry=entry,
                    alphabet=alphabet,
                ),
                "text/html; charset=utf-8",
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            parsed = urlparse(self.path)
            if parsed.path not in {"/add", "/api/add"}:
                text_response(self, HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")
                return

            try:
                params = {**parse_qs(parsed.query), **self.read_body_params()}
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                if parsed.path == "/api/add":
                    json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                else:
                    text_response(
                        self,
                        HTTPStatus.BAD_REQUEST,
                        render_page(
                            token_required=bool(token),
                            add_error=str(exc),
                            alphabet=alphabet,
                        ),
                        "text/html; charset=utf-8",
                    )
                return

            if parsed.path == "/api/add":
                self.handle_api_add(params)
                return
            self.handle_web_add(params)

        def read_body_params(self) -> dict[str, list[str]]:
            raw_length = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length > MAX_BODY_BYTES:
                raise ValueError(f"Request body is too large; max is {MAX_BODY_BYTES} bytes")

            body = self.rfile.read(length).decode("utf-8") if length else ""
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/json":
                data = json.loads(body or "{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON body must be an object")
                params: dict[str, list[str]] = {}
                for key, value in data.items():
                    if isinstance(value, list):
                        params[str(key)] = ["" if item is None else str(item) for item in value]
                    else:
                        params[str(key)] = ["" if value is None else str(value)]
                return params

            return parse_qs(body, keep_blank_values=True)

        def handle_web_add(self, params: dict[str, list[str]]) -> None:
            supplied_token = (params.get("token", [""])[0] or "").strip()
            value = params.get("value", [""])[0] or ""
            label = params.get("label", [""])[0] or ""
            kind = params.get("kind", ["auto"])[0] or "auto"
            created_entry = None
            add_error = ""

            if not is_authorized(self, token, params):
                add_error = "Unauthorized. Check the access token."
            else:
                try:
                    created_entry = create_generated_entry(
                        value=value,
                        label=label,
                        kind=kind,
                        data_path=data_path,
                        alphabet=alphabet,
                    )
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    add_error = str(exc)

            text_response(
                self,
                HTTPStatus.OK,
                render_page(
                    token_required=bool(token),
                    token_value=supplied_token,
                    created_entry=created_entry,
                    add_error=add_error,
                    new_value="" if created_entry else value,
                    new_label="" if created_entry else label,
                    new_kind="auto" if created_entry else kind,
                    alphabet=alphabet,
                ),
                "text/html; charset=utf-8",
            )

        def handle_api_add(self, params: dict[str, list[str]]) -> None:
            if not is_authorized(self, token, params):
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                entry = create_generated_entry(
                    value=params.get("value", [""])[0] or "",
                    label=params.get("label", [""])[0] or "",
                    kind=params.get("kind", ["auto"])[0] or "auto",
                    data_path=data_path,
                    alphabet=alphabet,
                )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            json_response(self, HTTPStatus.CREATED, entry.to_json())

        def handle_api_lookup(self, query: dict[str, list[str]]) -> None:
            if not is_authorized(self, token, query):
                json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            code = (query.get("code", [""])[0] or "").strip()
            if not code:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "missing code"})
                return
            try:
                entry = lookup_entry(code, data_path, alphabet)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if entry is None:
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found", "code": code})
                return
            json_response(self, HTTPStatus.OK, entry.to_json())

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    return Handler


def command_serve(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    # Validate data once at startup so config mistakes fail fast.
    load_entries(data_path, alphabet)

    token = args.token or os.environ.get("SHORTCODE_LOCKER_TOKEN") or os.environ.get("CODEBOOK_TOKEN") or None
    handler = make_handler(data_path, alphabet, token)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {APP_NAME} on http://{args.host}:{args.port}")
    print(f"Data file: {data_path}")
    print("Access token: required" if token else "Access token: not set (LAN-only/private network recommended)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        server.server_close()
    return 0


def command_lookup(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    entry = lookup_entry(args.code, data_path, alphabet)
    if entry is None:
        if args.json:
            print(json.dumps({"error": "not found", "code": args.code}, indent=2))
        else:
            print(f"No entry found for {args.code}")
        return 1
    if args.json:
        print(json.dumps(entry.to_json(), indent=2, ensure_ascii=False))
    else:
        payload = entry.to_json()
        label = f" ({payload['label']})" if payload["label"] else ""
        print(f"{payload['code']}{label}: {payload['value']} [{payload['kind']}]")
    return 0


def command_add(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    code = validate_code(args.code, alphabet)
    entries = load_entries(data_path, alphabet)
    if code in entries and not args.force:
        raise SystemExit(f"{code} already exists; use --force to replace")
    kind = normalize_kind(args.kind)
    entries[code] = Entry(code=code, value=args.value, label=args.label or "", kind=kind)
    save_entries(data_path, entries)
    print(json.dumps(entries[code].to_json(), indent=2, ensure_ascii=False))
    return 0


def command_remove(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    code = validate_code(args.code, alphabet)
    entries = load_entries(data_path, alphabet)
    if code not in entries:
        raise SystemExit(f"{code} does not exist")
    removed = entries.pop(code)
    save_entries(data_path, entries)
    print(json.dumps({"removed": removed.to_json()}, indent=2, ensure_ascii=False))
    return 0


def command_list(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    entries = load_entries(data_path, alphabet)
    payload = [entries[code].to_json() for code in sorted(entries)]
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for item in payload:
            label = f" ({item['label']})" if item["label"] else ""
            print(f"{item['code']}{label}: {item['value']} [{item['kind']}]")
    return 0


def command_generate(args: argparse.Namespace) -> int:
    data_path = Path(args.data).expanduser().resolve()
    alphabet = load_alphabet(data_path, args.alphabet)
    validate_alphabet(alphabet)
    entries = load_entries(data_path, alphabet)
    codes = generate_codes(args.count, entries, alphabet)
    if args.json:
        print(json.dumps({"codes": codes, "alphabet_size": len(alphabet)}, indent=2))
    else:
        for code in codes:
            print(code)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tiny 3-character home code resolver")
    parser.add_argument(
        "--data",
        default=os.environ.get("SHORTCODE_LOCKER_DATA") or os.environ.get("CODEBOOK_DATA", str(DEFAULT_DATA_PATH)),
        help="Path to codes.json (default: %(default)s or SHORTCODE_LOCKER_DATA)",
    )
    parser.add_argument(
        "--alphabet",
        default=None,
        help="Override allowed characters (default: data/config.json, SHORTCODE_LOCKER_ALPHABET, then built-in)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run web server")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    serve.add_argument("--token", default=None, help="Optional access token (or SHORTCODE_LOCKER_TOKEN env var)")
    serve.set_defaults(func=command_serve)

    lookup = sub.add_parser("lookup", help="Look up one code")
    lookup.add_argument("code")
    lookup.add_argument("--json", action="store_true")
    lookup.set_defaults(func=command_lookup)

    add = sub.add_parser("add", help="Add or replace an entry")
    add.add_argument("code")
    add.add_argument("value")
    add.add_argument("--label", default="")
    add.add_argument("--kind", default="auto", choices=["auto", "url", "uri", "text"])
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=command_add)

    remove = sub.add_parser("remove", help="Remove an entry")
    remove.add_argument("code")
    remove.set_defaults(func=command_remove)

    list_cmd = sub.add_parser("list", help="List entries on the command line")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=command_list)

    gen = sub.add_parser("generate", help="Generate unused random code(s)")
    gen.add_argument("--count", type=int, default=1)
    gen.add_argument("--json", action="store_true")
    gen.set_defaults(func=command_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 1
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
