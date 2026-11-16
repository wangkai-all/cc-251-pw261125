"""Command-line entry point: python -m gazetteer.cli <command>."""
from __future__ import annotations

import argparse
import json

from . import config
from .adjudication import decide_link, decide_name_reuse, resolve_field_conflict, resolve_pending
from .build import build
from .db import connect, init_db
from .ingest import ingest_all, ingest_file, list_runs
from .query import list_links, list_pending, region_at, region_history, regions_at
from .release import ReleaseError, create_release, get_release, list_releases
from .source import list_sources, register_defaults
from .web import serve


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=dict))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gazetteer",
        description="虚构多来源行政区划沿革对账演示（非官方资料）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库与三个来源")

    p_ingest = sub.add_parser("ingest", help="采集全部来源（坏文件只记失败）")
    p_ingest.add_argument("--file", help="只采集单个文件，格式 source_id:/path/to/file.json")

    sub.add_parser("build", help="从原始证据重建版本/沿革/待决队列")

    p_lookup = sub.add_parser("lookup", help="按业务日期点查地区")
    p_lookup.add_argument("region_id")
    p_lookup.add_argument("date")

    p_list = sub.add_parser("list-regions", help="列出某业务日期在册地区")
    p_list.add_argument("date")

    sub.add_parser("history", help="查看某地区全部历史版本").add_argument("region_id")
    sub.add_parser("pending", help="列出待决冲突")
    sub.add_parser("links", help="列出沿革关系")
    sub.add_parser("runs", help="列出采集运行（含失败）")
    sub.add_parser("sources", help="列来源状态")

    p_rel = sub.add_parser("release", help="创建不可变发布")
    p_rel.add_argument("date")
    p_rel.add_argument("policy", choices=["early", "aligned"])

    p_show_rel = sub.add_parser("show-release", help="查看发布内容与说明")
    p_show_rel.add_argument("release_id", type=int)

    p_dec = sub.add_parser("decide-link", help="裁决沿革关系 confirm/reject")
    p_dec.add_argument("link_id", type=int)
    p_dec.add_argument("decision", choices=["confirm", "reject"])
    p_dec.add_argument("rationale")

    p_field = sub.add_parser("decide-field", help="裁决字段冲突（人工覆盖取值）")
    p_field.add_argument("region_id")
    p_field.add_argument("effective_date")
    p_field.add_argument("field_name")
    p_field.add_argument("override_value")
    p_field.add_argument("rationale")

    p_reuse = sub.add_parser("decide-reuse", help="裁决名称复用 confirm/reject")
    p_reuse.add_argument("link_id", type=int)
    p_reuse.add_argument("decision", choices=["confirm", "reject"])
    p_reuse.add_argument("rationale")

    p_pend = sub.add_parser("resolve-pending", help="裁决通用待决项（如边界孤儿）")
    p_pend.add_argument("conflict_id", type=int)
    p_pend.add_argument("resolution")

    p_web = sub.add_parser("web", help="启动网页")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    conn = connect()

    if args.cmd == "init":
        init_db(conn)
        register_defaults(conn)
        print(f"initialised {config.DB_PATH}")
    elif args.cmd == "ingest":
        init_db(conn)
        register_defaults(conn)
        if args.file:
            source_id, path = args.file.split(":", 1)
            _print(ingest_file(conn, source_id, path))
        else:
            _print(ingest_all(conn))
    elif args.cmd == "build":
        _print(build(conn))
    elif args.cmd == "lookup":
        result = region_at(conn, args.region_id, args.date)
        _print(result or {"error": "no version on that date"})
    elif args.cmd == "list-regions":
        _print(regions_at(conn, args.date))
    elif args.cmd == "history":
        _print(region_history(conn, args.region_id))
    elif args.cmd == "pending":
        _print(list_pending(conn))
    elif args.cmd == "links":
        _print(list_links(conn))
    elif args.cmd == "runs":
        _print([dict(r) for r in list_runs(conn)])
    elif args.cmd == "sources":
        _print([dict(r) for r in list_sources(conn)])
    elif args.cmd == "release":
        try:
            _print(create_release(conn, args.date, args.policy))
        except ReleaseError as exc:
            print(f"RELEASE BLOCKED: {exc}")
            return 2
    elif args.cmd == "show-release":
        _print(get_release(conn, args.release_id))
    elif args.cmd == "decide-link":
        _print({"decision_id": decide_link(conn, args.link_id, args.decision, args.rationale)})
    elif args.cmd == "decide-field":
        _print({"decision_id": resolve_field_conflict(
            conn, args.region_id, args.effective_date,
            args.field_name, args.override_value, args.rationale)})
    elif args.cmd == "decide-reuse":
        _print({"decision_id": decide_name_reuse(conn, args.link_id, args.decision, args.rationale)})
    elif args.cmd == "resolve-pending":
        _print({"decision_id": resolve_pending(conn, args.conflict_id, args.resolution)})
    elif args.cmd == "web":
        serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
