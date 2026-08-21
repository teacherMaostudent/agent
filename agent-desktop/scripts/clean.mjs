import { rm } from "node:fs/promises";
import path from "node:path";

// 仅清理项目内可再生的编译目录，避免旧入口或已删除测试残留进入安装包。
const projectRoot = process.cwd();
for (const directory of ["dist", "dist-electron"]) {
  const target = path.resolve(projectRoot, directory);
  if (path.dirname(target) !== projectRoot) {
    throw new Error(`拒绝清理项目目录之外的路径：${target}`);
  }
  await rm(target, { recursive: true, force: true });
}
