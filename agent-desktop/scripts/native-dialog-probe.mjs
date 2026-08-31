/** 诊断真实文件对话框：仅追踪原始函数返回，不替换返回值或绕过人工选择。 */
import { mkdtemp, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const { _electron } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const project = fileURLToPath(new URL("../", import.meta.url));
const root = await mkdtemp(path.join(os.tmpdir(), "agent-desktop-e2e-probe-"));
const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE; delete env.VITE_DEV_SERVER_URL;
const application = await _electron.launch({ executablePath: path.join(project, "release/win-unpacked/Agent Workbench.exe"),
  args: [`--user-data-dir=${root}`, "--agent-safe-mode"], env, timeout: 30_000 });
try {
  const page = await application.firstWindow();
  await application.evaluate(({ dialog, BrowserWindow }) => {
    globalThis.nativeDialogTrace = [];
    const original = dialog.showOpenDialog.bind(dialog);
    dialog.showOpenDialog = async (...args) => {
      globalThis.nativeDialogTrace.push({ event: "called" });
      try {
        const result = await original(...args);
        globalThis.nativeDialogTrace.push({ event: "returned", canceled: result.canceled, pathCount: result.filePaths.length });
        return result;
      } catch (error) {
        globalThis.nativeDialogTrace.push({ event: "error", message: String(error) });
        throw error;
      }
    };
    const window = BrowserWindow.getAllWindows()[0]; window.show(); window.focus();
  });
  await page.getByRole("button", { name: "选择本地目录", exact: true }).click();
  try {
    const text = execFileSync("powershell.exe", ["-NoProfile", "-File", path.join(project, "scripts/native-dialog.ps1"),
      "-TargetProcessId", String(application.process().pid), "-Action", "Inspect"], { timeout: 30_000, windowsHide: true, encoding: "utf8" });
    console.log(text);
    execFileSync("powershell.exe", ["-NoProfile", "-File", path.join(project, "scripts/native-dialog.ps1"),
      "-TargetProcessId", String(application.process().pid), "-Action", "Cancel"], { timeout: 30_000, windowsHide: true });
  } catch { console.log("NO_NATIVE_DIALOG_FOUND"); }
  const trace = await application.evaluate(() => globalThis.nativeDialogTrace);
  console.log(JSON.stringify(trace));
  await writeFile(path.join(root, "native-trace.json"), JSON.stringify(trace, null, 2));
  console.log("NATIVE_PROBE=" + root);
} finally { await application.close(); }
