const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");
const root = path.join(__dirname, "../public");
const script = fs.readFileSync(path.join(root, "app.js"), "utf8");

// 使用真实页面和脚本，仅替换 HTTP 边界；检查弹窗复用而非只断言 HTML 字符串。
test("task disclosures survive same-run refresh and reset when selecting another run", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => ({
    ok: true, status: 200, json: async () => {
      if (url === "/api/session") return { authentication: "oidc", user_id: "test", tenant_id: "test", roles: [], permissions: [] };
      if (url.includes("?limit=")) return { items: [] };
      return {
        run_id: url.split("/").at(-1), status: "COMPLETED", can_control: false,
        answer: "fixture", citations: [], artifacts: [],
        execution: { plan: { intent: { name: "test" } }, timeline: [] },
      };
    },
  });
  try {
    win.eval(script);
    await new Promise(setImmediate);
    await win.openTask("run-a");
    const host = win.document.querySelector("#task-detail");
    const count = host.querySelectorAll("details[data-detail-key]").length;
    assert.equal(count, 5);
    assert.equal(host.querySelector("details[open]"), null);
    assert.match(host.querySelector(".evidence-detail summary").textContent, /0 条引用/);
    host.querySelector('[data-detail-key="planner"]').open = true;
    await win.openTask("run-a");
    assert.equal(host.querySelector('[data-detail-key="planner"]').open, true);
    await win.openTask("run-b");
    assert.equal(host.querySelectorAll("details[data-detail-key]").length, count);
    assert.equal(host.querySelector("details[open]"), null);
    win.compactExecutionDetails();
    assert.equal(host.querySelectorAll("details[data-detail-key]").length, count);
  } finally { win.close(); }
});

