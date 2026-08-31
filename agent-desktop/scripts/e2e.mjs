/**
 * Windows 真实 Electron 交互回归：运行已打包的程序、真实 preload/IPC 和本地服务。
 * 不替换 fetch/IPC，不访问已打开的用户实例。PLAYWRIGHT_MODULE 指向本机测试依赖。
 * 只在独立临时目录和 audit-desktop-* 租户创建测试数据；失败也保留截图/报告。
 */
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const { _electron } = await import(process.env.PLAYWRIGHT_MODULE
  ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright");
const project = fileURLToPath(new URL("../", import.meta.url));
const repo = path.dirname(project);
const root = await mkdtemp(path.join(os.tmpdir(), "agent-desktop-e2e-"));
const profile = path.join(root, "profile");
const tenant = "audit-desktop-" + Date.now();
const checks = [];
const errors = [];
const skipped = [];
const withoutNative = process.argv.includes("--without-native");
let application, page;
let runId = "";
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** 使用真实 Windows UI Automation 操作文件对话框，不替换 Electron dialog API。 */
async function nativeDialog(action, targetPath = "") {
  const processId = application.process().pid;
  return new Promise((resolve, reject) => execFile("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", path.join(project, "scripts", "native-dialog.ps1"), "-TargetProcessId", String(processId),
    "-Action", action, "-TargetPath", targetPath], { windowsHide: true, timeout: 30_000 },
  (error, stdout, stderr) => error ? reject(new Error(stderr || String(error))) : resolve(stdout)));
}

/** 有界轮询只读取 UI，终态错误应记录为失败，不把超时当成功。 */
async function until(predicate, milliseconds = 15_000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(250);
  }
  throw new Error("UI state did not satisfy assertion within " + milliseconds + "ms");
}

/** 单项失败保留证据，后续独立场景继续执行。 */
async function check(name, action) {
  try {
    await action();
    checks.push({ name, status: "PASS" });
    console.log("PASS " + name);
    return true;
  } catch (error) {
    checks.push({ name, status: "FAIL", error: String(error) });
    console.log("FAIL " + name + ": " + String(error).slice(0, 600));
    if (page) await page.screenshot({ path: path.join(root, name + ".png"), timeout: 5_000 }).catch(() => {});
    return false;
  }
}

/** 只为合成租户准备发布，绝不重写 demo/general-agent。 */
async function prepareAgent() {
  const spec = JSON.parse(execFileSync("py", ["-3.12", "-c",
    "import json,sys;sys.path.insert(0,'scripts');from platform_e2e import desktop_spec;print(json.dumps(desktop_spec()))"],
    { cwd: repo, encoding: "utf8" }));
  const headers = { "Content-Type": "application/json", "X-Tenant-Id": tenant,
    "X-User-Id": "desktop-test", "X-Roles": "agent-admin",
    "X-Control-Plane-Admin-Key": "local-control-plane-admin-key" };
  async function post(endpoint, body) {
    const response = await fetch("http://127.0.0.1:9002" + endpoint,
      { method: "POST", headers, body: JSON.stringify(body) });
    assert.ok(response.ok, "fixture " + endpoint + " HTTP " + response.status);
    return response.json();
  }
  await post("/v1/agents", { agent_id: "desktop-audit", spec });
  const version = await post("/v1/agents/desktop-audit/versions", {
    semantic_version: "1.0.0", change_summary: "isolated Desktop UI regression",
  });
  await post("/v1/agents/desktop-audit/releases", { version_id: version.version_id,
    environment: "local", rollout_percentage: 100, reason: "isolated Desktop UI regression" });
}

