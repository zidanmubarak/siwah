"""One-use, process-local gate approvals bound to candidate and repository state.

Only protected orchestration issues these internal capabilities. They are not
JSON verdicts supplied by a model or a persisted claim that checks passed.
This is an application API boundary, not a security boundary against arbitrary
same-account Python execution or access to this process's memory.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .manifest import build_manifest, verify_manifest


@dataclass(frozen=True)
class GateApproval:
    candidate_hash: str
    root: str
    head: str
    enforcement_hash: str
    policy: str


_issued: dict[int, GateApproval] = {}


def _issue_approval(root: Path, head: str, payload: str, *, policy: str) -> GateApproval:
    integrity = verify_manifest(Path(__file__).resolve().parents[3])
    if not integrity.ok:
        raise RuntimeError("kernel enforcement integrity check failed")
    approval = GateApproval(
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        str(root.resolve()),
        head,
        str(build_manifest(Path(__file__).resolve().parents[3])["hash"]),
        policy,
    )
    _issued[id(approval)] = approval
    return approval


def consume_approval(approval: GateApproval | None, root: Path, head: str, payload: str) -> None:
    if approval is None or _issued.pop(id(approval), None) is not approval:
        raise RuntimeError("promotion requires a fresh kernel-issued gate approval")
    if (
        approval.root != str(root.resolve())
        or approval.head != head
        or approval.candidate_hash != hashlib.sha256(payload.encode("utf-8")).hexdigest()
        or approval.enforcement_hash
        != str(build_manifest(Path(__file__).resolve().parents[3])["hash"])
        or not verify_manifest(Path(__file__).resolve().parents[3]).ok
    ):
        raise RuntimeError("gate approval does not match candidate, HEAD or enforcement")
