import { spawn } from "node:child_process";
import process from "node:process";

const npmCommand = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
const children = [];

function start(command, args, env = process.env) {
  const child = spawn(command, args, { env, stdio: "inherit", shell: false });
  children.push(child);
  child.on("exit", (code) => {
    if (code && code !== 0) process.exitCode = code;
  });
  return child;
}

start(npmCommand, ["exec", "tsc", "-p", "tsconfig.main.json", "--watch"]);
start(npmCommand, ["exec", "vite", "--host", "127.0.0.1"]);

setTimeout(() => {
  start(npmCommand, ["exec", "electron", "."], {
    ...process.env,
    VITE_DEV_SERVER_URL: "http://127.0.0.1:5173",
  });
}, 1500);

function shutdown() {
  for (const child of children) child.kill();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
