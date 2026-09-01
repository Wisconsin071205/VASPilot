/** 扩展入口：URI 唤起、虚拟文件系统注册与状态栏。 */
import * as vscode from "vscode";
import { ConsoleClient, ConsoleUnavailableError } from "./api";
import { describe } from "./errors";
import { RemoteWorkspaceProvider, SCHEME } from "./provider";

let client: ConsoleClient | null = null;
let statusItem: vscode.StatusBarItem | null = null;

function capBytes(): number {
  const mb = vscode.workspace
    .getConfiguration("huwei-bridge")
    .get<number>("maxFileSizeMB", 32);
  return mb * 1048576;
}

function maxEntries(): number {
  return vscode.workspace
    .getConfiguration("huwei-bridge")
    .get<number>("maxEntries", 500);
}

function getClient(): ConsoleClient {
  if (!client) {
    const explicit = vscode.workspace
      .getConfiguration("huwei-bridge")
      .get<string | null>("consoleUrl", null);
    client = ConsoleClient.create(explicit);
  }
  return client;
}

function setStatus(text: string, tooltip = "", error = false): void {
  if (!statusItem) return;
  statusItem.text = text;
  statusItem.tooltip = tooltip;
  statusItem.backgroundColor = error
    ? new vscode.ThemeColor("statusBarItem.errorBackground")
    : undefined;
  statusItem.show();
}

async function openRemote(
  server: string,
  remotePath: string,
  kind: string
): Promise<void> {
  const target = vscode.Uri.from({
    scheme: SCHEME,
    authority: server,
    path: remotePath,
  });
  if (kind === "file") {
    const doc = await vscode.workspace.openTextDocument(target);
    await vscode.window.showTextDocument(doc, { preview: false });
  } else {
    const folders = vscode.workspace.workspaceFolders ?? [];
    const already = folders.some((f) => f.uri.toString() === target.toString());
    if (!already) {
      vscode.workspace.updateWorkspaceFolders(folders.length, 0, {
        uri: target,
        name: `${server}:${remotePath.split("/").filter(Boolean).pop() ?? server}`,
      });
    }
    await vscode.commands.executeCommand(
      "workbench.explorer.folderView.focus"
    );
  }
  setStatus(
    `$(remote) Huwei Bridge: ${server}`,
    `虚拟工作区已连接 ${server}（按需读写，经本地控制台中转）`
  );
}

export function activate(context: vscode.ExtensionContext): void {
  statusItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    10
  );
  context.subscriptions.push(statusItem);

  const provider = new RemoteWorkspaceProvider(
    getClient,
    capBytes,
    maxEntries
  );
  context.subscriptions.push(
    vscode.workspace.registerFileSystemProvider(SCHEME, provider, {
      isCaseSensitive: true,
    })
  );

  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri(uri: vscode.Uri) {
        const q = new URLSearchParams(uri.query);
        const server = q.get("server") ?? "";
        const remotePath = q.get("path") ?? "";
        const kind = q.get("kind") ?? "folder";
        if (!server || !remotePath.startsWith("/")) {
          vscode.window.showErrorMessage(
            "唤起链接不完整：缺少 server 或 path 参数"
          );
          return;
        }
        setStatus("$(sync~spin) Huwei Bridge: 连接中…");
        openRemote(server, remotePath, kind).catch((err) => {
          setStatus(`$(error) Huwei Bridge: 连接失败`, describe(err), true);
          vscode.window.showErrorMessage(
            `Huwei Bridge 打开失败：${describe(err)}`
          );
        });
      },
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("huwei-bridge.reconnect", async () => {
      client = null;
      try {
        await getClient().servers();
        setStatus("$(check) Huwei Bridge: 控制台已就绪");
        vscode.window.showInformationMessage(
          "已重新检测到本地控制台，可再次从控制台右键唤起远端文件。"
        );
      } catch (err) {
        setStatus("$(error) Huwei Bridge: 控制台不可用", describe(err), true);
        vscode.window.showErrorMessage(describe(err));
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("huwei-bridge.refresh", async () => {
      await vscode.commands.executeCommand("workbench.action.files.revert");
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("huwei-bridge")) client = null;
    })
  );

  // 控制台未启动时保持安静：等 URI 唤起或手动命令再报错
  setStatus("$(plug) Huwei Bridge: 待唤起");
}

export function deactivate(): void {}
