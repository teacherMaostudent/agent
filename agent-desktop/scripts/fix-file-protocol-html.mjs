import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

// Vite adds `crossorigin` to generated asset tags. That is correct for HTTP(S),
// but Electron loads the packaged renderer from file://; Chromium then rejects the
// stylesheet as a CORS fetch and renders the functional React page without its CSS.
const indexPath = path.join(process.cwd(), "dist", "index.html");
const html = await readFile(indexPath, "utf8");
const normalized = html.replace(/\s+crossorigin(?=\s+(?:href|src)=)/g, "");

if (normalized === html) {
  throw new Error("未在桌面端入口中找到 Vite 的 crossorigin 资源属性");
}

await writeFile(indexPath, normalized, "utf8");
