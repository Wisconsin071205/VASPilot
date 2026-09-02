/** 扩展入口：URI 唤起、虚拟文件系统注册与状态栏。 */
import * as vscode from "vscode";
import { ConsoleClient, ConsoleUnavailableError, WorkspaceStatus } from "./api";
import { baseName, DENYLIST, parseWakeQuery } from "./core";
import { describe } from "./errors";
import { RemoteWorkspaceProvider, SCHEME } from "./provider";

let client: ConsoleClient | null = null;
let statusItem: vscode.StatusBarItem | null = null;
let out: vscode.OutputChannel | null = null;

export function log(msg: string): void {
  out?.appendLine(`[${new Date().toISOString()}] ${msg}`);
}

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

type StatusTone = "normal" | "info" | "warning" | "error";

function setStatus(text: string, tooltip = "", tone: StatusTone = "normal"): void {
  if (!statusItem) return;
  statusItem.text = text;
  statusItem.tooltip = tooltip;
  statusItem.backgroundColor = tone === "error"
    ? new vscode.ThemeColor("statusBarItem.errorBackground")
    : tone === "warning"
      ? new vscode.ThemeColor("statusBarItem.warningBackground")
      : tone === "info"
        ? new vscode.ThemeColor("statusBarItem.prominentBackground")
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
  log(`已打开: kind=${kind} server=${server}`);
}

function currentBridgeFolder(): vscode.WorkspaceFolder | undefined {
  return (vscode.workspace.workspaceFolders ?? []).find(
    (folder) => folder.uri.scheme === SCHEME
  );
}

async function findInCurrentRemoteFolder(): Promise<void> {
  const folder = currentBridgeFolder();
  if (!folder) {
    vscode.window.showInformationMessage(
      "请先用 Bridge 打开一个远端目录，再执行受限远端搜索。"
    );
    return;
  }
  const pattern = await vscode.window.showInputBox({
    title: "胡伟团队专用智能体：在远端路径中搜索",
    prompt: `仅搜索当前目录 ${folder.uri.path}（最多两层、200 个结果）`,
    value: "*",
    validateInput: (value) =>
      /^[A-Za-z0-9*?][A-Za-z0-9*?._+-]{0,127}$/.test(value)
        ? undefined
        : "只允许文件名通配符、字母、数字、点、下划线、加号和减号。",
  });
  if (!pattern) return;
  try {
    const result = await getClient().find(
      folder.uri.authority,
      folder.uri.path,
      pattern,
      2,
      200
    );
    const selection = await vscode.window.showQuickPick(
      result.files.map((file) => ({
        label: file.path.split("/").filter(Boolean).pop() ?? file.path,
        description: file.path,
        detail: `${file.size} bytes`,
        path: file.path,
      })),
      {
        title: "远端搜索结果",
        placeHolder: result.truncated
          ? "结果已截断为前 200 项，请缩小目录或收紧通配符。"
          : `在 ${folder.uri.path} 中找到 ${result.files.length} 项`,
      }
    );
    if (selection) await openRemote(folder.uri.authority, selection.path, "file");
  } catch (err) {
    log(`远端搜索失败: ${describe(err)}`);
    vscode.window.showErrorMessage(`远端搜索失败：${describe(err)}`);
  }
}

interface WorkspaceContext {
  workspaceId?: string;
  server?: string;
  remotePath?: string;
  mode?: "read-write" | "read-only";
}

function workspaceContext(): WorkspaceContext | null {
  const value = vscode.workspace.getConfiguration("huwei-bridge")
    .get<WorkspaceContext | null>("workspace", null);
  return value?.workspaceId && value.server ? value : null;
}

function prettyBytes(value: number | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n) || n < 1024) return `${Math.max(0, n)} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024 ** 3).toFixed(1)} GiB`;
}

function workspaceLabel(status: WorkspaceStatus): {
  text: string; tooltip: string; tone: StatusTone;
} {
  const leaf = status.remote_path.split("/").filter(Boolean).pop() ?? status.server;
  const base = `胡伟团队专用智能体: ${status.server} · ${leaf}`;
  const detail = `Vlab 缓存 ${prettyBytes(status.cache_bytes)} · ` +
    `待同步 ${status.pending_sync_files} · 可用 ${prettyBytes(status.vlab_space?.free_bytes)}`;
  if (status.mode === "read-only") {
    return { text: `$(lock) ${base} · 只读`, tooltip: detail, tone: "normal" };
  }
  if (status.status === "open" && status.pending_sync_files === 0) {
    return { text: `$(check) ${base} · 已同步`, tooltip: detail, tone: "normal" };
  }
  if (status.status === "recovering" || status.uploads_in_progress > 0) {
    return { text: `$(sync~spin) ${base} · 正在上传`, tooltip: detail, tone: "info" };
  }
  if (status.status === "sync_pending" || status.pending_sync_files > 0) {
    return { text: `$(warning) ${base} · 存在未同步修改`, tooltip: detail, tone: "warning" };
  }
  return { text: `$(error) ${base} · 同步失败`,
           tooltip: `${detail}\n${status.last_error || "请点击查看详情或重试。"}`,
           tone: "error" };
}