test("concurrent unauthorized requests start exactly one relogin navigation", async () => {
  let navigations = 0;
  const code = script.split("\n").filter(line =>
    /^(const localSignedOut=|const localProfiles=|function localHeaders\(|const state=|function headers\(|function sessionRedirectError\(|async function api\()/.test(line),
  ).join("\n");
  const sandbox = {
    URLSearchParams,
    window: { location: { search: "", replace: (url) => { assert.equal(url, "/auth/relogin"); navigations++; } } },
    fetch: async () => ({ ok: false, status: 401, json: async () => ({ detail: "expired" }) }),
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  const results = await Promise.allSettled([sandbox.api("/api/session"), sandbox.api("/api/workspace/runs")]);
  assert.equal(navigations, 1);
  assert.ok(results.every(result => result.reason?.name === "SessionRedirectError"));
});

test("navigation and account controls follow the verified account permissions", async () => {
  const scenarios = [
    { name: "business", roles: ["agent-user"], permissions: [], review: false, console: false, identity: false },
    { name: "reviewer", roles: ["agent-reviewer"], permissions: ["agent:review"], review: true, console: false, identity: false },
    {
      name: "administrator", roles: ["platform-super-admin"],
      permissions: ["agent:review", "ops:read", "identity:users:read", "identity:users:write"],
      review: true, console: true, identity: true,
    },
  ];
  for (const scenario of scenarios) {
    const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
      url: "http://localhost/", runScripts: "outside-only",
    });
    const win = dom.window;
    win.setInterval = () => 0;
    win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    win.fetch = async (url) => ({
      ok: true, status: 200, json: async () => {
        if (url === "/api/session") return {
          authentication: "oidc", user_id: scenario.name, tenant_id: "demo",
          roles: scenario.roles, permissions: scenario.permissions,
        };
        if (url === "/api/console/identity/users") return { items: [], roles: [], permissions: [] };
        return { items: [] };
      },
    });
    try {
      win.eval(script);
      await new Promise(setImmediate);
      assert.equal(win.document.querySelector('[data-view="review"]').hidden, !scenario.review);
      assert.equal(win.document.querySelector('[data-view="console"]').hidden, !scenario.console);
      assert.equal(win.document.querySelector('[data-view="identity"]').hidden, !scenario.identity);
      assert.equal(win.document.querySelector("#logout-button").hidden, false);
      assert.equal(win.document.querySelector("#switch-account-button").hidden, false);
      assert.match(win.document.querySelector("#identity-summary").textContent, new RegExp(scenario.name));
      assert.doesNotMatch(win.document.querySelector("#access-summary").textContent, /受限|禁止/);
    } finally { win.close(); }
  }
});

test("account panel discloses the active tenant, role, workspaces and effective permissions", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => ({ ok: true, status: 200, json: async () => {
    if (url === "/api/session") return {
      authentication: "oidc", user_id: "admin-subject", username: "admin", tenant_id: "demo",
      roles: ["platform-super-admin"], permissions: ["ops:read", "identity:users:read", "tenant:read"],
    };
    return { items: [], total_items: 0, limit: 8 };
  }});
  try {
    win.eval(script);
    await new Promise(setImmediate);
    win.document.querySelector("#account-button").click();
    assert.equal(win.document.querySelector("#account-dialog").open, true);
    assert.match(win.document.querySelector("#account-details").textContent, /admin/);
    assert.match(win.document.querySelector("#account-permission-list").textContent, /identity:users:read/);
    assert.equal(win.document.querySelector("#account-open-identity").hidden, false);
  } finally { win.close(); }
});

test("workspace stays compact and tenant administration mounts outside the console", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  const requests = [];
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => {
    requests.push(String(url));
    return {
      ok: true, status: 200, json: async () => {
        if (url === "/api/session") return {
          authentication: "oidc", user_id: "administrator", username: "admin", tenant_id: "demo",
          roles: ["platform-super-admin"], permissions: ["ops:read", "tenant:read", "identity:users:read"],
        };
        if (url === "/api/console/tenants") return { items: [{ tenant_id: "demo", status: "active" }] };
        if (url === "/api/console/identity/users") return { items: [], roles: [], permissions: [] };
        return { items: [] };
      },
    };
  };
  try {
    win.eval(script);
    await new Promise(setImmediate);
    assert.ok(requests.includes("/api/workspace/runs?limit=8&page=1"));
    assert.equal(win.document.querySelector("#tenant-catalog-section").parentElement.id, "identity-view");
    assert.equal(win.document.querySelector("#identity-users-section").parentElement.id, "identity-view");
    win.showView("identity");
    await new Promise(setImmediate);
    assert.equal(win.document.querySelector("#console-view").hidden, true);
    assert.equal(win.document.querySelector("#identity-view").hidden, false);
    assert.equal(win.document.querySelector("#new-task-button").hidden, true);
  } finally { win.close(); }
});

test("Workspace paginates authorized history and reuses one tenant form for create and edit", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  const requests = [];
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => {
    requests.push(String(url));
    return {
      ok: true, status: 200, json: async () => {
        if (url === "/api/session") return {
          authentication: "oidc", user_id: "administrator", username: "admin", tenant_id: "demo",
          roles: ["platform-super-admin"], permissions: ["ops:read", "tenant:read", "tenant:write", "identity:users:read"],
        };
        if (url.startsWith("/api/workspace/runs")) return {
          scope: "owned-or-shared", limit: 8, total_items: 19,
          items: [{ run_id: "run-page", agent_id: "general-agent", status: "COMPLETED", updated_at: new Date().toISOString(), summary: {} }],
        };
        if (url === "/api/console/tenants") return { items: [{ tenant_id: "demo", display_name: "Demo", data_region: "local", status: "active", created_by: "admin" }] };
        if (url === "/api/console/identity/users") return { items: [], roles: [], permissions: [] };
        return { items: [] };
      },
    };
  };
  try {
    win.eval(script);
    await new Promise(setImmediate);
    assert.equal(win.document.querySelector("#workspace-pagination").hidden, false);
    assert.deepEqual([...win.document.querySelectorAll(".pagination-button")].map((node) => node.textContent), ["上一页", "1", "2", "3", "下一页"]);
    await win.loadTasks(2);
    assert.ok(requests.includes("/api/workspace/runs?limit=8&page=2"));
    win.openTenantForm({ tenant_id: "demo", display_name: "Demo", data_region: "local", status: "active" });
    assert.equal(win.document.querySelector("#tenant-form-mode").value, "edit");
    assert.equal(win.document.querySelector("#tenant-form-id").disabled, true);
    assert.equal(win.document.querySelector("#tenant-form-reason-field").hidden, false);
    win.openTenantForm();
    assert.equal(win.document.querySelector("#tenant-form-mode").value, "create");
    assert.equal(win.document.querySelector("#tenant-form-id").disabled, false);
    assert.equal(win.document.querySelector("#tenant-form-reason-field").hidden, true);
  } finally { win.close(); }
});

