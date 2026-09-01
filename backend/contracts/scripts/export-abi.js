/**
 * Copy the compiled artifact to where the Python client reads it.
 *
 * The backend reads `CONTRACT_ABI_PATH` (default `app/chain/abi/Sutradhar.json`)
 * and never reaches into `artifacts/`, which is a build directory and gitignored.
 * The exported file keeps the bytecode as well as the ABI so
 * `scripts/deploy_contract.py` can deploy from the same artifact the client
 * binds against -- an ABI and a bytecode that came from different compilations
 * is exactly the kind of drift that produces a contract whose calls silently
 * decode to nothing.
 */

const fs = require("node:fs");
const path = require("node:path");

const ARTIFACT = path.join(
  __dirname,
  "..",
  "artifacts",
  "src",
  "Sutradhar.sol",
  "Sutradhar.json"
);
const DESTINATION = path.join(
  __dirname,
  "..",
  "..",
  "app",
  "chain",
  "abi",
  "Sutradhar.json"
);

if (!fs.existsSync(ARTIFACT)) {
  console.error(`no artifact at ${ARTIFACT} -- run \`npx hardhat compile\` first`);
  process.exit(1);
}

const artifact = JSON.parse(fs.readFileSync(ARTIFACT, "utf8"));

const exported = {
  contractName: artifact.contractName,
  sourceName: artifact.sourceName,
  abi: artifact.abi,
  bytecode: artifact.bytecode,
  deployedBytecode: artifact.deployedBytecode,
};

fs.mkdirSync(path.dirname(DESTINATION), { recursive: true });
fs.writeFileSync(DESTINATION, `${JSON.stringify(exported, null, 2)}\n`, "utf8");

const functions = artifact.abi.filter((entry) => entry.type === "function").length;
const events = artifact.abi.filter((entry) => entry.type === "event").length;
console.log(
  `exported ${artifact.contractName}: ${functions} function(s), ${events} event(s) -> ${DESTINATION}`
);
