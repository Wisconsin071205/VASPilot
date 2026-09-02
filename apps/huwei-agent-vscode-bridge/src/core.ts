/** 与 VS Code API 无关的纯逻辑：供自动化测试与运行时共用。 */
import { createHash } from "crypto";

export const SCHEME = "huwei-agent-remote";

/** 打开/写入时默认拒绝的二进制数据文件（与网关 TEXT_DENYLIST 一致） */
export const DENYLIST = new Set(["WAVECAR", "CHGCAR", "AECCAR0", "AECCAR2"]);

export function sha256Hex(data: Buffer | string): string {
  return createHash("sha256").update(data).digest("hex");
}

export function baseName(p: string): string {
  const parts = p.split("/").filter((x) => x.length > 0);
  return parts.length ? parts[parts.length - 1] : "";
}

/**
 * 判定一个文件是否允许以文本方式打开。
 * @returns 拒绝原因；null 表示允许。
 */
export function openRefusal(
  name: string,
  size: number,
  capBytes: number
): string | null {
  if (DENYLIST.has(name.toUpperCase())) {
    return (
      `${name} 属于二进制数据文件（WAVECAR/CHGCAR/AECCAR 类），` +
      "默认禁止以文本方式打开或编辑"
    );
  }
  if (size > capBytes) {
    const mb = (n: number) => (n / 1048576).toFixed(1);
    return (
      `文件大小 ${mb(size)} MiB 超过上限 ${mb(capBytes)} MiB，` +
      "按需读取策略已阻止下载（如确需查看请到集群终端处理）"
    );
  }
  return null;
}

/** 目录条目按需读取上限：只截取，绝不递归展开。 */
export function sliceEntries<T>(entries: T[], max: number): T[] {
  return entries.slice(0, Math.max(1, Math.floor(max)));
}

/** 唤起链接（由网页控制台与测试共用同一格式）。 */
export function wakeUri(server: string, p: string, kind: "file" | "folder") {
  const q = new URLSearchParams({
    server,
    path: p,
    kind,
  });
  return `vscode://huwei-team.huwei-agent-vscode-bridge/open?${q.toString()}`;
}

/** URI Handler 的输入校验；保持为纯函数，避免误把不完整链接打开成目录。 */
export interface WakeRequest {
  server: string;
  path: string;
  kind: "file" | "folder";
}

export function parseWakeQuery(query: string): WakeRequest {
  const q = new URLSearchParams(query);
  const server = q.get("server") ?? "";
  const path = q.get("path") ?? "";
  const kind = q.get("kind") ?? "";
  if (!server || !path.startsWith("/") || (kind !== "file" && kind !== "folder")) {
    throw new Error("唤起链接不完整或 kind 无效：需要 server、绝对 path 和 file/folder");
  }
  return { server, path, kind };
}
