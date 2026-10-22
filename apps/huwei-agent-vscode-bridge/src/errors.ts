/** 把底层错误翻译成用户可读的中文提示。 */
import { ApiError, ConsoleUnavailableError } from "./api";

export function describe(err: unknown): string {
  if (err instanceof ConsoleUnavailableError) return err.message;
  if (err instanceof ApiError) {
    switch (err.code) {
      case "auth_required":
        return "服务器会话未连接：请在「远端控制智能体」控制台左侧" +
          "点击该服务器的「连接」并输入密码后重试";
      case "remote_changed":
        return "远端文件已被其他操作修改，请比较后再保存（本次保存已拒绝覆盖）";
      case "text_denylist":
        return "该文件类型默认禁止文本编辑（WAVECAR/CHGCAR/AECCAR 等二进制数据）";
      case "outside_root":
        return "路径超出该服务器允许访问的根目录范围";
      case "not_found":
        return "远端文件或目录不存在（可能已被移动或删除）";
      default:
        return err.message;
    }
  }
  const msg = (err as Error)?.message ?? String(err);
  return msg;
}