async function refreshWorkspaceStatus(showDetails = false): Promise<void> {
  const context = workspaceContext();
  if (!context?.workspaceId) return;
  try {
    const status = await getClient().workspaceStatus(context.workspaceId);
    const label = workspaceLabel(status);
    setStatus(label.text, label.tooltip, label.tone);
    if (!statusItem) return;
    statusItem.command = "huwei-bridge.workspaceStatus";
    if (!showDetails) return;
    const detail = `${status.server} · ${status.remote_path}\n` +
      `状态：${status.status}\n缓存：${prettyBytes(status.cache_bytes)}\n` +
      `待同步：${status.pending_sync_files}\n` +
      (status.last_error ? `错误：${status.last_error}` : "");
    const action = await vscode.window.showInformationMessage(
      detail, "刷新", "关闭工作区", "重试同步"
    );
    if (action === "刷新") await refreshWorkspaceStatus(false);
    if (action === "关闭工作区") {
      await getClient().workspaceClose(status.workspace_id);
      vscode.window.showInformationMessage("工作区已安全关闭；Vlab 缓存保留，可稍后清理。");
      await refreshWorkspaceStatus(false);
    }
    if (action === "重试同步") {
      await getClient().workspaceRecover(status.workspace_id);
      await refreshWorkspaceStatus(false);
    }
  } catch (err) {
    // 只有完整工作区已声明时才显示故障，避免安全编辑模式在控制台
    // 未启动时产生误导性的启动错误。
    setStatus("$(error) 胡伟团队专用智能体: 工作区不可用", describe(err), "error");
    if (showDetails) vscode.window.showErrorMessage(`工作区状态读取失败：${describe(err)}`);
  }
}

const warnedWorkspaceFiles = new Set<string>();

async function protectFullWorkspaceEditor(editor: vscode.TextEditor | undefined): Promise<void> {
  if (!editor || !workspaceContext()) return;
  const uri = editor.document.uri;
  if (uri.scheme === SCHEME || uri.scheme === "untitled") return;
  const name = baseName(uri.path).toUpperCase();
  if (DENYLIST.has(name)) {
    vscode.window.showWarningMessage(`${name} 是受保护的 VASP 大数据文件；完整工作区默认排除它。`);
    return;
  }
  try {
    const stat = await vscode.workspace.fs.stat(uri);
    const key = uri.toString();
    if (stat.size > 256 * 1024 * 1024) {
      if (!warnedWorkspaceFiles.has(key)) {
        warnedWorkspaceFiles.add(key);
        vscode.window.showWarningMessage(
          `${baseName(uri.path)} 大于 256 MiB：已请求本会话只读。请用集群工具处理大文件。`
        );
        // 此命令由最新版 VS Code 提供；若某个发行版没有它，提示仍会保留，
        // 不会把文件自动下载、复制或静默改写。
        await vscode.commands.executeCommand(
          "workbench.action.files.setActiveEditorReadonlyInSession"
        ).then(undefined, () => undefined);
      }
    } else if (stat.size > 32 * 1024 * 1024 && !warnedWorkspaceFiles.has(key)) {
      warnedWorkspaceFiles.add(key);
      vscode.window.showWarningMessage(
        `${baseName(uri.path)} 大于 32 MiB；继续前请确认 Vlab 缓存空间。`
      );
    }
  } catch {
    // Remote-SSH 连接尚未就绪时不干扰普通编辑；状态栏会显示具体连接错误。
  }
}

export function activate(context: vscode.ExtensionContext): void {
  out = vscode.window.createOutputChannel("Huwei Bridge");
  context.subscriptions.push(out);
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
  provider.log = (msg) => log(msg);
  provider.warn = (msg) => vscode.window.showWarningMessage(msg);
  context.subscriptions.push(
    vscode.workspace.registerFileSystemProvider(SCHEME, provider, {
      isCaseSensitive: true,
    })
  );

  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri(uri: vscode.Uri) {
        let request;
        try {
          request = parseWakeQuery(uri.query);
        } catch (err) {
          log(`唤起链接不完整: ${uri.toString()}`);
          vscode.window.showErrorMessage(
            `唤起链接无效：${describe(err)}`
          );
          return;
        }
        const { server, path: remotePath, kind } = request;
        log(`唤起: server=${server} path=${remotePath} kind=${kind}`);
        setStatus("$(sync~spin) Huwei Bridge: 连接中…");
        openRemote(server, remotePath, kind).catch((err) => {
          log(`打开失败: ${describe(err)}`);
          setStatus(`$(error) Huwei Bridge: 连接失败`, describe(err), "error");
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
        setStatus("$(error) Huwei Bridge: 控制台不可用", describe(err), "error");
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
    vscode.commands.registerCommand("huwei-bridge.find", findInCurrentRemoteFolder)
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("huwei-bridge.workspaceStatus", () =>
      refreshWorkspaceStatus(true))
  );
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      void protectFullWorkspaceEditor(editor);
    })
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("huwei-bridge")) client = null;
    })
  );

  // 控制台未启动时保持安静：安全编辑等待 URI 唤起；完整工作区则刷新
  // Vlab 状态栏，但不弹出无关错误。
  if (workspaceContext()) {
    void refreshWorkspaceStatus(false);
    void protectFullWorkspaceEditor(vscode.window.activeTextEditor);
  } else {
    setStatus("$(plug) Huwei Bridge: 待唤起");
  }
}

export function deactivate(): void {}
