// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title Sutradhar anchoring registry
/// @notice Records that an address claimed a hash at a point in time. Nothing more.
///
/// This contract does not authenticate a physical textile and cannot. It proves
/// WHO claimed WHAT WHEN, unchangeably. A hash present here means someone with a
/// writer key asserted a record; whether that assertion is true is a question for
/// the co-op attestation and inspection layers off chain, not for this file.
///
/// Design notes that the off-chain writer depends on:
///
/// *Anchors are write-once.* Re-anchoring a known hash reverts with
/// {AlreadyAnchored} rather than overwriting the stored timestamp. Overwriting
/// would rewrite the WHEN, which is the one thing this contract exists to fix in
/// place. The Python writer treats that revert as success, because a reorg replay
/// whose original transaction was re-included is a job already done, not a failure.
///
/// *State is stored, not only logged.* One packed slot per anchor makes
/// verification a single `eth_call` against `itemAnchors`, so a verifier never has
/// to scan logs or trust an indexer. The indexer exists for throughput, not for
/// correctness.
///
/// *Writing is allowlisted.* On a public testnet an open `anchorItem` lets anyone
/// fill the registry with hashes of nothing. The allowlist bounds the set of
/// addresses whose claims this registry carries; it is not a claim that those
/// addresses are honest.
contract Sutradhar {
    /// @dev address(160) + uint64(64) = 224 bits. One storage slot, one SSTORE.
    struct ItemAnchor {
        address issuer;
        uint64 anchoredAt;
    }

    /// @dev address(160) + uint64(64) + uint32(32) = 256 bits. Exactly one slot.
    struct BatchAnchor {
        address issuer;
        uint64 anchoredAt;
        uint32 leafCount;
    }

    address public owner;

    mapping(address => bool) public writers;

    /// @notice itemHash => who anchored it and when. `anchoredAt == 0` means absent.
    mapping(bytes32 => ItemAnchor) public itemAnchors;

    /// @notice Merkle root => who anchored it, when, and over how many leaves.
    mapping(bytes32 => BatchAnchor) public batchAnchors;

    event ItemAnchored(
        bytes32 indexed itemHash,
        bytes32 indexed issuerHash,
        address indexed issuer,
        uint256 timestamp
    );

    event BatchAnchored(bytes32 indexed root, uint32 leafCount, address issuer, uint256 timestamp);

    event WriterSet(address indexed writer, bool allowed);

    event OwnerTransferred(address indexed previousOwner, address indexed newOwner);

    error NotOwner();
    error NotWriter();
    error ZeroHash();
    error ZeroAddress();
    error EmptyBatch();
    /// @dev Carries the key so the off-chain writer can confirm which anchor already existed.
    error AlreadyAnchored(bytes32 key);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyWriter() {
        if (!writers[msg.sender]) revert NotWriter();
        _;
    }

    constructor(address initialWriter) {
        owner = msg.sender;
        emit OwnerTransferred(address(0), msg.sender);

        // The deployer can always write, so a deployment is never dead on arrival.
        writers[msg.sender] = true;
        emit WriterSet(msg.sender, true);

        // Deployer and relayer are usually different keys: the relayer is the hot
        // one the backend holds, the deployer ideally is not.
        if (initialWriter != address(0) && initialWriter != msg.sender) {
            writers[initialWriter] = true;
            emit WriterSet(initialWriter, true);
        }
    }

    // ------------------------------------------------------------------ anchoring

    /// @notice Anchor one item hash.
    /// @param itemHash keccak256 of the RFC 8785 canonical item preimage.
    /// @param issuerHash Salted identity digest of the registrant. Never a user id,
    ///        never an email -- deleting the off-chain salt makes this unlinkable,
    ///        which is the erasure mechanism this chain cannot otherwise provide.
    function anchorItem(bytes32 itemHash, bytes32 issuerHash) external onlyWriter {
        if (itemHash == bytes32(0)) revert ZeroHash();
        if (itemAnchors[itemHash].anchoredAt != 0) revert AlreadyAnchored(itemHash);

        itemAnchors[itemHash] = ItemAnchor({issuer: msg.sender, anchoredAt: uint64(block.timestamp)});

        emit ItemAnchored(itemHash, issuerHash, msg.sender, block.timestamp);
    }

    /// @notice Anchor a Merkle root covering many item hashes.
    /// @dev The economics of the whole system live here: one transaction for a
    ///      whole batch instead of one per item. Each item keeps an independently
    ///      verifiable inclusion proof, served off chain and checked against this
    ///      root.
    function anchorBatch(bytes32 root, uint32 leafCount) external onlyWriter {
        if (root == bytes32(0)) revert ZeroHash();
        if (leafCount == 0) revert EmptyBatch();
        if (batchAnchors[root].anchoredAt != 0) revert AlreadyAnchored(root);

        batchAnchors[root] = BatchAnchor({
            issuer: msg.sender,
            anchoredAt: uint64(block.timestamp),
            leafCount: leafCount
        });

        emit BatchAnchored(root, leafCount, msg.sender, block.timestamp);
    }

    // ------------------------------------------------------------------ reads

    /// @notice True once `itemHash` has been anchored.
    function isItemAnchored(bytes32 itemHash) external view returns (bool) {
        return itemAnchors[itemHash].anchoredAt != 0;
    }

    /// @notice True once `root` has been anchored.
    function isBatchAnchored(bytes32 root) external view returns (bool) {
        return batchAnchors[root].anchoredAt != 0;
    }

    // ------------------------------------------------------------------ admin

    function setWriter(address writer, bool allowed) external onlyOwner {
        if (writer == address(0)) revert ZeroAddress();
        writers[writer] = allowed;
        emit WriterSet(writer, allowed);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnerTransferred(owner, newOwner);
        owner = newOwner;
    }
}
