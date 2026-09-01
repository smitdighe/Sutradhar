"""Media upload, three-tier storage, and IPFS pinning.

IPFS stores nothing by itself. The SHA-256 goes on chain and proves the bytes
were never altered; the bytes themselves live in three places so the chain
guarantee survives a lapsed pinning tier and an ephemeral disk.
"""
