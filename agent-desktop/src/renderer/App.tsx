import { useEffect, useMemo, useState } from "react";
import type { ModelRouteCatalog, RunHistoryItem, RuntimeEvent, RunSnapshot, WorkspacePreview } from "../shared/contracts";
import { browserPreviewApi } from "./browser-preview";
import { FactDetails } from "./FactDetails";

const templates = [
  { id: "scan", title: "源码与日志扫描", task: "使用 controlled_scan 在白名单 scope=workspace 中查找 TODO、异常处理缺口和潜在敏感信息，输出按严重度排序并带文件与行号的报告。" },
  { id: "research", title: "证据型研究报告", task: "围绕当前问题检索知识库，区分事实、推断和未知项，输出每项结论对应的证据 ID，并明确证据不足之处。" },
  { id: "organize", title: "工作区整理预案", task: "根据工作区清单生成安全的文件整理预案。不得直接修改文件；列出拟移动项、目标目录、冲突风险和可回滚步骤，等待人工批准。" },
];

// 拒绝与预算耗尽同样是终态，不再轮询或开放 Steering。
const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED", "LIMIT_EXCEEDED", "REJECTED"]);
const desktopApi = window.agentDesktop ?? browserPreviewApi;

/**
 * 任务尚未开始时也明确列出可审计范围；只有真实 Run 产生后才会填入实际数据，
 * 因而不会把“支持某能力”错误表达为“本次已经执行过”。
 */
const observabilityChecklist = [
  ["01", "Planner 计划", "意图、实体、复杂度、SLA、预算与路由"],
  ["02", "Harness / Executor", "已选择的执行器与发布约束"],
  ["03", "RAG / Evidence", "实际文档、Evidence ID 与降级事实"],
  ["04", "模型路由", "模型、路由版本、修订版与回退链"],
  ["05", "受控扫描", "controlled_scan 是否真的执行"],
  ["06", "工具安全", "参数、权限、准入与执行结果"],
  ["07", "成本账本", "Token、延迟、支出与剩余预算"],
  ["08", "人工审批", "触发原因、批准或拒绝记录"],
  ["09", "恢复与控制", "Steering、取消、恢复入口与状态"],
  ["10", "Governance 审计", "已写入的不可变审计事件"],
  ["11", "Context 历史", "选中消息的角色、时间与内容哈希"],
  ["12", "Release / Snapshot", "Agent 发布版本与冻结执行快照"],
] as const;

/** 将账本事件名翻译成操作者可判断的阶段；原始标识仍在时间线中保留。 */
const eventLabels: Record<string, string> = {
  "runtime.run.started": "运行已创建", "runtime.user.message": "收到任务",
  "runtime.context.injected": "上下文已装配", "runtime.plan.admitted": "执行计划已准入",
  "runtime.retrieval.completed": "知识检索完成", "runtime.model.requested": "模型决策请求",
  "runtime.tool.requested": "受控工具请求", "runtime.tool.completed": "受控工具完成",
  "runtime.run.state_changed": "运行状态变更",
};

/** 客户端仅投影 Runtime 的受控结果，不在界面上重新解释或补造决策。 */
function resultRecord(run: RunSnapshot | null): Record<string, unknown> {
  return run?.result && typeof run.result === "object" ? run.result : {};
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
}

