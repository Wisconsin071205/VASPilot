/** 本地控制台客户端：唯一的数据通道（本机 → 控制台 → Vlab → 目标服务器）。 */
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export class ConsoleUnavailableError extends Error {
  constructor() {
    super(
      "本地控制台未运行或无法访问：请先启动「远端控制智能体」控制台" +
        "（桌面快捷方式），或在扩展设置 huwei-bridge.consoleUrl 填写控制台地址"
    );
  }
}

export class ApiError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

export interface ConsoleInfo {
  url: string;
  token: string;
}

export interface Entry {
  name: string;
  type: "file" | "dir";
  size: number;
  mtime?: string;
}

export interface ListResult {
  path: string;
  entries: Entry[];
  truncated?: boolean;
  limit?: number;
}

export interface StatInfo {
  path: string;
  kind?: string;
  size: number;
  mtime_epoch?: number;
}

export interface WriteResult {
  path: string;
  sha256: string;
  size: number;
  mtime_epoch: number;
}

export interface WorkspaceStatus {
  workspace_id: string;
  server: string;
  remote_path: string;
  mount_path: string;
  mode: "read-write" | "read-only";
  status: "open" | "sync_pending" | "needs_recovery" | "recovering" | "closed" | string;
  cache_bytes: number;
  pending_sync_files: number;
  uploads_in_progress: number;
  last_sync_at?: string;
  last_error?: string;
  vlab_space?: { free_bytes: number; used_percent: number };
}

export function discoveryFilePath(): string {
  return path.join(os.homedir(), ".vaspilot", "ui.json");
}

/** 读取控制台启动时写出的发现文件（url + token）。 */
export function readDiscoveryFile(homedir?: string): ConsoleInfo {
  const base = homedir ?? os.homedir();
  const p = path.join(base, ".vaspilot", "ui.json");
  let data: any;
  try {
    data = JSON.parse(fs.readFileSync(p, "utf-8"));
  } catch {
    throw new ConsoleUnavailableError();
  }
  if (!data.url || !data.token) throw new ConsoleUnavailableError();
  return {
    url: String(data.url).replace(/\/+$/, ""),
    token: String(data.token),
  };
}

type FetchLike = (url: string, init: any) => Promise<any>;

export class ConsoleClient {
  private constructor(
    readonly baseUrl: string,
    readonly token: string,
    private fetchFn: FetchLike
  ) {}

  /** explicitUrl 为扩展设置里的手动覆盖项（token 始终取发现文件）。 */
  static create(
    explicitUrl?: string | null,
    fetchFn: FetchLike = fetch,
    homedir?: string
  ): ConsoleClient {
    const info = readDiscoveryFile(homedir);
    const url = (explicitUrl ?? info.url).replace(/\/+$/, "");
    return new ConsoleClient(url, info.token, fetchFn);
  }

  async call<T>(action: string, body: Record<string, unknown>): Promise<T> {
    let res: any;
    try {
      res = await this.fetchFn(`${this.baseUrl}/api/${action}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Vaspilot-Token": this.token,
        },
        body: JSON.stringify(body),
      });
    } catch {
      throw new ConsoleUnavailableError();
    }
    const data = await res.json().catch(() => ({
      ok: false,
      error: { code: "bad_response", message: "控制台返回无法解析" },
    }));
    if (!data.ok) {
      throw new ApiError(
        data.error?.code ?? "error",
        data.error?.message ?? "未知错误"
      );
    }
    return data as T;
  }

  servers() {
    return this.call<{
      servers: Array<{ name: string; connected: boolean; remote_root: string }>;
    }>("state", {});
  }

  list(server: string, p: string, limit = 500) {
    return this.call<ListResult>("remote.list", {
      server,
      path: p,
      limit,
    });
  }

  read(server: string, p: string) {
    return this.call<{ path: string; content: string; size: number }>(
      "remote.read",
      { server, path: p }
    );
  }

  stat(server: string, p: string) {
    return this.call<StatInfo>("remote.stat", { server, path: p });
  }

  write(server: string, p: string, content: string, expectedSha: string) {
    return this.call<WriteResult>("remote.write", {
      server,
      path: p,
      content,
      expected_sha256: expectedSha,
    });
  }

  remove(server: string, p: string) {
    return this.call<{ trash_id: string }>("remote.remove", {
      server,
      path: p,
    });
  }

  mkdir(server: string, p: string) {
    return this.call<{ path: string }>("remote.mkdir", { server, path: p });
  }

  find(server: string, p: string, pattern: string, maxDepth = 2, limit = 200) {
    return this.call<{
      root: string;
      pattern: string;
      files: Array<{ path: string; size: number }>;
      truncated?: boolean;
    }>("remote.find", {
      server,
      path: p,
      pattern,
      max_depth: maxDepth,
      limit,
    });
  }

  workspaceStatus(workspaceId: string) {
    return this.call<WorkspaceStatus>("workspace.status", {
      workspace_id: workspaceId,
    });
  }

  workspaceClose(workspaceId: string) {
    return this.call<{ workspace_id: string; closed: boolean }>("workspace.close", {
      workspace_id: workspaceId,
    });
  }

  workspaceRecover(workspaceId: string) {
    return this.call<WorkspaceStatus>("workspace.recover", {
      workspace_id: workspaceId,
      action: "retry",
    });
  }
}
