"""Shared escaped HTML shell, navigation, tables, forms, and CSRF injection."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from crypto_threshold.dashboard.csrf import inject_csrf_into_post_forms
from crypto_threshold.dashboard_ui.i18n import t


class SafeHtml(str):
    """HTML fragment whose dynamic parts were escaped by a local helper."""


def render_page(title: str, body: str, lang: str, current_path: str) -> str:
    body = inject_csrf_into_post_forms(body)
    links = (
        ("/", "nav.overview"),
        ("/markets", "nav.markets"),
        ("/calibration", "nav.calibration"),
        ("/paper", "nav.paper"),
        ("/shadow", "nav.shadow"),
        ("/readiness", "nav.readiness"),
        ("/setup/wallet", "nav.wallet"),
    )
    nav = "".join(
        f'<a href="{href(path, lang)}">{e(t(lang, label))}</a>' for path, label in links
    )
    language = (
        f'<a href="{href(current_path, "zh")}">{e(t(lang, "language.zh"))}</a>'
        " / "
        f'<a href="{href(current_path, "en")}">{e(t(lang, "language.en"))}</a>'
    )
    return f"""<!doctype html>
<html lang="{"zh-CN" if lang == "zh" else "en"}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#07111f; --panel:#101d2e; --line:#26384d;
      --text:#e6eef8; --muted:#93a4b8; --cyan:#45d7dc; --blue:#7db7ff;
      --green:#72e09a; --red:#ff9a9a; --amber:#ffd27d; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(145deg,#07111f,#0b1728 55%,#07111f);
      color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    header {{ position:sticky; top:0; z-index:3; padding:16px 24px; background:rgba(7,17,31,.94);
      border-bottom:1px solid var(--line); backdrop-filter:blur(14px); }}
    .brand {{ display:flex; justify-content:space-between; gap:16px; align-items:center; }}
    h1 {{ margin:0; font-size:20px; }}
    nav {{ margin-top:12px; display:flex; gap:14px; flex-wrap:wrap; }}
    a {{ color:var(--blue); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    main {{ width:min(1280px,calc(100% - 32px)); margin:24px auto 64px; }}
    h2 {{ margin:0 0 6px; font-size:26px; }} h3 {{ margin:0 0 12px; }}
    .lede,.muted {{ color:var(--muted); }} .eyebrow {{ color:var(--cyan); font-weight:700;
      text-transform:uppercase; letter-spacing:.08em; font-size:12px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }}
    .card {{ background:rgba(16,29,46,.92); border:1px solid var(--line); border-radius:14px;
      padding:16px; margin:16px 0; overflow-x:auto; box-shadow:0 18px 50px rgba(0,0,0,.16); }}
    .stat {{ font-size:28px; font-weight:750; color:var(--cyan); }}
    .badge {{ display:inline-flex; padding:3px 8px; border:1px solid var(--line);
      border-radius:999px; font-size:12px; }} .ok {{ color:var(--green); }}
    .warning {{ color:var(--amber); }} .danger {{ color:var(--red); }}
    table {{ width:100%; border-collapse:collapse; min-width:720px; }}
    th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left;
      vertical-align:top; overflow-wrap:anywhere; }} th {{ color:#c8d7e9; }}
    code,pre,.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
    pre {{ white-space:pre-wrap; background:#06101d; border:1px solid var(--line);
      border-radius:10px; padding:12px; }}
    form {{ display:grid; gap:12px; max-width:760px; }}
    label {{ display:grid; gap:6px; }} input {{ width:100%; padding:10px; color:var(--text);
      background:#06101d; border:1px solid #40546c; border-radius:8px; }}
    input[type=checkbox] {{ width:auto; }} .check {{ display:flex; align-items:center; gap:8px; }}
    button {{ width:max-content; border:0; border-radius:8px; padding:10px 15px;
      background:#1fa7ad; color:#021619; font-weight:750; cursor:pointer; }}
    .flash {{ padding:11px 13px; border:1px solid #2f7c62; background:#0b362a;
      border-radius:10px; margin-bottom:16px; }}
    .flash.error {{ border-color:#9c4b4b; background:#3a171d; }}
    .no-go {{ border-color:#7d4a4a; background:rgba(60,20,27,.65); }}
    @media (max-width:700px) {{ header {{ padding:14px 16px; }} .brand {{ align-items:flex-start; }}
      main {{ width:min(100% - 20px,1280px); }} }}
  </style>
</head>
<body>
  <header><div class="brand"><h1>{e(t(lang, "app.title"))}</h1><span>{language}</span></div>
    <nav>{nav}</nav></header>
  <main>{body}</main>
</body>
</html>"""


def section(title: str, body: str, *, css: str = "") -> str:
    class_name = f"card {css}".strip()
    return f'<section class="{e(class_name)}"><h3>{e(title)}</h3>{body}</section>'


def table(headers: list[str], rows: list[list[Any]], lang: str) -> str:
    if not rows:
        return f'<p class="muted">{e(t(lang, "common.no_rows"))}</p>'
    head = "".join(f"<th>{e(value)}</th>" for value in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{_render_cell(cell)}</td>"
            for cell in row
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def definition_table(rows: list[tuple[str, Any]]) -> str:
    body = "".join(
        f"<tr><th>{e(label)}</th><td>{e(value if value not in (None, '') else '-')}</td></tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def flash(query: dict[str, list[str]], lang: str) -> str:
    key = single(query, "flash")
    if not key:
        return ""
    css = "flash error" if single(query, "level") == "error" else "flash"
    detail = single(query, "detail")
    message = t(lang, key)
    if detail:
        message = f"{message}: {detail}"
    return f'<div class="{css}">{e(message)}</div>'


def href(path: str, lang: str, **params: object) -> str:
    parsed = urlparse(path)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
    query["lang"] = lang
    query.update({key: str(value) for key, value in params.items() if value is not None})
    encoded = urlencode(query)
    return f"{parsed.path or '/'}?{encoded}" if encoded else (parsed.path or "/")


def single(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    value = values[-1].strip() if values else ""
    return value or None


def e(value: object) -> str:
    return escape(str(value), quote=True)


def link(path: str, label: object, lang: str) -> SafeHtml:
    return SafeHtml(f'<a href="{e(href(path, lang))}">{e(label)}</a>')


def _render_cell(cell: Any) -> str:
    if isinstance(cell, SafeHtml):
        return str(cell)
    return e(cell if cell not in (None, "") else "-")