try {
  await mkdir(profile);
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.VITE_DEV_SERVER_URL;
  application = await _electron.launch({
    executablePath: path.join(project, "release", "win-unpacked", "Agent Workbench.exe"),
    args: [`--user-data-dir=${profile}`, "--agent-safe-mode"],
    env, cwd: project, timeout: 30_000,
  });
  page = await application.firstWindow();
  page.setDefaultTimeout(12_000);
  page.on("pageerror", (error) => { errors.push(String(error)); console.log("RENDERER_ERROR " + String(error).slice(0, 300)); });
  const actualProfile = await application.evaluate(({ app }) => app.getPath("userData"));
  assert.equal(path.resolve(actualProfile).toLowerCase(), path.resolve(profile).toLowerCase(),
    "refuse to write to the user's real Electron profile");
  await check("packaged-launch-and-security", async () => {
    await page.getByRole("heading", { name: "把任务交给可观察、可中断的 Agent" }).waitFor();
    const preferences = await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
    assert.equal(preferences.contextIsolation, true);
    assert.equal(preferences.nodeIntegration, false);
    assert.equal(await page.getByRole("button", { name: "开始执行", exact: true }).isDisabled(), true);
    await page.screenshot({ path: path.join(root, "01-startup.png") });
  });
  await check("invalid-runtime-visible-error", async () => {
    await page.getByLabel("地址", { exact: true }).fill("ftp://invalid.local");
    await page.getByRole("button", { name: /验证连接/ }).click();
    await until(async () => (await page.locator(".error").textContent()).includes("HTTP(S)"));
    assert.equal(await page.getByRole("button", { name: "开始执行", exact: true }).isDisabled(), true);
  });
  if (!withoutNative) await check("native-directory-cancel", async () => {
    // 原生模态框可能延迟 Chromium click 的完成通知，必须先启动窗口监听。
    await Promise.all([nativeDialog("Cancel"), page.getByRole("button", { name: "选择本地目录", exact: true }).click()]);
    assert.equal(await page.getByText(/只发送有界文件清单/).count(), 0);
  });
  if (!withoutNative) await check("native-directory-select", async () => {
    const workspace = path.join(root, "fixture-workspace");
    await mkdir(workspace);
    await writeFile(path.join(workspace, "audit.txt"), "TODO synthetic fixture only\n");
    await Promise.all([nativeDialog("Choose", workspace), page.getByRole("button", { name: "选择本地目录", exact: true }).click()]);
    await page.getByText(/fixture-workspace · 1 项/).waitFor();
  });
  await prepareAgent();
  const connected = await check("connect-real-runtime", async () => {
    await page.getByLabel("地址", { exact: true }).fill("http://127.0.0.1:8001/api/v1");
    await page.getByLabel("租户", { exact: true }).fill(tenant);
    await page.getByLabel("用户", { exact: true }).fill("desktop-test");
    await page.getByLabel("Agent ID", { exact: true }).fill("desktop-audit");
    await page.getByRole("button", { name: /验证连接/ }).click();
    await page.getByRole("button", { name: "已连接", exact: true }).waitFor();
  });
  if (connected) {
    await check("custom-task-entry", async () => {
      await page.getByRole("button", { name: /^＋ 自定义发布任务/ }).click();
      assert.equal(await page.getByLabel("自定义发布任务内容").inputValue(), "");
      await page.getByLabel("自定义发布任务内容").fill("自定义桌面任务草稿");
      assert.equal(await page.locator(".task-source").textContent(), "CUSTOM");
      assert.match(await page.locator(".composer-footer").textContent(), /desktop-audit.*local/);
    });
    await check("task-template-selection", async () => {
      await page.getByRole("button", { name: /^证据型研究报告/ }).click();
      assert.match(await page.locator("textarea").inputValue(), /证据 ID/);
      await page.getByRole("button", { name: /^工作区整理预案/ }).click();
      assert.match(await page.locator("textarea").inputValue(), /不得直接修改文件/);
    });
    await check("submit-real-task-and-timeline", async () => {
      await page.locator("textarea").fill("桌面交互测试：直接回答 2+2 的结果，不调用工具或知识库。");
      await page.getByRole("button", { name: "开始执行", exact: true }).click();
      await until(async () => (await page.locator(".status").textContent()) === "COMPLETED", 180_000);
      runId = await page.locator(".result dd").first().textContent();
      assert.match(runId, /^run_/);
      assert.match(await page.locator(".result > pre").textContent(), /4/);
      assert.ok(await page.locator(".timeline article").count() > 0);
    });
    if (runId) {
      await check("all-eight-report-disclosures", async () => {
        const details = page.locator("details.fact-details");
        assert.equal(await details.count(), 8);
        for (let index = 0; index < 8; index++) {
          const item = details.nth(index);
          assert.equal(await item.locator("pre").count(), 0);
          await item.locator("summary").click();
          await item.locator("pre").waitFor();
          JSON.parse(await item.locator("pre").textContent());
          await item.locator("summary").click();
          await until(async () => await item.locator("pre").count() === 0);
        }
      });
      await check("feedback-persisted-and-redacted", async () => {
        await page.getByPlaceholder("可选：失败原因或改进建议（本地保存并自动脱敏）").fill("UI audit fixture sk-synthetic_test_only_123456789");
        await page.getByRole("button", { name: "有帮助", exact: true }).click();
        await page.getByText("反馈已记录，可用于下一轮回归集。").waitFor();
        const saved = await readFile(path.join(profile, "feedback", "feedback.jsonl"), "utf8");
        assert.ok(saved.includes(runId) && saved.includes("[REDACTED_API_KEY]"));
        assert.ok(!saved.includes("sk-synthetic_test_only_123456789"));
      });
      if (!withoutNative) await check("native-export-cancel", async () => {
        await Promise.all([nativeDialog("Cancel"), page.getByRole("button", { name: "导出本次运行诊断包", exact: true }).click()]);
        assert.equal(await page.getByText(/^已导出：/).count(), 0);
      });
      if (!withoutNative) await check("native-export-write-and-verify", async () => {
        const destination = path.join(root, "diagnostic-export.json");
        await Promise.all([nativeDialog("Choose", destination), page.getByRole("button", { name: "导出本次运行诊断包", exact: true }).click()]);
        await page.getByText(/^已导出：/).waitFor();
        const saved = JSON.parse(await readFile(destination, "utf8"));
        assert.equal(saved.run.run_id, runId);
        assert.equal(saved.schemaVersion, "desktop-run-export/v1");
        assert.ok(saved.events.length > 0);
      });
      await check("terminal-controls-absent", async () => {
        assert.equal(await page.getByRole("button", { name: "取消任务", exact: true }).count(), 0);
        assert.equal(await page.getByPlaceholder("在下一安全边界补充或修正任务").count(), 0);
      });
      await check("restore-history-after-renderer-reload", async () => {
        await page.reload();
        await page.getByLabel("租户", { exact: true }).fill(tenant);
        await page.getByLabel("用户", { exact: true }).fill("desktop-test");
        await page.getByRole("button", { name: /验证连接/ }).click();
        await page.getByRole("button", { name: "已连接", exact: true }).waitFor();
        await page.getByRole("button", { name: new RegExp("COMPLETED · desktop-audit") }).click();
        await page.locator("details.fact-details").first().waitFor();
        assert.equal(await page.locator(".result dd").first().textContent(), runId);
        assert.equal(await page.locator("details.fact-details[open]").count(), 0);
      });
    }
    await check("connector-pair-confirm-revoke", async () => {
      await page.getByRole("button", { name: "生成配对码", exact: true }).click();
      const code = page.locator("aside code");
      await code.waitFor();
      await page.getByLabel("配对码", { exact: true }).fill(await code.textContent());
      await page.getByRole("button", { name: "确认配对", exact: true }).click();
      await page.getByRole("button", { name: "撤销 Connector", exact: true }).click();
      await page.getByText("Connector 已撤销，原配对关系不可恢复。").waitFor();
    });
    if (process.argv.includes("--with-tool")) await check("runtime-dispatched-controlled-scan", async () => {
      const previous = await page.locator(".result dd").first().textContent();
      await page.getByLabel("Agent ID", { exact: true }).fill("desktop-audit");
      await page.locator("textarea").fill("桌面工具测试：调用 controlled_scan 工具，在 scope=workspace 中按字面搜索 TODO。必须实际调用工具后报告匹配数；不要检索知识库。");
      await page.getByRole("button", { name: "开始执行", exact: true }).click();
      await until(async () => (await page.locator(".result dd").first().textContent()) !== previous);
      await until(async () => (await page.locator(".status").textContent()) === "COMPLETED", 180_000);
      const details = page.locator("details.fact-details").nth(3);
      await details.locator("summary").click();
      await details.locator("pre").waitFor();
      const facts = JSON.parse(await details.locator("pre").textContent());
      assert.ok(facts.results.some((item) => item.tool === "controlled_scan" && item.success === true));
      assert.ok(facts.dispatches.length > 0);
      await details.locator("summary").click();
      runId = await page.locator(".result dd").first().textContent();
    });
    await check("missing-release-visible-error", async () => {
      await page.getByLabel("Agent ID", { exact: true }).fill("missing-desktop-agent");
      await page.locator("textarea").fill("桌面测试不存在的发布");
      await page.getByRole("button", { name: "开始执行", exact: true }).click();
      await until(async () => (await page.locator(".error").textContent()).includes("agent_release_not_found"));
      if (runId) assert.equal(await page.locator(".result dd").first().textContent(), runId);
    });
    await check("real-steering-then-cancel", async () => {
      await page.getByLabel("Agent ID", { exact: true }).fill("desktop-audit");
      await page.locator("textarea").fill("桌面取消测试：分三步解释分布式队列的可靠性，给出详细对比，不调用任何工具。");
      await page.getByRole("button", { name: "开始执行", exact: true }).click();
      await until(async () => (await page.locator(".status").textContent()) === "RUNNING", 30_000);
      const steering = page.getByPlaceholder("在下一安全边界补充或修正任务");
      await steering.fill("这是测试补充指令：最终只需要一句话。");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await until(async () => await steering.inputValue() === "");
      await page.getByRole("button", { name: "取消任务", exact: true }).click();
      await until(async () => (await page.locator(".status").textContent()) === "CANCELLED", 90_000);
      assert.equal(await page.getByRole("button", { name: "取消任务", exact: true }).count(), 0);
    });
    await check("empty-task-disabled", async () => {
      await page.locator("textarea").fill("   ");
      assert.equal(await page.getByRole("button", { name: "开始执行", exact: true }).isDisabled(), true);
    });
  }
} catch (error) {
  checks.push({ name: "setup-or-unhandled", status: "FAIL", error: String(error) });
  console.log("FAIL setup-or-unhandled: " + String(error));
} finally {
  if (application) await application.close().catch(() => {});
  if (withoutNative) skipped.push("native-directory-cancel", "native-directory-select", "native-export-cancel", "native-export-write-and-verify");
  if (!process.argv.includes("--with-tool")) skipped.push("runtime-dispatched-controlled-scan");
  await writeFile(path.join(root, "report.json"), JSON.stringify({ tenant, root, runId, checks, skipped, pageErrors: errors,
    approval: "not yet tested", sandbox: "compatibility mode" }, null, 2));
  console.log("DESKTOP_E2E_ARTIFACTS=" + root);
  console.log("DESKTOP_E2E=" + checks.filter((item) => item.status === "PASS").length + "/" + checks.length);
  console.log("SKIPPED=" + skipped.join(","));
  console.log("NOT_COVERED=approval/resume,local-connector-scan-to-RAG,production-OIDC,sandbox-enforced mode");
  if (checks.some((item) => item.status === "FAIL")) process.exitCode = 1;
}
