"""Conclave OS CLI.

  python cli.py submit "your task text"
  python cli.py list
  python cli.py status s_20260612_ab12cd34
  python cli.py log s_20260612_ab12cd34
"""

from __future__ import annotations

import argparse
import json
import sys

from conclave_os.service import ConclaveService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conclave-os")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="submit a task and run it")
    p_submit.add_argument("text")
    p_submit.add_argument("--source", default="cli")
    p_submit.add_argument(
        "--backend", default=None, choices=["mock", "switchboard"],
        help="agent backend (default: CONCLAVE_OS_BACKEND env or mock)",
    )

    sub.add_parser("list", help="list sessions")

    p_serve = sub.add_parser("serve", help="run the Conclave OS service + web dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8790)
    p_serve.add_argument("--backend", default=None, choices=["mock", "switchboard"])
    sub.add_parser("pending", help="list pending approvals across sessions")

    p_approve = sub.add_parser("approve", help="approve a pending approval (resumes the session)")
    p_approve.add_argument("session_id")
    p_approve.add_argument("approval_id")
    p_approve.add_argument("--by", default="user")

    p_deny = sub.add_parser("deny", help="deny a pending approval (cancels the session)")
    p_deny.add_argument("session_id")
    p_deny.add_argument("approval_id")
    p_deny.add_argument("--by", default="user")

    sub.add_parser("inputs", help="list questions agents are waiting on")

    p_answer = sub.add_parser("answer", help="answer an agent's question (resumes the session)")
    p_answer.add_argument("session_id")
    p_answer.add_argument("input_id")
    p_answer.add_argument("text", nargs="+")
    p_answer.add_argument("--by", default="user")

    p_decline = sub.add_parser("decline", help="decline an agent's question (cancels the session)")
    p_decline.add_argument("session_id")
    p_decline.add_argument("input_id")
    p_decline.add_argument("--by", default="user")

    p_status = sub.add_parser("status", help="show a session")
    p_status.add_argument("session_id")

    p_log = sub.add_parser("log", help="print a session's JSONL event trail")
    p_log.add_argument("session_id")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import os

        import uvicorn

        if args.backend:
            os.environ["CONCLAVE_OS_BACKEND"] = args.backend
        print(f"Conclave OS dashboard: http://{args.host}:{args.port}/")
        uvicorn.run("conclave_os.main:app", host=args.host, port=args.port)
        return 0

    service = ConclaveService(backend=getattr(args, "backend", None))

    if args.command == "submit":
        session = service.run(args.text, source=args.source)
        print(f"session: {session.session_id}")
        print(f"status:  {session.status.value}  ({session.stop_reason or 'completed normally'})")
        if session.final:
            print(json.dumps(session.final.model_dump(), indent=2, ensure_ascii=False))
        for a in session.approvals:
            if a.status == "pending":
                print(f"PENDING APPROVAL [{a.approval_id}]: {a.action} (category={a.category}, risk={a.risk.value})")
                print(f"  resolve with: python cli.py approve|deny {session.session_id} {a.approval_id}")
        for r in session.input_requests:
            if r.status == "pending":
                print(f"AGENT QUESTION [{r.input_id}] from {r.role.value}@{r.agent}: {r.question}")
                print(f"  resolve with: python cli.py answer|decline {session.session_id} {r.input_id} [text]")
        return 0

    if args.command == "pending":
        rows = service.pending_approvals()
        if not rows:
            print("no pending approvals")
        for a in rows:
            print(f"{a['session_id']}  {a['approval_id']}  [{a['category']}/{a['risk']}]  {a['action']}")
        return 0

    if args.command == "inputs":
        rows = service.pending_inputs()
        if not rows:
            print("no pending agent questions")
        for r in rows:
            print(f"{r['session_id']}  {r['input_id']}  [{r['role']}@{r['agent']}]")
            print(f"  Q: {r['question']}")
        return 0

    if args.command in ("answer", "decline"):
        try:
            if args.command == "answer":
                session = service.answer(args.session_id, args.input_id, " ".join(args.text), by=args.by)
            else:
                session = service.decline_input(args.session_id, args.input_id, by=args.by)
        except (KeyError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"session: {session.session_id}")
        print(f"status:  {session.status.value}  ({session.stop_reason or 'completed normally'})")
        for r in session.input_requests:
            if r.status == "pending":
                print(f"AGENT QUESTION [{r.input_id}] from {r.role.value}@{r.agent}: {r.question}")
                print(f"  resolve with: python cli.py answer|decline {session.session_id} {r.input_id} [text]")
        if session.final:
            print(json.dumps(session.final.model_dump(), indent=2, ensure_ascii=False))
        return 0

    if args.command in ("approve", "deny"):
        approved = args.command == "approve"
        try:
            session = service.approve(args.session_id, args.approval_id, approved, by=args.by)
        except (KeyError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"session: {session.session_id}")
        print(f"status:  {session.status.value}  ({session.stop_reason or 'completed normally'})")
        for f in session.files_changed:
            print(f"FILE WRITTEN: {f}")
        for a in session.approvals:
            if a.status == "pending":
                print(f"PENDING APPROVAL [{a.approval_id}]: {a.action} (category={a.category}, risk={a.risk.value})")
                print(f"  resolve with: python cli.py approve|deny {session.session_id} {a.approval_id}")
        if session.final:
            print(json.dumps(session.final.model_dump(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "list":
        for s in service.list():
            print(f"{s['session_id']}  {s['status']:<20} {s['created_at']}")
        return 0

    if args.command == "status":
        data = service.get(args.session_id)
        if data is None:
            print("session not found", file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if args.command == "log":
        path = service.store.session_log_path(args.session_id)
        if not path.exists():
            print("no log for that session", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
