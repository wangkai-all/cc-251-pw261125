"""Real web UI over the gazetteer (stdlib http.server, no frameworks).

Pages
-----
/                  dashboard: pipeline state, pending queue, releases
/sources           independent sources + collection runs (incl. failures)
/regions?date=     regions as of a business date (point-in-time view)
/region/<id>       full version history, evolution links and evidence
/releases          immutable releases and their coverage notes
/release/<id>      one release with included/excluded explanation
/pending           conflict queue with adjudication forms

Every page carries the fictional-data disclaimer and every displayed
fact links back to the raw source batch that proves it.
"""
from __future__ import annotations

import html
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import query, release as release_mod, source as source_mod
from .adjudication import (
    AdjudicationError,
    decide_link,
    decide_name_reuse,
    resolve_field_conflict,
    resolve_pending,
)
from .db import connect
from .ingest import list_runs
from .util import loads

DISCLAIMER = (
    "演示系统：全部地区、来源与文件均为虚构，仅用于说明多来源沿革对账，"
    "不冒充任何官方资料。"
)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def layout(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 沿革对账演示</title>
<style>
:root {{ --ink:#1c2330; --muted:#5d6b82; --line:#d9e0ea; --bg:#f5f7fb;
        --accent:#1f5fa8; --warn:#a25b00; --bad:#8a2b2b; --ok:#1f6b3f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",
       system-ui,sans-serif; color:var(--ink); background:var(--bg); line-height:1.55; }}
header {{ background:#122a45; color:#fff; padding:14px 28px; }}
header h1 {{ margin:0; font-size:19px; }}
header nav {{ margin-top:6px; font-size:14px; }}
header nav a {{ color:#cfe0f5; margin-right:16px; text-decoration:none; }}
header nav a:hover {{ text-decoration:underline; }}
.disclaimer {{ background:#fff4e0; color:var(--warn); border-bottom:1px solid #e8cfa2;
       padding:7px 28px; font-size:13px; }}
main {{ max-width:1080px; margin:22px auto; padding:0 20px; }}
h2 {{ font-size:17px; border-left:4px solid var(--accent); padding-left:9px; }}
table {{ border-collapse:collapse; width:100%; background:#fff; margin:10px 0 22px;
       font-size:13.5px; }}
th,td {{ border:1px solid var(--line); padding:7px 9px; text-align:left; vertical-align:top; }}
th {{ background:#eef3fa; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:8px;
       padding:14px 18px; margin-bottom:18px; }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; }}
.b-proposed {{ background:#fdeecd; color:var(--warn); }}
.b-confirmed,.b-adjudicated {{ background:#e2f3e7; color:var(--ok); }}
.b-rejected {{ background:#f7e2e2; color:var(--bad); }}
.b-failed {{ background:#f7e2e2; color:var(--bad); }}
.b-success {{ background:#e2f3e7; color:var(--ok); }}
.b-unchanged {{ background:#eceff4; color:var(--muted); }}
.muted {{ color:var(--muted); font-size:12.5px; }}
.evidence {{ font-size:12.5px; background:#f8fafc; border:1px dashed var(--line);
       padding:8px 10px; margin-top:6px; }}
form.inline {{ display:inline; }}
input[type=text],textarea {{ width:100%; padding:6px 8px; border:1px solid var(--line);
       border-radius:5px; font:inherit; }}
button {{ background:var(--accent); color:#fff; border:0; border-radius:5px;
       padding:6px 14px; cursor:pointer; font:inherit; }}
button.danger {{ background:var(--bad); }}
.flash-ok {{ background:#e2f3e7; color:var(--ok); padding:9px 14px; border-radius:6px; }}
.flash-err {{ background:#f7e2e2; color:var(--bad); padding:9px 14px; border-radius:6px; }}
code {{ background:#eef1f6; padding:1px 5px; border-radius:4px; }}
</style></head>
<body>
<header><h1>云川省行政区划沿革对账（虚构演示）</h1>
<nav><a href="/">总览</a><a href="/sources">来源与采集</a>
<a href="/regions">按日期查地区</a><a href="/pending">待决队列</a>
<a href="/releases">版本发布</a></nav></header>
<div class="disclaimer">⚠ {esc(DISCLAIMER)}</div>
<main>{body}</main>
<footer class="muted" style="text-align:center;padding:18px">
点查（point-in-time）视图按业务日期回溯，证据来自不可变原始报文；历史版本只追加、不回写。
</footer></body></html>""".encode("utf-8")


def _badge(status: str) -> str:
    return f'<span class="badge b-{esc(status)}">{esc(status)}</span>'


def _evidence_block(evidence: list[dict]) -> str:
    rows = []
    for ev in evidence:
        if ev.get("payload") is None:
            rows.append(
                f"<li>{esc(ev.get('source_name','系统扫描'))} · 批次 {esc(ev.get('batch_id'))} "
                f"· 发布于 {esc(ev.get('published_at'))}（系统派生证据）</li>"
            )
            continue
        rows.append(
            f"<li><b>{esc(ev['source_name'])}</b>（{esc(ev['source_id'])}）· "
            f"批次 <code>{esc(ev['batch_id'])}</code> · 发布于 {esc(ev['published_at'])} · "
            f"采集于 {esc(ev['ingested_at'])}<br><code>{esc(ev['payload'])}</code></li>"
        )
    return '<div class="evidence">证据链：<ul style="margin:6px 0 0;padding-left:20px">' + "".join(rows) + "</ul></div>"


class Handler(BaseHTTPRequestHandler):
    server_version = "GazetteerDemo/0.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # ---------- helpers ----------
    def _send(self, content: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, path: str):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            with connect() as conn:
                if path == "/":
                    self._send(self._dashboard(conn))
                elif path == "/sources":
                    self._send(self._sources(conn))
                elif path == "/regions":
                    self._send(self._regions(conn, qs.get("date", ["2023-06-01"])[0]))
                elif path.startswith("/region/"):
                    self._send(self._region_detail(conn, path.split("/", 2)[2]))
                elif path == "/releases":
                    self._send(self._releases(conn))
                elif path.startswith("/release/"):
                    self._send(self._release_detail(conn, int(path.rsplit("/", 1)[1])))
                elif path == "/pending":
                    self._send(self._pending(conn))
                elif path == "/healthz":
                    self._send(b'{"status":"ok"}', ctype="application/json")
                else:
                    self._send(layout("未找到", "<p>页面不存在。"), status=404)
        except Exception as exc:  # pragma: no cover - UI safety net
            self._send(layout("错误", f'<p class="flash-err">{esc(exc)}</p>'), status=500)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        form = self._form()
        try:
            with connect() as conn:
                if parsed.path == "/adjudicate/link":
                    decide_link(conn, int(form["link_id"]), form["decision"],
                                form["rationale"], form.get("decided_by", "web-operator"))
                elif parsed.path == "/adjudicate/reuse":
                    decide_name_reuse(conn, int(form["link_id"]), form["decision"],
                                      form["rationale"], form.get("decided_by", "web-operator"))
                elif parsed.path == "/adjudicate/field":
                    resolve_field_conflict(
                        conn, form["region_id"], form["effective_date"],
                        form["field_name"], form["override_value"],
                        form["rationale"], form.get("decided_by", "web-operator"),
                    )
                elif parsed.path == "/adjudicate/pending":
                    resolve_pending(
                        conn, int(form["conflict_id"]), form["resolution"],
                        form.get("decided_by", "web-operator"),
                    )
                elif parsed.path == "/release/create":
                    rel = release_mod.create_release(
                        conn, form["business_date"], form["policy"],
                        form.get("created_by", "web-operator"),
                    )
                    self._redirect(f"/release/{rel['release_id']}")
                    return
                else:
                    self._send(layout("未找到", "<p>操作不存在。"), status=404)
                    return
        except (AdjudicationError, release_mod.ReleaseError) as exc:
            self._send(layout("操作被拒绝", f'<p class="flash-err">{esc(exc)}</p>'
                                            '<p><a href="/pending">返回待决队列</a></p>'),
                       status=409)
            return
        self._redirect("/pending")

    # ---------- pages ----------
    def _dashboard(self, conn) -> bytes:
        sources = source_mod.list_sources(conn)
        pending = query.list_pending(conn)
        releases = release_mod.list_releases(conn)
        runs = list_runs(conn)[:6]
        src_rows = "".join(
            f"<tr><td>{esc(s['name'])}</td><td>{esc(s['kind'])}</td>"
            f"<td>{esc(s['record_count'])}</td><td>{esc(s['latest_published_at'] or '—')}</td>"
            f"<td>{esc(s['failed_runs'])}</td></tr>" for s in sources
        )
        run_rows = "".join(
            f"<tr><td>{esc(r['source_name'])}</td><td><code>{esc(r['file_path'])}</code></td>"
            f"<td>{_badge(r['status'])}</td><td>{esc(r['error'] or '')}</td>"
            f"<td>{esc(r['finished_at'])}</td></tr>" for r in runs
        )
        body = f"""
<h2>流水线状态</h2>
<div class="card"><table><tr><th>来源</th><th>类型</th><th>原始记录</th>
<th>最新发布时间</th><th>失败采集次数</th></tr>{src_rows}</table>
<p class="muted">三个来源独立发布、错峰到达；采集失败只记录失败批次，不影响其他来源。</p></div>
<h2>待决事项（{len(pending)}）</h2>
<div class="card"><p>{'无待决事项。' if not pending else ''}
{''.join(f'<p>{_badge(p["kind"])} {esc(p["region_id"] or "—")} @ {esc(p["effective_date"] or "—")} — {esc(p["detail"].get("reason",""))}</p>' for p in pending[:8])}
{'<p><a href="/pending">前往裁决 →</a></p>' if pending else ''}</p></div>
<h2>最近采集</h2>
<div class="card"><table><tr><th>来源</th><th>文件</th><th>状态</th><th>错误</th><th>时间</th></tr>{run_rows}</table></div>
<h2>已发布版本（{len(releases)}）</h2>
<div class="card"><ul>{''.join(f'<li><a href="/release/{r["release_id"]}">#{r["release_id"]}</a> {esc(r["business_date"])} · {esc(r["policy"])} · {esc(r["created_at"])}</li>' for r in releases) or '<li>暂无发布</li>'}</ul></div>
"""
        return layout("总览", body)

    def _sources(self, conn) -> bytes:
        runs = list_runs(conn)
        rows = "".join(
            f"<tr><td>{esc(r['source_name'])}</td><td><code>{esc(r['file_path'])}</code></td>"
            f"<td>{_badge(r['status'])}</td><td>{esc(r['records_loaded'])}</td>"
            f"<td>{esc(r['error'] or '—')}</td><td>{esc(r['started_at'])}</td>"
            f"<td><code>{esc((r['file_sha256'] or '')[:12])}</code></td></tr>"
            for r in runs
        )
        body = f"""
<h2>采集运行记录（失败可恢复）</h2>
<div class="card"><table><tr><th>来源</th><th>文件</th><th>状态</th><th>载入</th>
<th>错误信息</th><th>开始时间</th><th>报文哈希</th></tr>{rows}</table>
<p class="muted">坏文件产生 failed 记录且不写入任何原始记录；修复后重新采集即可续跑，
已成功的批次不会重复入库（按 SHA-256 与自然键去重）。</p></div>
"""
        return layout("来源与采集", body)

    def _regions(self, conn, date: str) -> bytes:
        regions = query.regions_at(conn, date)
        rows = "".join(
            f"<tr><td><a href='/region/{esc(r['region_id'])}'>{esc(r['region_id'])}</a></td>"
            f"<td>{esc(r['name'])}{' 🏳' if r['overrides'] else ''}</td>"
            f"<td>{esc(r['parent_id'] or '—')}</td>"
            f"<td>{esc(r['valid_from'])} → {esc(r['valid_to'] or '至今')}</td>"
            f"<td>{'有冲突，待/已裁决' if r['conflict'] else '一致'}</td>"
            f"<td>{len(r['sources'])} 条证据</td></tr>"
            for r in regions
        )
        body = f"""
<h2>业务日期 {esc(date)} 的地区（点查视图）</h2>
<div class="card"><form method="get" action="/regions">
<label>查询业务日期 <input type="text" name="date" value="{esc(date)}" style="width:160px">
（YYYY-MM-DD）</label>
<button type="submit">查询</button></form>
<p class="muted">返回的是该日期当天生效的历史版本，不会用当前名称替换历史；
🏳 表示该版本含人工裁决覆盖（原始证据仍保留）。</p></div>
<div class="card"><table><tr><th>地区代码</th><th>名称</th><th>上级</th>
<th>有效期</th><th>来源一致性</th><th>证据</th></tr>{rows}</table>
<p class="muted">共 {len(regions)} 个在册地区。</p></div>
"""
        return layout("按日期查地区", body)

    def _region_detail(self, conn, region_id: str) -> bytes:
        history = query.region_history(conn, region_id)
        if not history:
            return layout("未找到", f"<p>地区 {esc(region_id)} 不存在。</p>", )
        links = query.evolution_for(conn, region_id)
        ver_html = ""
        for v in history:
            conflict = (
                f'<p class="badge b-proposed">字段冲突：{esc(v["conflict"])}</p>'
                if v["conflict"] else ""
            )
            overrides = "".join(
                f'<li>{esc(d["field_name"])} 覆盖为 <b>{esc(d["override_value"])}</b>'
                f'（{esc(d["decided_by"])}：{esc(d["rationale"])}）</li>'
                for d in v["overrides"]
            )
            ver_html += f"""
<div class="card"><h3 style="margin:4px 0">{esc(v['name'])}
<span class="muted">{esc(v['valid_from'])} → {esc(v['valid_to'] or '至今')}</span></h3>
<p>上级：{esc(v['parent_id'] or '—')}</p>{conflict}
{('<ul>'+overrides+'</ul>') if overrides else ''}
{_evidence_block(query.evidence_for(conn, v['sources']))}</div>"""
        link_rows = ""
        for lk in links:
            link_rows += (
                f"<tr><td>{esc(lk['link_type'])}</td>"
                f"<td>{esc(lk['from_region_id'] or '—')} → {esc(lk['to_region_id'])}</td>"
                f"<td>{esc(lk['effective_date'])}</td><td>{_badge(lk['status'])}</td>"
                f"<td>{esc(lk['reason'] or '')}</td></tr>"
            )
        body = f"""
<h2>地区 {esc(region_id)} · {esc(history[-1]['name'])}</h2>
{ver_html}
<h2>沿革关系</h2>
<div class="card"><table><tr><th>类型</th><th>方向</th><th>生效日</th>
<th>状态</th><th>说明</th></tr>{link_rows or '<tr><td colspan=5>无</td></tr>'}</table>
<p class="muted">拆分/合并在三方来源未对齐前保持 proposed；名称复用必须人工确认，
系统不会自动合并同名地区。</p></div>
<p><a href="/regions">← 返回点查</a></p>
"""
        return layout(f"地区 {region_id}", body)

    def _pending(self, conn) -> bytes:
        pending = query.list_pending(conn)
        cards = []
        for p in pending:
            detail = p["detail"]
            forms = ""
            if p["kind"] in ("source_gap",) and detail.get("link_id"):
                forms = f"""
<form method="post" action="/adjudicate/link" class="inline">
<input type="hidden" name="link_id" value="{detail['link_id']}">
<input type="hidden" name="decision" value="confirm">
<input type="text" name="rationale" placeholder="裁决理由（必填）" required>
<button type="submit">确认关系</button></form>
<form method="post" action="/adjudicate/link" class="inline">
<input type="hidden" name="link_id" value="{detail['link_id']}">
<input type="hidden" name="decision" value="reject">
<input type="text" name="rationale" placeholder="否决理由（必填）" required>
<button class="danger" type="submit">否决关系</button></form>"""
            elif p["kind"] == "name_reuse" and detail.get("link_id"):
                forms = f"""
<form method="post" action="/adjudicate/reuse" class="inline">
<input type="hidden" name="link_id" value="{detail['link_id']}">
<input type="hidden" name="decision" value="confirm">
<input type="text" name="rationale" placeholder="确认同一沿革主体的理由" required>
<button type="submit">确认同一主体</button></form>
<form method="post" action="/adjudicate/reuse" class="inline">
<input type="hidden" name="link_id" value="{detail['link_id']}">
<input type="hidden" name="decision" value="reject">
<input type="text" name="rationale" placeholder="判定为同名不同体的理由" required>
<button class="danger" type="submit">判定同名不同体</button></form>"""
            elif p["kind"] == "field_conflict":
                kept = detail.get("kept", {})
                forms = f"""
<form method="post" action="/adjudicate/field">
<input type="hidden" name="region_id" value="{esc(p['region_id'])}">
<input type="hidden" name="effective_date" value="{esc(p['effective_date'])}">
<label>裁决字段
<select name="field_name"><option>name</option><option>parent_id</option></select></label>
<input type="text" name="override_value" value="{esc(kept.get('name',''))}" placeholder="人工认定取值" required>
<input type="text" name="rationale" placeholder="裁决理由（必填），原始证据保留不删" required>
<button type="submit">提交字段裁决</button></form>"""
            else:
                # boundary_orphan and any other generic queue item
                forms = f"""
<form method="post" action="/adjudicate/pending">
<input type="hidden" name="conflict_id" value="{p['conflict_id']}">
<input type="text" name="resolution" placeholder="人工处理结论（必填），如：边界引用有效，挂接至当日版本" required>
<button type="submit">记录人工裁决并关闭</button></form>"""
            cards.append(
                f'<div class="card">{_badge(p["kind"])} '
                f"<b>{esc(p['region_id'] or '—')}</b> @ {esc(p['effective_date'] or '—')}"
                f"<p>{esc(detail.get('reason',''))}</p>"
                f"<pre style='white-space:pre-wrap;font-size:12px'>{esc(detail)}</pre>{forms}</div>"
            )
        body = f"""
<h2>待决队列（{len(pending)}）</h2>
<p class="muted">人工裁决、持续采集、历史回补三条线并行；裁决写入后立即重算，
后续任何采集（含旧批次回补）都不能撤销已记录的裁决。</p>
{''.join(cards) or '<div class="card">没有待决事项，整批已对齐。</div>'}
<h2>发布新版本</h2>
<div class="card"><form method="post" action="/release/create">
<label>业务日期 <input type="text" name="business_date" value="2023-06-01" style="width:150px" required></label>
<label>策略 <select name="policy"><option value="early">early（先发布可信区域）</option>
<option value="aligned">aligned（等待整批对齐）</option></select></label>
<button type="submit">创建不可变发布</button></form>
<p class="muted">aligned 在有待决事项时会被拒绝；early 会暂缓未确认拆分/合并整组，
并在发布说明中列出覆盖代价与未对齐范围。任一发布都不会同时包含已确认拆分组的父与子。</p></div>
"""
        return layout("待决队列", body)

    def _releases(self, conn) -> bytes:
        rels = release_mod.list_releases(conn)
        cards = "".join(
            f'<div class="card"><h3><a href="/release/{r["release_id"]}">#{r["release_id"]}</a> '
            f"{esc(r['business_date'])} · {_badge(r['policy'])}</h3>"
            f"<p>{esc(r['note'])}</p><p class='muted'>创建于 {esc(r['created_at'])} by {esc(r['created_by'])}</p></div>"
            for r in rels
        ) or '<div class="card">暂无发布。</div>'
        return layout("版本发布", f"<h2>不可变发布历史</h2>{cards}")

    def _release_detail(self, conn, release_id: int) -> bytes:
        data = release_mod.get_release(conn, release_id)
        rel, items = data["release"], data["items"]
        rows = "".join(
            f"<tr><td>{esc(i['region_id'])}</td><td>{esc(i['name'])}</td>"
            f"<td>{esc(i['parent_id'] or '—')}</td>"
            f"<td>{esc(i['valid_from'])} → {esc(i['valid_to'] or '至今')}</td></tr>"
            for i in items
        )
        body = f"""
<h2>发布 #{rel['release_id']} · {esc(rel['business_date'])} · {_badge(rel['policy'])}</h2>
<div class="card"><h3>发布说明（覆盖代价与未对齐范围）</h3><p>{esc(rel['note'])}</p>
<p class="muted">发布后不可修改；重复创建同日期同策略发布将被拒绝。</p></div>
<div class="card"><table><tr><th>地区代码</th><th>名称</th><th>上级</th><th>所用版本有效期</th></tr>
{rows}</table><p class="muted">纳入 {len(items)} 个地区。</p></div>
<p><a href="/releases">← 返回发布列表</a></p>
"""
        return layout(f"发布 {release_id}", body)


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = make_server(host, port)
    print(f"Gazetteer demo on http://{host}:{port}/  (Ctrl-C to stop)")
    httpd.serve_forever()
