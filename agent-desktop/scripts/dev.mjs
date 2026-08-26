import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// Do not spawn pnpm.cmd on Windows. Node.js 24 rejects some batch-file launches
// with shell=false as EINVAL, especially when the workspace path contains non-ASCII
// characters. Resolve the installed JavaScript entry points and Electron executable
// directly so argument boundaries remain intact without enabling a command shell.
const projectRoot = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const tscCli = path.join(projectRoot, "node_modules", "typescript", "bin", "tsc");
const viteCli = path.join(projectRoot, "node_modules", "vite", "bin", "vite.js");
const electronExecutable = require("electron");
const children = new Set();
let shuttingDown = false;

/** Start one development process and retain it for coordinated shutdown. */
function start(command, args, env = process.env) {
  const child = spawn(command, args, {
    cwd: projectRoot,
    env,
    stdio: "inherit",
    shell: false,
  });
  children.add(child);
  child.once("error", (error) => {
    console.error(`无法启动 ${command}:`, error);
    process.exitCode = 1;
    shutdown();
  });
  child.once("exit", () => children.delete(child));
  return child;
}

/** Wait until Vite accepts HTTP requests instead of relying on a fixed delay. */
async function waitForDevServer(url, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return;
    } catch {
      // Vite is still binding its socket; retry within the bounded startup window.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Vite 未在 ${timeoutMs / 1_000} 秒内就绪：${url}`);
}

/** Stop compiler, web server and Electron together on exit or startup failure. */
function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (!child.killed) child.kill();
  }
}

async function main() {
  start(process.execPath, [tscCli, "-p", "tsconfig.main.json", "--watch"]);
  start(process.execPath, [viteCli, "--host", "127.0.0.1"]);

  const devServerUrl = "http://127.0.0.1:5173";
  await waitForDevServer(devServerUrl);
  // Keep development Chromium data away from the installed application's profile.
  // This prevents concurrent installed/dev instances from contending for GPU and HTTP
  // cache directories, which otherwise produces Windows access-denied errors.
  const devDataRoot = path.join(projectRoot, ".electron-cache");
  const devProfile = path.join(devDataRoot, "profile");
  const devDiskCache = path.join(devDataRoot, "disk-cache");
  mkdirSync(devProfile, { recursive: true });
  mkdirSync(devDiskCache, { recursive: true });
  const electronArgs = [
    `--user-data-dir=${devProfile}`,
    `--disk-cache-dir=${devDiskCache}`,
    ".",
  ];
  if (process.platform === "win32") {
    // The main process can relaunch into this mode after a Chromium sandbox crash,
    // but a dev relaunch would detach from this supervisor and lose the Vite server.
    // Start Windows development in the same bounded compatibility mode up front;
    // context isolation remains enabled and Node integration remains disabled.
    electronArgs.push("--agent-safe-mode");
  }
  const electron = start(
    electronExecutable,
    electronArgs,
    {
      ...process.env,
      VITE_DEV_SERVER_URL: devServerUrl,
    },
  );
  electron.once("exit", (code) => {
    shutdown();
    process.exitCode = code ?? 0;
  });
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
  shutdown();
});
