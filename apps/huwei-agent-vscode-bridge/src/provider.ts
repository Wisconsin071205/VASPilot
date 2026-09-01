/** 虚拟文件系统提供器：按需读写，绝不做全量扫描或整包下载。 */
import * as vscode from "vscode";
import { ConsoleClient } from "./api";
import { describe } from "./errors";
import { baseName, openRefusal, sha256Hex } from "./core";

export const SCHEME = "huwei-agent-remote";

interface Baseline {
  sha: string;
  size: number;
  mtimeEpoch: number;
}

export class RemoteWorkspaceProvider implements vscode.FileSystemProvider {
  private emitter = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
  readonly onDidChangeFile = this.emitter.event;
  /** 打开（读取）文件时的基线：保存时用于冲突检测。 */
  private baselines = new Map<string, Baseline>();

  constructor(
    private getClient: () => ConsoleClient,
    private capBytes: () => number,
    private maxEntries: () => number
  ) {}

  private key(uri: vscode.Uri): string {
    return `${uri.authority}#${uri.path}`;
  }

  watch(): vscode.Disposable {
    // 远端变更不主动推送；VS Code 的刷新命令即可触发重新读取
    return new vscode.Disposable(() => {});
  }

  async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
    try {
      const st = await this.getClient().stat(uri.authority, uri.path);
      const isDir = String(st.kind ?? "").includes("dir");
      const ms = (st.mtime_epoch ?? 0) * 1000;
      return {
        type: isDir
          ? vscode.FileType.Directory
          : vscode.FileType.File,
        ctime: ms,
        mtime: ms,
        size: st.size,
      };
    } catch (err) {
      throw this.asFsError(err, uri);
    }
  }

  async readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
    try {
      const doc = await this.getClient().list(uri.authority, uri.path);
      const capped = doc.entries.slice(0, this.maxEntries());
      return capped.map((e) => [
        e.name,
        e.type === "dir" ? vscode.FileType.Directory : vscode.FileType.File,
      ]);
    } catch (err) {
      throw this.asFsError(err, uri);
    }
  }

  async readFile(uri: vscode.Uri): Promise<Uint8Array> {
    const client = this.getClient();
    const name = baseName(uri.path);
    try {
      const st = await client.stat(uri.authority, uri.path);
      const refusal = openRefusal(name, st.size ?? 0, this.capBytes());
      if (refusal) throw vscode.FileSystemError.NoPermissions(refusal);
      const doc = await client.read(uri.authority, uri.path);
      const content = Buffer.from(doc.content ?? "", "utf-8");
      this.baselines.set(this.key(uri), {
        sha: sha256Hex(content),
        size: content.byteLength,
        mtimeEpoch: st.mtime_epoch ?? 0,
      });
      return content;
    } catch (err) {
      if (err instanceof vscode.FileSystemError) throw err;
      throw this.asFsError(err, uri);
    }
  }

  async writeFile(
    uri: vscode.Uri,
    content: Uint8Array,
    _options: { create: boolean; overwrite: boolean }
  ): Promise<void> {
    const client = this.getClient();
    const key = this.key(uri);
    const expected = this.baselines.get(key)?.sha ?? "";
    try {
      const result = await client.write(
        uri.authority,
        uri.path,
        Buffer.from(content).toString("utf-8"),
        expected
      );
      this.baselines.set(key, {
        sha: result.sha256,
        size: result.size,
        mtimeEpoch: result.mtime_epoch,
      });
    } catch (err) {
      // 冲突属于权限语义：拒绝覆盖并保留远端原文件
      if (
        err instanceof Object &&
        (err as { code?: string }).code === "remote_changed"
      ) {
        throw vscode.FileSystemError.NoPermissions(describe(err));
      }
      throw this.asFsError(err, uri);
    }
  }

  async delete(uri: vscode.Uri): Promise<void> {
    try {
      await this.getClient().remove(uri.authority, uri.path);
      this.baselines.delete(this.key(uri));
      this.emitter.fire([{ type: vscode.FileChangeType.Deleted, uri }]);
    } catch (err) {
      throw this.asFsError(err, uri);
    }
  }

  async createDirectory(uri: vscode.Uri): Promise<void> {
    try {
      await this.getClient().mkdir(uri.authority, uri.path);
      this.emitter.fire([{ type: vscode.FileChangeType.Created, uri }]);
    } catch (err) {
      throw this.asFsError(err, uri);
    }
  }

  rename(): never {
    throw vscode.FileSystemError.NoPermissions(
      "虚拟工作区暂不支持重命名/移动（请在控制台或集群终端操作）"
    );
  }

  private asFsError(err: unknown, uri: vscode.Uri): vscode.FileSystemError {
    if (err instanceof vscode.FileSystemError) return err;
    const msg = describe(err);
    if (msg.includes("不存在")) {
      return vscode.FileSystemError.FileNotFound(uri);
    }
    return vscode.FileSystemError.Unavailable(msg);
  }
}
