"""PBFT protocol message dataclasses with canonical JSON signing helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from nacl.signing import SigningKey, VerifyKey

from byzheist.pbft.crypto import sha256_hex, sign_payload, verify_payload


@dataclass(frozen=True)
class Request:
    """Client transfer request (or primary-generated noop gap fill in NewView)."""

    TYPE: ClassVar[str] = "REQUEST"

    client_id: str
    timestamp: int
    sender: str
    recipient: str
    amount: int
    noop: bool = False
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "client_id": self.client_id,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "recipient": self.recipient,
            "amount": self.amount,
            "noop": self.noop,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Request:
        return cls(
            client_id=str(data["client_id"]),
            timestamp=int(data["timestamp"]),
            sender=str(data["sender"]),
            recipient=str(data["recipient"]),
            amount=int(data["amount"]),
            noop=bool(data.get("noop", False)),
            signature=str(data.get("signature", "")),
        )

    def digest(self) -> str:
        return sha256_hex(self.to_dict())

    def signed(self, signing_key: SigningKey) -> Request:
        sig = sign_payload(signing_key, self.signable_dict())
        return Request(
            client_id=self.client_id,
            timestamp=self.timestamp,
            sender=self.sender,
            recipient=self.recipient,
            amount=self.amount,
            noop=self.noop,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class PrePrepare:
    """Primary proposal assigning sequence number seq in view to request."""

    TYPE: ClassVar[str] = "PRE_PREPARE"

    view: int
    seq: int
    digest: str
    request: Request
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "view": self.view,
            "seq": self.seq,
            "digest": self.digest,
            "request": self.request.to_dict(),
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrePrepare:
        return cls(
            view=int(data["view"]),
            seq=int(data["seq"]),
            digest=str(data["digest"]),
            request=Request.from_dict(data["request"]),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> PrePrepare:
        sig = sign_payload(signing_key, self.signable_dict())
        return PrePrepare(
            view=self.view,
            seq=self.seq,
            digest=self.digest,
            request=self.request,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class Prepare:
    """Backup vote carrying the primary's signed PrePrepare for transferable proof."""

    TYPE: ClassVar[str] = "PREPARE"

    view: int
    seq: int
    digest: str
    pre_prepare: PrePrepare
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "view": self.view,
            "seq": self.seq,
            "digest": self.digest,
            "pre_prepare": self.pre_prepare.to_dict(),
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Prepare:
        return cls(
            view=int(data["view"]),
            seq=int(data["seq"]),
            digest=str(data["digest"]),
            pre_prepare=PrePrepare.from_dict(data["pre_prepare"]),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> Prepare:
        sig = sign_payload(signing_key, self.signable_dict())
        return Prepare(
            view=self.view,
            seq=self.seq,
            digest=self.digest,
            pre_prepare=self.pre_prepare,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class Commit:
    """Replica commit vote once prepared(view, seq, digest) holds."""

    TYPE: ClassVar[str] = "COMMIT"

    view: int
    seq: int
    digest: str
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "view": self.view,
            "seq": self.seq,
            "digest": self.digest,
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Commit:
        return cls(
            view=int(data["view"]),
            seq=int(data["seq"]),
            digest=str(data["digest"]),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> Commit:
        sig = sign_payload(signing_key, self.signable_dict())
        return Commit(
            view=self.view,
            seq=self.seq,
            digest=self.digest,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class Reply:
    """Signed replica execution result sent back to the client."""

    TYPE: ClassVar[str] = "REPLY"

    view: int
    seq: int
    client_id: str
    timestamp: int
    result: str  # "ok" | "rejected"
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "view": self.view,
            "seq": self.seq,
            "client_id": self.client_id,
            "timestamp": self.timestamp,
            "result": self.result,
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reply:
        return cls(
            view=int(data["view"]),
            seq=int(data["seq"]),
            client_id=str(data["client_id"]),
            timestamp=int(data["timestamp"]),
            result=str(data["result"]),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> Reply:
        sig = sign_payload(signing_key, self.signable_dict())
        return Reply(
            view=self.view,
            seq=self.seq,
            client_id=self.client_id,
            timestamp=self.timestamp,
            result=self.result,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class PreparedCert:
    """Transferable certificate proving (view, seq, digest) prepared at 2f+1 replicas."""

    view: int
    seq: int
    digest: str
    pre_prepare: PrePrepare
    prepares: tuple[Prepare, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "seq": self.seq,
            "digest": self.digest,
            "pre_prepare": self.pre_prepare.to_dict(),
            "prepares": [p.to_dict() for p in self.prepares],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreparedCert:
        return cls(
            view=int(data["view"]),
            seq=int(data["seq"]),
            digest=str(data["digest"]),
            pre_prepare=PrePrepare.from_dict(data["pre_prepare"]),
            prepares=tuple(Prepare.from_dict(p) for p in data.get("prepares", ())),
        )


@dataclass(frozen=True)
class ViewChange:
    """Signed view-change vote carrying highest-view prepared certificates per seq."""

    TYPE: ClassVar[str] = "VIEW_CHANGE"

    new_view: int
    last_exec_seq: int
    prepared: tuple[PreparedCert, ...]
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "new_view": self.new_view,
            "last_exec_seq": self.last_exec_seq,
            "prepared": [p.to_dict() for p in self.prepared],
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ViewChange:
        return cls(
            new_view=int(data["new_view"]),
            last_exec_seq=int(data["last_exec_seq"]),
            prepared=tuple(PreparedCert.from_dict(p) for p in data.get("prepared", ())),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> ViewChange:
        sig = sign_payload(signing_key, self.signable_dict())
        return ViewChange(
            new_view=self.new_view,
            last_exec_seq=self.last_exec_seq,
            prepared=self.prepared,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


@dataclass(frozen=True)
class NewView:
    """New primary proof of 2f+1 ViewChanges and deterministic re-proposed PrePrepares."""

    TYPE: ClassVar[str] = "NEW_VIEW"

    new_view: int
    view_changes: tuple[ViewChange, ...]
    pre_prepares: tuple[PrePrepare, ...]
    signer: str
    signature: str = ""

    def signable_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "new_view": self.new_view,
            "view_changes": [vc.to_dict() for vc in self.view_changes],
            "pre_prepares": [pp.to_dict() for pp in self.pre_prepares],
            "signer": self.signer,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.signable_dict()
        d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewView:
        return cls(
            new_view=int(data["new_view"]),
            view_changes=tuple(ViewChange.from_dict(v) for v in data.get("view_changes", ())),
            pre_prepares=tuple(PrePrepare.from_dict(p) for p in data.get("pre_prepares", ())),
            signer=str(data["signer"]),
            signature=str(data.get("signature", "")),
        )

    def signed(self, signing_key: SigningKey) -> NewView:
        sig = sign_payload(signing_key, self.signable_dict())
        return NewView(
            new_view=self.new_view,
            view_changes=self.view_changes,
            pre_prepares=self.pre_prepares,
            signer=self.signer,
            signature=sig,
        )

    def verify(self, verify_key: VerifyKey) -> bool:
        return verify_payload(verify_key, self.signable_dict(), self.signature)


ProtocolMessage = Request | PrePrepare | Prepare | Commit | Reply | ViewChange | NewView
