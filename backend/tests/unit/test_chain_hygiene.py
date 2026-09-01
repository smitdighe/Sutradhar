"""Static guarantees about ``app/chain`` that no runtime test can give.

A swallowed exception in a queue drain is indistinguishable from a queue that
works: the jobs stop, nothing is logged, and every symptom points at the chain.
So the ban on swallowing is enforced by reading the source, not by hoping.

The contract surface check is the other half. The writer encodes calldata against
a selector computed from an assumed signature; if the deployed ABI drifts, the
transactions do not fail loudly, they call something else or nothing at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.chain.contract import (
    REQUIRED_EVENTS,
    REQUIRED_FUNCTIONS,
    ContractSurfaceError,
    event_topic,
    load_contract,
    selector,
)

pytestmark = pytest.mark.unit

CHAIN_DIR = Path(__file__).resolve().parents[2] / "app" / "chain"

# The two patterns that hide a failure completely: a bare except, and an except
# whose entire body is `pass`.
SWALLOWED = re.compile(r"except\s*:|except[^\n]*:\s*\n\s*pass\b")


def chain_sources() -> list[Path]:
    return sorted(path for path in CHAIN_DIR.rglob("*.py"))


class TestNoSwallowedErrors:
    def test_the_chain_package_has_sources_to_check(self) -> None:
        # Guards against the whole check silently passing on an empty glob.
        assert len(chain_sources()) >= 8

    @pytest.mark.parametrize("path", chain_sources(), ids=lambda p: p.name)
    def test_no_bare_except_and_no_silently_passed_exception(self, path: Path) -> None:
        source = path.read_text(encoding="utf-8")

        offences = [
            (source[: match.start()].count("\n") + 1, match.group(0).strip())
            for match in SWALLOWED.finditer(source)
        ]

        assert not offences, f"{path.name} swallows exceptions at {offences}"

    @pytest.mark.parametrize("path", chain_sources(), ids=lambda p: p.name)
    def test_every_broad_except_re_raises_or_logs(self, path: Path) -> None:
        """``except Exception`` is allowed, but only where it does something."""
        lines = path.read_text(encoding="utf-8").splitlines()

        for index, line in enumerate(lines):
            if "except Exception" not in line:
                continue
            body = "\n".join(lines[index + 1 : index + 8])
            assert (
                "logger." in body or "raise" in body or "return" in body
            ), f"{path.name}:{index + 1} catches broadly without logging or re-raising"


class TestContractSurface:
    def test_the_deployed_abi_exposes_what_the_writer_encodes(self) -> None:
        binding = load_contract()

        names = {entry["name"] for entry in binding.abi if entry.get("type") == "function"}
        events = {entry["name"] for entry in binding.abi if entry.get("type") == "event"}

        assert set(REQUIRED_FUNCTIONS) <= names
        assert set(REQUIRED_EVENTS) <= events

    def test_a_drifted_signature_fails_at_load_rather_than_at_send(self) -> None:
        from app.chain.contract import ContractBinding

        drifted = [
            {
                "type": "function",
                "name": "anchorItem",
                # One argument instead of two: encoding against this would
                # produce calldata nothing answers to.
                "inputs": [{"name": "itemHash", "type": "bytes32"}],
                "outputs": [],
            }
        ]

        with pytest.raises(ContractSurfaceError, match="anchorItem"):
            ContractBinding("0x" + "11" * 20, drifted)

    def test_an_event_that_changed_its_indexing_is_rejected(self) -> None:
        from app.chain.contract import ContractBinding

        artifact = load_contract().abi
        mutated = []
        for entry in artifact:
            if entry.get("type") == "event" and entry["name"] == "ItemAnchored":
                copy = dict(entry)
                copy["inputs"] = [
                    # Un-indexing the item hash moves it out of the topics and
                    # into the data blob, which silently breaks every decode.
                    {**argument, "indexed": False}
                    for argument in entry["inputs"]
                ]
                mutated.append(copy)
            else:
                mutated.append(entry)

        with pytest.raises(ContractSurfaceError, match="ItemAnchored"):
            ContractBinding("0x" + "11" * 20, mutated)

    def test_selectors_and_topics_match_the_canonical_signatures(self) -> None:
        # Hardcoded so a change to the hashing or the signature builder shows up
        # here rather than as transactions that quietly do nothing.
        assert selector("anchorItem", ("bytes32", "bytes32")).hex() == "fa7187ad"
        assert selector("anchorBatch", ("bytes32", "uint32")).hex() == "97077da9"
        assert event_topic("ItemAnchored", ("bytes32", "bytes32", "address", "uint256")) == (
            "0x5055b1b31c7527630748499fb827fca7aa86bb9dd63250660971ec609ccd3bda"
        )
        assert event_topic("BatchAnchored", ("bytes32", "uint32", "address", "uint256")) == (
            "0xae1ce706916ab35524806d94ec01cf225e4241ae631ef80efb0208e396d3a1c4"
        )


class TestRevertDecoding:
    def test_already_anchored_is_recognised_as_a_completed_job(self) -> None:
        binding = load_contract()
        payload = selector("AlreadyAnchored", ("bytes32",)) + bytes(31) + b"\x07"

        decoded = binding.decode_revert(payload)

        assert decoded is not None
        assert decoded.is_already_anchored

    def test_a_solidity_string_revert_is_decoded(self) -> None:
        from eth_abi.abi import encode as abi_encode

        from app.chain.contract import decode_revert

        payload = bytes.fromhex("08c379a0") + abi_encode(["string"], ["not the owner"])

        decoded = decode_revert(payload)

        assert decoded is not None
        assert decoded.name == "Error"
        assert decoded.args == ("not the owner",)

    def test_an_unrecognised_revert_is_admitted_rather_than_guessed(self) -> None:
        from app.chain.contract import decode_revert

        assert decode_revert(b"\xde\xad\xbe\xef") is None
        assert decode_revert(b"") is None
        assert decode_revert(None) is None

    def test_a_missing_artifact_names_the_command_that_builds_it(self) -> None:
        with pytest.raises(ContractSurfaceError, match="npm run compile"):
            load_contract(abi_path=Path("/nonexistent/Sutradhar.json"))
