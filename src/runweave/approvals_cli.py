"""`runweave approvals`: decide agents' approval requests from a terminal.

    runweave approvals list [--all]            pending requests (or all) on the server
    runweave approvals show ID [--json]        one request with its exact arguments
    runweave approvals approve ID [--yes]      show it, confirm, then approve
    runweave approvals reject ID [--yes]       show it, confirm, then reject

A decision is sent with the binding just read (argument digest, policy version, expiry),
so it applies only to the request that was shown. Exit codes: 0 done, 1 the server
refused the decision, 2 a usage, not-found or connection problem.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

DEFAULT_SERVER = "http://127.0.0.1:8000"


class CliProblem(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _client(server: str) -> httpx.Client:
    headers = {"Accept": "application/json"}
    token = os.environ.get("RUNWEAVE_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=server.rstrip("/"), headers=headers, timeout=30, trust_env=False)


def _call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, path, **kwargs)
    except httpx.HTTPError as error:
        raise CliProblem(
            f"Cannot reach RunWeave at {client.base_url} ({type(error).__name__})"
        ) from None


def _detail_code(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return str(detail.get("code")) if isinstance(detail, dict) else f"HTTP_{response.status_code}"


def _check(response: httpx.Response, approval_id: str | None = None) -> Any:
    if response.status_code == 404 and approval_id:
        raise CliProblem(f"Approval request {approval_id} was not found")
    if response.status_code in (401, 403):
        raise CliProblem(
            "RunWeave refused the request (set RUNWEAVE_API_TOKEN for a server with "
            f"authentication): {_detail_code(response)}"
        )
    if response.status_code == 409:
        raise CliProblem(f"Decision refused: {_detail_code(response)}", code=1)
    if response.status_code >= 400:
        raise CliProblem(f"RunWeave answered {response.status_code}: {_detail_code(response)}")
    return response.json()


def _expires(value: str) -> str:
    seconds = int((datetime.fromisoformat(value) - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "expired"
    return f"expires in {seconds // 60} min" if seconds >= 60 else f"expires in {seconds} s"


def describe(approval: dict[str, Any]) -> str:
    state = approval["status"]
    if state == "pending":
        state += f", {_expires(approval['expires_at'])}"
    lines = [
        f"Request    {approval['id']}  ({state})",
        f"Agent      {approval['agent']}  (run {approval['run_id']})",
        f"Action     {approval['action']}",
        f"Digest     {approval['argument_digest']}",
        f"Policy     {approval['policy_version']}",
    ]
    if approval.get("decided_by"):
        lines.append(f"Decided    by {approval['decided_by']} at {approval['decided_at']}")
    lines += ["Arguments:", json.dumps(approval["arguments"], indent=2, ensure_ascii=False)]
    return "\n".join(lines)


def _approval_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise CliProblem(f"Not an approval request ID: {value!r}") from None


def list_command(args: argparse.Namespace) -> int:
    with _client(args.server) as client:
        status = "all" if args.all else "pending"
        body = _check(_call(client, "GET", "/v1/approvals", params={"status": status}))
    if body.get("durable") is False:
        print(
            "This RunWeave has no database, so agents cannot request approvals. Start it "
            "with RUNWEAVE_DATABASE_URL set to PostgreSQL."
        )
        return 0
    approvals = body["approvals"]
    if args.json:
        print(json.dumps(approvals, indent=2, ensure_ascii=False))
        return 0
    if not approvals:
        print("No pending requests." if not args.all else "No requests.")
        return 0
    rows = [("ID", "STATUS", "AGENT", "ACTION", "RUN", "EXPIRES")] + [
        (
            a["id"],
            a["status"],
            a["agent"],
            a["action"],
            a["run_id"][:8],
            _expires(a["expires_at"]) if a["status"] == "pending" else "-",
        )
        for a in approvals
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    return 0


def show_command(args: argparse.Namespace) -> int:
    approval_id = _approval_id(args.id)
    with _client(args.server) as client:
        approval = _check(_call(client, "GET", f"/v1/approvals/{approval_id}"), approval_id)
    print(json.dumps(approval, indent=2, ensure_ascii=False) if args.json else describe(approval))
    return 0


def decide_command(args: argparse.Namespace) -> int:
    approval_id = _approval_id(args.id)
    decision = args.decision
    with _client(args.server) as client:
        approval = _check(_call(client, "GET", f"/v1/approvals/{approval_id}"), approval_id)
        print(describe(approval))
        if not args.yes:
            print(f"{decision.capitalize()} this request? [y/N] ", end="", flush=True)
            answer = sys.stdin.readline()
            if not answer:
                raise CliProblem("No answer to the prompt; pass --yes to decide without one")
            if answer.strip().lower() not in ("y", "yes"):
                raise CliProblem("Not decided.")
        body = {
            "decision": decision,
            "argument_digest": approval["argument_digest"],
            "policy_version": approval["policy_version"],
            "expires_at": approval["expires_at"],
        }
        decided = _check(
            _call(client, "POST", f"/v1/approvals/{approval_id}", json=body), approval_id
        )
    print(f"{decided['status'].capitalize()}.")
    return 0


def add_parser(commands: Any) -> None:
    approvals = commands.add_parser(
        "approvals", help="List and decide agents' approval requests (durable RunWeave)"
    )
    actions = approvals.add_subparsers(dest="approvals_command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--server", default=DEFAULT_SERVER, help=f"RunWeave to talk to ({DEFAULT_SERVER})"
        )

    listing = actions.add_parser("list", help="Pending requests (--all for decided ones too)")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--json", action="store_true")
    common(listing)
    listing.set_defaults(handler=run, action=list_command)
    show = actions.add_parser("show", help="One request with its exact arguments")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    common(show)
    show.set_defaults(handler=run, action=show_command)
    for name in ("approve", "reject"):
        decide = actions.add_parser(name, help=f"Show a request, confirm, then {name} it")
        decide.add_argument("id")
        decide.add_argument("--yes", action="store_true", help="Decide without a prompt")
        common(decide)
        decide.set_defaults(handler=run, action=decide_command, decision=name)


def run(args: argparse.Namespace) -> int:
    try:
        code: int = args.action(args)
    except CliProblem as problem:
        print(f"runweave: {problem}", file=sys.stderr)
        return problem.code
    return code
