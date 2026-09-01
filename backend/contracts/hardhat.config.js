/**
 * Hardhat is here for two jobs only: compile the registry, and run a local EVM
 * node natively when one is wanted. Deployment lives in Python
 * (`backend/scripts/deploy_contract.py`) because the relayer key is already
 * loaded there and there is no reason to hand it to a second runtime.
 *
 * `evmVersion: "paris"` is deliberate. Polygon PoS trails Ethereum's hardfork
 * schedule, and a contract compiled for Cancun can emit opcodes (PUSH0, TSTORE)
 * that a Polygon node has not yet activated. Paris is the safe floor; nothing in
 * Sutradhar.sol needs anything newer.
 */

/** @type {import('hardhat/config').HardhatUserConfig} */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: "paris",
    },
  },
  paths: {
    sources: "./src",
    artifacts: "./artifacts",
    cache: "./cache",
  },
  networks: {
    // `npm run node` -- a native local chain, no containers involved.
    hardhat: {
      chainId: 31337,
    },
  },
};
