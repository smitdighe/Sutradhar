"""Deploy the Sutradhar registry from the same artifact the client binds against.

Deployment lives here rather than in a Hardhat script for one reason: the relayer
key is already loaded in this process, and handing a private key to a second
runtime to save a few lines is a bad trade. Hardhat's job is to compile.

The ABI and the bytecode come from one exported artifact, so a contract cannot be
deployed from one compilation and called against another -- the kind of drift that
produces a live address whose calls decode to nothing.

Usage::

    # a local node, started natively with `npm run node` in backend/contracts
    uv run python scripts/deploy_contract.py --rpc http://127.0.0.1:8545 --chain-id 31337

    # Polygon Amoy, using CHAIN_RPC_URL and CHAIN_SIGNER_PRIVATE_KEY from .env
    uv run python scripts/deploy_contract.py

Prints the deployed address. Put it in ``CONTRACT_ADDRESS``; nothing is written
to ``.env`` automatically, because silently editing a config file is a surprise
nobody wants from a deploy script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from app.config import get_settings  # noqa: E402

GWEI = 10**9


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Deploy the Sutradhar anchoring registry.")
    parser.add_argument("--rpc", default=settings.chain_rpc_url)
    parser.add_argument("--chain-id", type=int, default=settings.chain_id)
    parser.add_argument(
        "--private-key",
        default=settings.chain_signer_private_key,
        help="deployer key; defaults to CHAIN_SIGNER_PRIVATE_KEY",
    )
    parser.add_argument(
        "--writer",
        default="",
        help=(
            "address allowlisted to anchor, in addition to the deployer. "
            "Defaults to the deployer itself."
        ),
    )
    parser.add_argument("--max-fee-gwei", type=int, default=settings.chain_max_fee_gwei)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    settings = get_settings()

    if not args.private_key.strip():
        print(
            "no deployer key: pass --private-key or set CHAIN_SIGNER_PRIVATE_KEY",
            file=sys.stderr,
        )
        return 2

    artifact_path = settings.contract_abi_path
    if not artifact_path.is_file():
        print(
            f"no artifact at {artifact_path} -- run `npm run compile` in backend/contracts",
            file=sys.stderr,
        )
        return 2
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    web3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not web3.is_connected():
        print(f"cannot reach {args.rpc}", file=sys.stderr)
        return 2

    reported = web3.eth.chain_id
    if reported != args.chain_id:
        # Deploying to the wrong network is easy to do and expensive to notice.
        print(
            f"chain id mismatch: rpc reports {reported}, expected {args.chain_id}",
            file=sys.stderr,
        )
        return 2

    account = Account.from_key(args.private_key.strip())
    balance = web3.eth.get_balance(account.address)
    print(f"deployer  : {account.address}")
    print(f"balance   : {web3.from_wei(balance, 'ether')}")
    if balance == 0:
        print("deployer has no funds on this network", file=sys.stderr)
        return 2

    initial_writer = args.writer.strip() or account.address
    contract = web3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])
    constructor = contract.constructor(Web3.to_checksum_address(initial_writer))

    latest = web3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", 0) or 0
    try:
        tip = int(web3.eth.max_priority_fee)
    except Exception:  # noqa: BLE001 - not every node implements it
        tip = 1 * GWEI
    max_fee = base_fee * 2 + tip
    cap = args.max_fee_gwei * GWEI
    if max_fee > cap:
        print(
            f"required max fee {max_fee / GWEI:.4f} gwei exceeds the cap "
            f"{args.max_fee_gwei} gwei; refusing to deploy",
            file=sys.stderr,
        )
        return 2

    gas = constructor.estimate_gas({"from": account.address})
    transaction = constructor.build_transaction(
        {
            "from": account.address,
            "nonce": web3.eth.get_transaction_count(account.address, "pending"),
            "chainId": args.chain_id,
            "gas": gas * 120 // 100,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": min(tip, max_fee),
            "type": 2,
        }
    )

    signed = account.sign_transaction(transaction)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    tx_hash = web3.eth.send_raw_transaction(raw)
    print(f"tx        : {tx_hash.hex()}")

    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt["status"] != 1:
        print("deployment reverted", file=sys.stderr)
        return 1

    address = receipt["contractAddress"]
    print(f"address   : {address}")
    print(f"block     : {receipt['blockNumber']}")
    print(f"gas used  : {receipt['gasUsed']}")
    print(f"writer    : {initial_writer}")
    print()
    print("set these in backend/.env:")
    print(f"  CONTRACT_ADDRESS={address}")
    print(f"  CHAIN_ID={args.chain_id}")
    print(f"  CHAIN_RPC_URL={args.rpc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
