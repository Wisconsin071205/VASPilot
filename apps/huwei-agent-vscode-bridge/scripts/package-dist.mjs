import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";

// Keep the distributable at the repository-level path documented for other
// computers. The two child processes are fixed build tools, not user input.
mkdirSync("../../dist", { recursive: true });
execFileSync("npm", ["run", "compile"], { stdio: "inherit", shell: process.platform === "win32" });
execFileSync("npx", ["--yes", "@vscode/vsce", "package", "--allow-missing-repository",
  "--out", "../../dist/huwei-agent-vscode-bridge.vsix"],
  { stdio: "inherit", shell: process.platform === "win32" });
