import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";

// Keep the distributable at the repository-level path documented for other
// computers. The two child processes are fixed build tools, not user input.
mkdirSync("../../dist", { recursive: true });
execFileSync("npm", ["run", "compile"], { stdio: "inherit", shell: process.platform === "win32" });
execFileSync("npx", ["--yes", "@vscode/vsce", "package", "--allow-missing-repository",
  "--out", "../../dist/huwei-agent-vscode-bridge.vsix"],
  { stdio: "inherit", shell: process.platform === "win32" });
const manifest = JSON.parse(readFileSync("package.json", "utf8"));
copyFileSync("../../dist/huwei-agent-vscode-bridge.vsix",
  `${manifest.name}-${manifest.version}.vsix`);