test("release catalog lists semantic Versions and carries the selected opaque ID into Release creation", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => ({
    ok: true, status: 200, json: async () => {
      if (url === "/api/session") return {
        authentication: "oidc", user_id: "administrator", username: "admin", tenant_id: "demo",
        roles: ["platform-super-admin"], permissions: ["ops:read", "release:read", "release:create"],
      };
      if (url === "/api/console/agents?limit=8&page=1") return {
        items: [{ agent_id: "general-agent", revision: 7 }], total_items: 1, limit: 8,
      };
      if (url === "/api/console/agents/general-agent/versions") return { items: [{
        version_id: "ver_123", semantic_version: "1.2.3", source_revision: 7, change_summary: "safe update",
      }] };
      if (url === "/api/console/agents/general-agent/releases") return { items: [] };
      return { items: [] };
    },
  });
  try {
    win.eval(script);
    await new Promise(setImmediate);
    win.showView("console");
    await new Promise(setImmediate);
    assert.match(win.document.querySelector("#release-catalog-list").textContent, /v1\.2\.3/);
    const button = [...win.document.querySelectorAll("#release-catalog-list button")]
      .find((node) => node.textContent === "用于创建 Release");
    assert.ok(button);
    button.click();
    assert.equal(win.document.querySelector("#release-agent-id").value, "general-agent");
    assert.equal(win.document.querySelector("#release-version-id").value, "ver_123");
  } finally { win.close(); }
});

test("release controls use compact field grids instead of vertically centred equal-height cards", () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"));
  try {
    const forms = [...dom.window.document.querySelectorAll("#release-operations-section .operation-card")];
    assert.equal(forms.length, 3);
    assert.ok(forms.every((form) => form.querySelector(":scope > .operation-fields")));
    assert.match(fs.readFileSync(path.join(root, "styles.css"), "utf8"), /\.operation-fields\{display:grid/);
    assert.match(fs.readFileSync(path.join(root, "styles.css"), "utf8"), /\.service-list\{[^}]*align-items:start/);
  } finally { dom.window.close(); }
});

test("only a permitted manager can open the tenant-scoped reviewer picker", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => ({
    ok: true, status: 200, json: async () => {
      if (url === "/api/session") return {
        authentication: "oidc", user_id: "manager-a", username: "manager", tenant_id: "demo",
        roles: ["platform-operator"], permissions: ["run:review:assign"],
      };
      if (url === "/api/workspace/reviewers") return { items: [{ user_id: "reviewer-a", username: "expert" }] };
      return { items: [] };
    },
  });
  try {
    win.eval(script);
    await new Promise(setImmediate);
    await win.openReviewAssignmentDialog();
    const select = win.document.querySelector("#workspace-reviewer-select");
    assert.equal(select.options.length, 2);
    assert.equal(select.options[1].value, "reviewer-a");
    assert.match(select.options[1].textContent, /expert/);
    assert.equal(win.document.querySelector("#workspace-review-assignment-dialog").open, true);
  } finally { win.close(); }
});

// 验证 Wiki 不是只存在于后端接口：具备知识审查权限的专家能够看到候选、打开审批门禁，
// 普通业务用户则不会收到或渲染该管理能力。真正的授权仍由 BFF 和 Wiki 服务重复校验。
test("knowledge reviewer sees governed Wiki candidates and opens the human gate", async () => {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost/", runScripts: "outside-only",
  });
  const win = dom.window;
  const requests = [];
  win.setInterval = () => 0;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.fetch = async (url) => {
    requests.push(String(url));
    return {
      ok: true, status: 200, json: async () => {
        if (url === "/api/session") return {
          authentication: "oidc", user_id: "expert-a", tenant_id: "demo",
          roles: ["agent-reviewer", "knowledge-reviewer"],
          permissions: ["agent:review", "knowledge:review", "knowledge:compile"],
        };
        if (String(url).startsWith("/api/review/wiki/candidates")) return [{
          candidate_id: "wiki-candidate-1", status: "pending_review", root_task_id: "run-1",
          conclusion: "退款规则结论", sources: [{ level: "raw_evidence" }],
          drafts: [{ title: "退款规则", summary: "经证据支持的规则候选" }],
        }];
        return { items: [] };
      },
    };
  };
  try {
    win.eval(script);
    await new Promise(setImmediate);
    win.showView("review");
    await new Promise(setImmediate);
    assert.ok(requests.some((url) => url.startsWith("/api/review/wiki/candidates")));
    assert.equal(win.document.querySelector("#wiki-review-section").hidden, false);
    assert.match(win.document.querySelector("#wiki-candidate-list").textContent, /退款规则/);
    win.document.querySelector("#wiki-candidate-list button").click();
    assert.equal(win.document.querySelector("#wiki-review-dialog").open, true);
    assert.match(win.document.querySelector("#wiki-review-summary").textContent, /wiki-candidate-1/);
  } finally { win.close(); }
});

test("local identity mode does not present an editable IdP directory", () => {
  const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
  assert.match(source, /用户目录尚未启用/);
  assert.match(source, /identity_management_available/);
});