function shortJson(value: unknown): string {
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function matchingEvents(events: RuntimeEvent[], eventType: string): RuntimeEvent[] {
  return events.filter((event) => event.event_type === eventType);
}

export function App() {
  const [baseUrl, setBaseUrl] = useState("http://127.0.0.1:8001/api/v1");
  const [tenant, setTenant] = useState("demo");
  const [user, setUser] = useState("desktop-user");
  const [permissions, setPermissions] = useState("rag:read,file:scan,tool:invoke,connector:pair,connector:grant,connector:revoke");
  const [token, setToken] = useState("");
  const [connected, setConnected] = useState(false);
  const [task, setTask] = useState(templates[0].task);
  const [taskSource, setTaskSource] = useState<"custom" | string>(templates[0].id);
  const [agentId, setAgentId] = useState("general-agent");
  const [environment, setEnvironment] = useState("local");
  const [taskSessionId, setTaskSessionId] = useState("");
  const [modelCatalog, setModelCatalog] = useState<ModelRouteCatalog | null>(null);
  const [modelRoute, setModelRoute] = useState("");
  const [workspace, setWorkspace] = useState<WorkspacePreview | null>(null);
  const [run, setRun] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [steering, setSteering] = useState("");
  const [feedbackNote, setFeedbackNote] = useState("");
  const [feedbackSaved, setFeedbackSaved] = useState(false);
  const [capabilities, setCapabilities] = useState<Record<string, unknown> | null>(null);
  const [history, setHistory] = useState<RunHistoryItem[]>([]);
  const [exportedPath, setExportedPath] = useState("");
  const [auditEvents, setAuditEvents] = useState<Record<string, unknown>[]>([]);
  const [auditStatus, setAuditStatus] = useState("not_requested");
  const [connectorId, setConnectorId] = useState("");
  const [pairingCode, setPairingCode] = useState("");
  const [confirmCode, setConfirmCode] = useState("");
  const [connector, setConnector] = useState<Record<string, unknown> | null>(null);
  const [connectorMessage, setConnectorMessage] = useState("");
  const [connectorTasks, setConnectorTasks] = useState<Record<string, unknown>[]>([]);

  useEffect(() => desktopApi.onRuntimeEvent((runId, event) => {
    if (runId === run?.run_id) setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
  }), [run?.run_id]);

  useEffect(() => desktopApi.onRuntimeError((runId, message) => {
    if (runId === run?.run_id && !terminal.has(run.status)) setError(message);
  }), [run]);

  useEffect(() => {
    if (!connected || !agentId.trim() || !environment.trim()) return;
    // Reusing this ID for submit pins the same Release that populated the dropdown.  A later
    // canary update therefore cannot silently switch the task to a different Snapshot.
    const sessionId = `desktop_${crypto.randomUUID().replaceAll("-", "")}`;
    let active = true;
    setTaskSessionId(sessionId);
    setModelCatalog(null);
    setModelRoute("");
    void desktopApi.modelRoutes(agentId, environment, sessionId).then((catalog) => {
      if (!active) return;
      setModelCatalog(catalog);
      setModelRoute(catalog.default_route || catalog.items[0]?.route_name || "");
    }).catch((reason) => {
      if (active) setError(`无法读取已发布模型：${String(reason)}`);
    });
    return () => { active = false; };
  }, [connected, agentId, environment]);

  useEffect(() => {
    if (!run || terminal.has(run.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const current = await desktopApi.getRun(run.run_id);
        setRun(current.result && Object.keys(current.result).length ? { ...current, ...current.result } : current);
      } catch (reason) { setError(String(reason)); }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.status]);

  useEffect(() => {
    if (!connected || !connectorId || connector?.status !== "CONNECTED") return;
    const heartbeat = async () => {
      try {
        await desktopApi.heartbeatConnector(connectorId);
        setConnector(await desktopApi.connectorStatus(connectorId));
      } catch (reason) {
        setConnectorMessage(`Connector 心跳失败：${String(reason)}`);
      }
    };
    void heartbeat();
    const timer = window.setInterval(() => void heartbeat(), 30_000);
    return () => window.clearInterval(timer);
  }, [connected, connector?.status, connectorId]);

  useEffect(() => {
    if (!connected || !connectorId || connectorTasks.length === 0) return;
    const refreshDelivery = async () => {
      const refreshed = await Promise.all(connectorTasks.map(async (item) => {
        if (!item.task_id || item.ui_status === "AWAITING_CONFIRMATION" || item.ui_status === "EXECUTING") return item;
        try {
          return { ...item, ...(await desktopApi.connectorTaskStatus(connectorId, String(item.task_id))) };
        } catch {
          return item;
        }
      }));
      setConnectorTasks(refreshed);
    };
    void refreshDelivery();
    const timer = window.setInterval(() => void refreshDelivery(), 5_000);
    return () => window.clearInterval(timer);
  }, [connected, connectorId, connectorTasks.length]);

  async function executeClaimedConnectorTask(item: Record<string, unknown>) {
    const taskId = String(item.task_id || "");
    if (!taskId || !window.confirm(
      `确认允许本机执行 ${String(item.tool_name || "受控工具")}？\n\n` +
      `Run: ${String(item.run_id || "")}\n该操作只读取已选择的工作区，并上传脱敏、限长结果。`,
    )) return;
    setConnectorTasks((current) => current.map((candidate) =>
      candidate.task_id === item.task_id ? { ...candidate, ui_status: "EXECUTING" } : candidate
    ));
    try {
      const completed = await desktopApi.executeConnectorTask(connectorId, taskId);
      setConnectorTasks((current) => current.map((candidate) =>
        candidate.task_id === item.task_id ? { ...candidate, ...completed, ui_status: "COMPLETED" } : candidate
      ));
      setConnectorMessage(
        `本机任务已审计：扫描 ${String(completed.files_scanned || 0)} 个文件，` +
        `发现 ${String(completed.finding_count || 0)} 项；工件交付 ${String(completed.artifact_delivery_status || "PENDING")}。`,
      );
    } catch (reason) {
      setConnectorTasks((current) => current.map((candidate) =>
        candidate.task_id === item.task_id ? { ...candidate, ui_status: "FAILED", ui_error: String(reason) } : candidate
      ));
      setConnectorMessage(`本机任务未执行：${String(reason)}`);
    }
  }

  useEffect(() => {
    if (!connected || !connectorId || connector?.status !== "CONNECTED") return;
    const claim = async () => {
      try {
        const item = await desktopApi.claimConnectorTask(connectorId);
        if (item) setConnectorTasks((current) => current.some((task) => task.task_id === item.task_id) ? current : [{ ...item, ui_status: "AWAITING_CONFIRMATION" }, ...current].slice(0, 10));
      } catch (reason) {
        setConnectorMessage(`Connector 任务轮询失败：${String(reason)}`);
      }
    };
    void claim();
    const timer = window.setInterval(() => void claim(), 5_000);
    return () => window.clearInterval(timer);
  }, [connected, connector?.status, connectorId]);

  useEffect(() => {
    if (!run || !terminal.has(run.status)) return;
    // 切换任务后丢弃旧请求响应，防止较慢的审计查询覆盖新任务的证据。
    let active = true;
    void desktopApi.getAuditEvents(run.run_id)
      .then((response) => { if (active) { setAuditEvents(response.items); setAuditStatus(response.status); } })
      .catch((reason) => { if (active) { setAuditStatus("unavailable"); setError(String(reason)); } });
    return () => { active = false; };
  }, [run?.run_id, run?.status]);

  useEffect(() => {
    if (!run) return;
    const item: RunHistoryItem = { runId: run.run_id, agentId, environment, status: run.status, updatedAt: new Date().toISOString() };
    void desktopApi.recordRun(item).then(() => desktopApi.listRunHistory()).then(setHistory);
  }, [run?.run_id, run?.status, agentId, environment]);

  const answer = useMemo(() => String((run?.result?.answer ?? (run as Record<string, unknown> | null)?.answer) || ""), [run]);
  const result = useMemo(() => resultRecord(run), [run]);
  const plan = useMemo(() => result.execution_plan && typeof result.execution_plan === "object" ? result.execution_plan as Record<string, unknown> : {}, [result]);
  const evidence = useMemo(() => recordArray(result.evidence), [result]);
  const observations = useMemo(() => recordArray(result.observations), [result]);
  const planAdmission = useMemo(() => matchingEvents(events, "runtime.plan.admitted").at(-1), [events]);
  const contextEvent = useMemo(() => matchingEvents(events, "runtime.context.injected").at(-1), [events]);
  const epochs = useMemo(() => matchingEvents(events, "runtime.request_epoch.pinned"), [events]);
  const toolIntents = useMemo(() => matchingEvents(events, "runtime.tool.intent_recorded"), [events]);
  const toolDispatches = useMemo(() => matchingEvents(events, "runtime.tool.dispatched"), [events]);
  const auditProjection = useMemo(() => events.filter((event) => ["runtime.run.state_changed", "runtime.run.completed"].includes(event.event_type)), [events]);
  const contextSummary = useMemo(() => result.context_summary && typeof result.context_summary === "object" ? result.context_summary as Record<string, unknown> : {}, [result]);

  async function connect() {
    setBusy(true); setError("");
    try {
      await desktopApi.configure({ baseUrl, tenantId: tenant, userId: user, permissions, bearerToken: token || undefined });
      setCapabilities(await desktopApi.capabilities());
      setHistory(await desktopApi.listRunHistory());
      setConnected(true);
    } catch (reason) { setConnected(false); setError(String(reason)); }
    finally { setBusy(false); }
  }

  /** 生成一次性配对码；明文只在本次响应和界面内存中存在，服务端仅保存哈希。 */
  async function pairDesktopConnector() {
    setBusy(true); setError(""); setConnectorMessage("");
    try {
      const result = await desktopApi.pairConnector("Agent Workbench", ["workspace:read", "controlled_scan"]);
      setConnectorId(String(result.connector_id || ""));
      setPairingCode(String(result.pairing_code || ""));
      setConfirmCode("");
      setConnector({ ...result, status: "PENDING" });
      setConnectorMessage("配对码已生成，仅显示在本次会话中，10 分钟内有效。");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  /** 确认配对后重新读取服务端状态，避免仅凭前端状态显示“已连接”。 */
  async function confirmDesktopConnector() {
    if (!connectorId || !confirmCode.trim()) return;
    setBusy(true); setError("");
    try {
      await desktopApi.confirmConnector(connectorId, confirmCode.trim());
      const status = await desktopApi.connectorStatus(connectorId);
      setConnector(status);
      setPairingCode("");
      setConfirmCode("");
      setConnectorMessage("Connector 已连接；后续能力仍受 Runtime 权限和工具目录约束。");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function revokeDesktopConnector() {
    if (!connectorId) return;
    setBusy(true); setError("");
    try {
      await desktopApi.revokeConnector(connectorId);
      setConnector(await desktopApi.connectorStatus(connectorId));
      setPairingCode("");
      setConfirmCode("");
      setConnectorMessage("Connector 已撤销，原配对关系不可恢复。");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function submit() {
    setBusy(true); setError("");
    try {
      if (!taskSessionId || !modelRoute) throw new Error("请等待已发布模型路由加载完成");
      const created = await desktopApi.submit({ task, agent_id: agentId, environment, session_id: taskSessionId, model_route: modelRoute, metadata: { interaction_channel: "desktop", desktop_scope: workspace ? "workspace" : "", workspace_manifest: workspace?.entries.slice(0, 120) ?? [] } });
      // 新任务提交成功后才清除旧任务视图；失败时保留原有诊断信息。
      if (run) await desktopApi.stopEvents(run.run_id);
      setEvents([]); setAuditEvents([]); setAuditStatus("not_requested");
      setFeedbackSaved(false); setFeedbackNote(""); setExportedPath(""); setSteering("");
      setRun(created);
      void desktopApi.streamEvents(created.run_id, 0);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  /** 选择模板只填充草稿；执行时仍解析当前 Agent 的 Active Release。 */
  function chooseTemplate(id: string, templateTask: string) {
    setTaskSource(id);
    setTask(templateTask);
  }

  /** 自定义任务提供明确的空白入口，不要求用户先覆盖演示模板。 */
  function startCustomTask() {
    setTaskSource("custom");
    setTask("");
  }

  /** 修改模板正文即转为自定义任务，避免继续把用户内容标记为模板。 */
  function updateTask(value: string) {
    if (taskSource !== "custom") setTaskSource("custom");
    setTask(value);
  }

  async function decide(approved: boolean) {
    if (!run) return;
    setBusy(true);
    try { setRun(await desktopApi.approve(run.run_id, approved, approved ? "用户在桌面端批准" : "用户在桌面端拒绝")); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function saveFeedback(rating: "positive" | "negative") {
    if (!run) return;
    await desktopApi.saveFeedback({
      runId: run.run_id,
      rating,
      category: "quality",
      note: feedbackNote,
    });
    setFeedbackSaved(true);
  }

  return <div className="shell">
    <aside>
      <div className="brand"><span>EA</span><div><strong>Agent Workbench</strong><small>受控桌面执行台</small></div></div>
      <section><h3>Runtime 连接</h3>
        <label>地址<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label>
        <div className="twocol"><label>租户<input value={tenant} onChange={(e) => setTenant(e.target.value)} /></label><label>用户<input value={user} onChange={(e) => setUser(e.target.value)} /></label></div>
        <label>权限<input value={permissions} onChange={(e) => setPermissions(e.target.value)} /></label>
        <label>OIDC Token（仅保存在主进程内存）<input type="password" value={token} onChange={(e) => setToken(e.target.value)} /></label>
        <button className={connected ? "success" : "primary"} onClick={connect} disabled={busy}>{connected ? "已连接" : "验证连接（开始前必做）"}</button>
      </section>
      <section><h3>Desktop Connector</h3>
        <p className="muted">为本桌面端建立短时配对关系。配对码不会写入磁盘，撤销后不可复用。</p>
        <button className="secondary" onClick={pairDesktopConnector} disabled={!connected || busy}>生成配对码</button>
        {connectorId && <>
          <p className="muted">Connector：{connectorId.slice(0, 18)}… · 状态：{String(connector?.status || "PENDING")}</p>
          {pairingCode && <p className="muted">本次配对码：<code>{pairingCode}</code>（请在需要连接的受控端输入）</p>}
          <div className="twocol"><input aria-label="配对码" placeholder="输入配对码" value={confirmCode} onChange={(e) => setConfirmCode(e.target.value)} /><button className="primary" onClick={confirmDesktopConnector} disabled={busy || !confirmCode.trim()}>确认配对</button></div>
          {connector?.status === "CONNECTED" && <button className="danger" onClick={revokeDesktopConnector} disabled={busy}>撤销 Connector</button>}
          {connectorMessage && <p className="muted">{connectorMessage}</p>}
          {connectorTasks.length > 0 && <div className="muted">本机任务队列：{connectorTasks.map((item) => {
            const state = String(item.ui_status || item.status || "AWAITING_CONFIRMATION");
            const delivery = String(item.artifact_delivery_status || "");
            const canExecute = state === "AWAITING_CONFIRMATION" || state === "FAILED";
            return <button className="template" disabled={!canExecute} key={String(item.task_id)} onClick={() => void executeClaimedConnectorTask(item)}><b>{String(item.tool_name || "受控任务")} · {state}</b><span>Run {String(item.run_id || "").slice(0, 12)}…{delivery ? ` · 工件 ${delivery}` : " · 等待人工确认"}{item.artifact_id ? ` · ${String(item.artifact_id).slice(0, 12)}…` : ""}</span>{item.ui_error ? <small>{String(item.ui_error)}</small> : null}</button>;
          })}</div>}
        </>}
      </section>
      <section className="task-library">
        <h3>任务入口</h3>
        <button
          className={`custom-task-entry ${taskSource === "custom" ? "active" : ""}`}
          aria-pressed={taskSource === "custom"}
          onClick={startCustomTask}
        >
          <b>＋ 自定义发布任务</b>
          <span>输入任意业务目标，由当前 Agent 的 Active Release 受控执行。</span>
        </button>
        <p className="section-caption">演示模板</p>
        {templates.map((item) => <button
          className={`template ${taskSource === item.id ? "active" : ""}`}
          aria-pressed={taskSource === item.id}
          key={item.id}
          onClick={() => chooseTemplate(item.id, item.task)}
        ><b>{item.title}</b><span>{item.task.slice(0, 42)}…</span></button>)}
      </section>
      <section><h3>受控工作区</h3><button className="secondary" onClick={async () => setWorkspace(await desktopApi.selectWorkspace())}>选择本地目录</button>{workspace && <p className="muted">{workspace.rootName} · {workspace.totalEntries} 项{workspace.truncated ? "（已截断）" : ""}<br/>只发送有界文件清单，不发送绝对路径或文件正文。</p>}</section>
      <section><h3>最近运行</h3>{history.length ? history.slice(0, 5).map((item) => <button className="template" key={item.runId} onClick={async () => { setError(""); try { const restored = await desktopApi.getRun(item.runId); if(run)await desktopApi.stopEvents(run.run_id); setEvents([]); setAuditEvents([]); setAuditStatus("not_requested"); setFeedbackSaved(false); setExportedPath(""); setRun(restored); void desktopApi.streamEvents(item.runId, 0); } catch (reason) { setError(String(reason)); } }}><b>{item.status} · {item.agentId}</b><span>{item.runId.slice(0, 22)}…</span></button>) : <p className="muted">本机尚无运行索引。</p>}</section>
    </aside>
    <main>
      <header><div><p className="eyebrow">ENTERPRISE AGENT EXECUTION PLATFORM</p><h1>把任务交给可观察、可中断的 Agent</h1></div><div className={`status ${run?.status?.toLowerCase() || "idle"}`}>{run?.status || "IDLE"}</div></header>
      <section className="composer">
        <div className="composer-head">
          <div className="agent-config">
            <label>Agent ID<input value={agentId} onChange={(e) => setAgentId(e.target.value)} /></label>
            <label>发布环境<select value={environment} onChange={(e) => setEnvironment(e.target.value)}><option value="local">local</option><option value="staging">staging</option><option value="production">production</option></select></label>
            <label>指定模型路由<select value={modelRoute} disabled={!connected || !modelCatalog?.items.length} onChange={(e) => setModelRoute(e.target.value)}><option value="">{connected ? "正在读取发布模型…" : "请先验证连接"}</option>{(modelCatalog?.items ?? []).map((item) => <option key={item.route_name} value={item.route_name}>{item.route_name} · {item.models.join(" → ")}</option>)}</select></label>
          </div>
          <span>计划、工具和模型均受发布快照约束</span>
        </div>
        <div className="task-editor-head">
          <div>
            <strong>{taskSource === "custom" ? "自定义发布任务" : "演示任务草稿"}</strong>
            <small>{taskSource === "custom" ? "描述目标、输入、限制和期望输出；不会创建或修改 Release。" : "模板内容可以直接修改，修改后自动转为自定义任务。"}</small>
          </div>
          <span className={`task-source ${taskSource === "custom" ? "custom" : "template-source"}`}>{taskSource === "custom" ? "CUSTOM" : "TEMPLATE"}</span>
        </div>
        <label className="task-editor-label">
          任务内容
          <textarea
            aria-label="自定义发布任务内容"
            maxLength={8000}
            placeholder="例如：读取已授权知识与文件，分析问题并给出包含证据、风险和后续建议的结果。"
            value={task}
            onChange={(event) => updateTask(event.target.value)}
          />
        </label>
        <div className="composer-footer">
          <span>{task.length.toLocaleString()} / 8,000 字符 · 执行时解析 {agentId || "未指定 Agent"} 在 {environment} 的 Active Release · {modelRoute || "等待模型路由"}</span>
          <div className="actions"><button className="primary" onClick={submit} disabled={!connected || busy || !task.trim()}>开始执行</button>{run && !terminal.has(run.status) && <button className="danger" onClick={async () => setRun(await desktopApi.cancel(run.run_id))}>取消任务</button>}</div>
        </div>
      </section>
      {error && <div className="error">{error}</div>}
      {capabilities && <section className="readiness"><div><b>本机运行能力</b><span>执行器：{recordArray(capabilities.execution_providers).map((item) => String(item.profile)).join("、") || "未声明"}</span></div><div><b>发布约束</b><span>目录版本：{String(capabilities.catalog_version || "—")} · 运行时能力：{recordArray(capabilities.capability_manifests).length}</span></div><div><b>模型状态</b><span>由发布快照和 Gateway 决定；未配置凭证时不会伪造模型输出。</span></div></section>}
      {run && <section className="execution-summary"><div><span>执行计划</span><b>{String(plan.plan_id || "准备中")}</b><small>{String((plan.intent as Record<string, unknown> | undefined)?.name || "等待 Planner")}</small></div><div><span>检索证据</span><b>{evidence.length}</b><small>{evidence.length ? "已注入决策上下文" : "本次未取得可用证据"}</small></div><div><span>工具调用</span><b>{observations.length}</b><small>{observations.some((item) => item.success === false) ? "存在失败，见工具面板" : "受目录与权限约束"}</small></div><div><span>成本 / 步数</span><b>{String((result.budget as Record<string, unknown> | undefined)?.spent_cost_usd ?? 0)} / {String(result.steps ?? 0)}</b><small>运行账本持续记录</small></div></section>}
      <div className="grid">
        <section className="panel timeline"><h2>执行时间线 <span>{events.length}</span></h2>{events.length === 0 ? <div className="empty">提交任务后，这里展示计划、检索、模型与工具事件。</div> : events.map((event) => <article key={event.event_id}><i></i><div><time>#{event.sequence} · {eventLabels[event.event_type] || event.event_type}</time><p>{event.model_message?.content || String(event.status || event.metadata?.reason || "事件已提交")}</p><small className="raw-event">{event.event_type} · {event.event_id}</small></div></article>)}</section>
        <section className="panel result"><h2>结果与人工控制</h2>{run?.status === "WAITING_APPROVAL" && <div className="approval"><strong>发现需要人工批准的操作</strong><p>审批决定会写入运行账本，拒绝不会绕过原检查点。</p><button className="primary" onClick={() => decide(true)}>批准并继续</button><button className="danger" onClick={() => decide(false)}>拒绝</button></div>}{answer ? <pre>{answer}</pre> : <div className="empty">最终答案、引用及错误会显示在这里。</div>}{run && !terminal.has(run.status) && <div className="steer"><input placeholder="在下一安全边界补充或修正任务" value={steering} onChange={(e) => setSteering(e.target.value)} /><button onClick={async () => { await desktopApi.sendInput(run.run_id, steering); setSteering(""); }}>发送</button></div>}{run && terminal.has(run.status) && <div className="feedback"><strong>这次任务是否真正解决了问题？</strong><input placeholder="可选：失败原因或改进建议（本地保存并自动脱敏）" value={feedbackNote} onChange={(e) => setFeedbackNote(e.target.value)} />{feedbackSaved ? <span>反馈已记录，可用于下一轮回归集。</span> : <div><button onClick={() => saveFeedback("positive")}>有帮助</button><button onClick={() => saveFeedback("negative")}>需改进</button></div>}</div>}{run && <button className="secondary" onClick={async () => { const exported = await desktopApi.exportRun(run, events); setExportedPath(exported?.path || ""); }}>导出本次运行诊断包</button>}{exportedPath && <p className="muted">已导出：{exportedPath}</p>}<dl><dt>Run ID</dt><dd>{run?.run_id || "—"}</dd><dt>状态</dt><dd>{run?.status || "—"}</dd><dt>事件游标</dt><dd>{events.at(-1)?.sequence || 0}</dd></dl></section>
      </div>
      {run && <div className="inspection-grid"><section className="panel compact"><h2>计划与治理</h2><dl><dt>计划哈希</dt><dd>{String(plan.plan_hash || "—")}</dd><dt>路由</dt><dd>{String((plan.route as Record<string, unknown> | undefined)?.route || "—")}</dd><dt>快照</dt><dd>{String((run as Record<string, unknown>).snapshot_id || "—")}</dd><dt>错误码</dt><dd>{String(run.error || result.error_code || "—")}</dd></dl></section><section className="panel compact"><h2>证据</h2>{evidence.length ? evidence.map((item, index) => <article className="detail-item" key={String(item.evidence_id || index)}><b>{String(item.evidence_id || item.document_id || `证据 ${index + 1}`)}</b><p>{String(item.content || item.snippet || item.text || "已检索证据（内容受脱敏与上限约束）")}</p></article>) : <div className="empty small">没有可展示的知识库证据。离线或无匹配时会明确降级。</div>}</section><section className="panel compact"><h2>工具与安全</h2>{observations.length ? observations.map((item, index) => <article className={`detail-item ${item.success === false ? "failed" : ""}`} key={`${String(item.tool || "tool")}-${index}`}><b>{String(item.tool || "受控工具")} · {item.success === false ? "失败" : "完成"}</b><details><summary>查看工具结果</summary><pre>{shortJson(item.result ?? item.error ?? item)}</pre></details></article>) : <div className="empty small">本次没有工具执行。工具不会因界面请求而绕过发布快照、权限或审批。</div>}</section></div>}
      {!run && <section className="idle-observability panel" style={{ marginTop: 18 }}>
        <h2>运行事实观察清单</h2>
        <p>当前尚未创建 Run。点击左侧“验证连接（开始前必做）”后可提交任务；任务开始后，此清单会被本次实际事件、证据和审计记录替换为下方的完整运行事实。</p>
        <div className="observability-list" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10, marginTop: 14 }}>{observabilityChecklist.map(([number, title, description]) => <div key={number} style={{ border: "1px solid #293957", background: "#0d1527", borderRadius: 8, padding: 10 }}><b style={{ display: "block", color: "#cbd7f2", fontSize: 12 }}>{number} · {title}</b><span style={{ display: "block", color: "#8d9bb5", fontSize: 11, lineHeight: 1.4, marginTop: 4 }}>{description}</span><em style={{ display: "block", color: "#7185b2", fontSize: 10, fontStyle: "normal", marginTop: 6 }}>等待任务</em></div>)}</div>
      </section>}
      {run && <section className="full-detail" key={run.run_id}>
        <h2>完整运行事实</h2>
        <div className="inspection-grid">
          <FactDetails title="1–2. Planner 与 Harness" description="Planner 计划与 Harness 选择的执行器均来自已发布快照，不能由界面覆盖。" data={{ plan_id: plan.plan_id, intent: plan.intent, entities: plan.entities, source_plan: plan.source_plan, complexity: plan.complexity, sla: plan.sla, cost: plan.cost, route: plan.route, executor_profile: plan.executor_profile, execution_mode: plan.execution_mode, execution_requirements: plan.execution_requirements, admission: planAdmission?.metadata }} />
          <FactDetails title="3. RAG 与 Evidence" description="展示本次实际检索结果；空数组不是“有证据”，而是未检索/无匹配/已降级。" data={{ evidence, retrieval_observations: observations.filter((item) => item.type === "retrieval"), retrieval_policy: plan.retrieval_policy }} />
          <FactDetails title="4. 模型路由与降级" description="本地离线决策也会保留冻结的逻辑路由，但不等于实际消耗了该模型 Token。" data={{ model_policy_version: plan.model_policy_version, fallback_chain: (plan.route as Record<string, unknown> | undefined)?.fallback_chain, epochs: epochs.map((event) => event.metadata), llm_calls: (result.budget as Record<string, unknown> | undefined)?.llm_calls }} />
          <FactDetails title="5–6. 工具、参数与权限" description="参数取自工具意图账本；权限与风险检查取自计划准入，执行事实取自工具结果。" data={{ intents: toolIntents.map((event) => ({ metadata: event.metadata, arguments: event.model_message?.content })), dispatches: toolDispatches.map((event) => event.metadata), tool_scope_check: recordArray(planAdmission?.metadata?.checks).filter((item) => item.check === "tool_scope" || item.check === "risk"), results: observations.filter((item) => item.type === "tool") }} />
          <FactDetails title="7. 成本、Token 与预算" description="Token 只在真实 Gateway 响应提供时计入；离线决策显示为 0 次 LLM 调用。" data={{ latency_ms: result.latency_ms, budget: result.budget, remaining: { cost_usd: Math.max(0, Number((result.budget as Record<string, unknown> | undefined)?.max_cost_usd || 0) - Number((result.budget as Record<string, unknown> | undefined)?.spent_cost_usd || 0)), steps: Math.max(0, Number((result.budget as Record<string, unknown> | undefined)?.max_steps || 0) - Number((result.budget as Record<string, unknown> | undefined)?.step_count || 0)) } }} />
          <FactDetails title="8–9. 审批、恢复与控制" description="审批原因由中断对象给出；Steering 写入邮箱，取消经状态机处理，均不会直接修改历史结果。" data={{ status: run.status, termination_reason: result.termination_reason, approval_interrupts: result.interrupts, available_actions: run.status === "WAITING_APPROVAL" ? ["approve", "reject", "cancel"] : terminal.has(run.status) ? ["export", "feedback"] : ["steering", "cancel"] }} />
          <FactDetails title="10. Governance 审计事件" description="仅查询当前用户拥有的 Run；Runtime 代理该受限读取，桌面端不持有治理审计员密钥。" data={{ status: auditStatus, received: auditEvents, pending_projection: auditProjection.map((event) => ({ governance_event_id: event.event_type === "runtime.run.state_changed" ? `gov_${event.event_id}` : event.event_id, source_event: event.event_type, sequence: event.sequence, status: event.status })) }} />
          <FactDetails title="11–12. Context 与 Release" description="Context 仅展示被选中历史的角色、时间与内容哈希，不把完整历史复制到诊断面板。" data={{ context_event: contextEvent?.metadata, selected_history: contextSummary.selected_history, release: { agent_id: run.agent_id, agent_version: (run as Record<string, unknown>).context && ((run as Record<string, unknown>).context as Record<string, unknown>).agent_version, snapshot_id: run.snapshot_id, graph_version: ((run as Record<string, unknown>).context as Record<string, unknown> | undefined)?.graph_version, model_policy_version: ((run as Record<string, unknown>).context as Record<string, unknown> | undefined)?.model_policy_version } }} />
        </div>
      </section>}
    </main>
  </div>;
}
