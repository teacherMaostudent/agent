import { useEffect, useMemo, useState } from "react";
import type { RunHistoryItem, RuntimeEvent, RunSnapshot, WorkspacePreview } from "../shared/contracts";
import { browserPreviewApi } from "./browser-preview";

const templates = [
  { id: "scan", title: "源码与日志扫描", task: "使用 controlled_scan 在白名单 scope=workspace 中查找 TODO、异常处理缺口和潜在敏感信息，输出按严重度排序并带文件与行号的报告。" },
  { id: "research", title: "证据型研究报告", task: "围绕当前问题检索知识库，区分事实、推断和未知项，输出每项结论对应的证据 ID，并明确证据不足之处。" },
  { id: "organize", title: "工作区整理预案", task: "根据工作区清单生成安全的文件整理预案。不得直接修改文件；列出拟移动项、目标目录、冲突风险和可回滚步骤，等待人工批准。" },
];

const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
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
  const [permissions, setPermissions] = useState("rag:read,file:scan,tool:invoke");
  const [token, setToken] = useState("");
  const [connected, setConnected] = useState(false);
  const [task, setTask] = useState(templates[0].task);
  const [agentId, setAgentId] = useState("general-agent");
  const [environment, setEnvironment] = useState("local");
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

  useEffect(() => desktopApi.onRuntimeEvent((runId, event) => {
    if (runId === run?.run_id) setEvents((current) => current.some((item) => item.event_id === event.event_id) ? current : [...current, event]);
  }), [run?.run_id]);

  useEffect(() => desktopApi.onRuntimeError((runId, message) => {
    if (runId === run?.run_id && !terminal.has(run.status)) setError(message);
  }), [run]);

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
    if (!run || !terminal.has(run.status)) return;
    void desktopApi.getAuditEvents(run.run_id)
      .then((response) => { setAuditEvents(response.items); setAuditStatus(response.status); })
      .catch((reason) => { setAuditStatus("unavailable"); setError(String(reason)); });
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

  async function submit() {
    setBusy(true); setError(""); setEvents([]); setFeedbackSaved(false); setFeedbackNote("");
    try {
      const created = await desktopApi.submit({ task, agent_id: agentId, environment, metadata: { interaction_channel: "desktop", desktop_scope: workspace ? "workspace" : "", workspace_manifest: workspace?.entries.slice(0, 120) ?? [] } });
      setRun(created);
      void desktopApi.streamEvents(created.run_id, 0);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
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
      <section><h3>演示任务</h3>{templates.map((item) => <button className="template" key={item.id} onClick={() => setTask(item.task)}><b>{item.title}</b><span>{item.task.slice(0, 42)}…</span></button>)}</section>
      <section><h3>受控工作区</h3><button className="secondary" onClick={async () => setWorkspace(await desktopApi.selectWorkspace())}>选择本地目录</button>{workspace && <p className="muted">{workspace.rootName} · {workspace.totalEntries} 项{workspace.truncated ? "（已截断）" : ""}<br/>只发送有界文件清单，不发送绝对路径或文件正文。</p>}</section>
      <section><h3>最近运行</h3>{history.length ? history.slice(0, 5).map((item) => <button className="template" key={item.runId} onClick={async () => { setError(""); try { setRun(await desktopApi.getRun(item.runId)); void desktopApi.streamEvents(item.runId, 0); } catch (reason) { setError(String(reason)); } }}><b>{item.status} · {item.agentId}</b><span>{item.runId.slice(0, 22)}…</span></button>) : <p className="muted">本机尚无运行索引。</p>}</section>
    </aside>
    <main>
      <header><div><p className="eyebrow">ENTERPRISE AGENT EXECUTION PLATFORM</p><h1>把任务交给可观察、可中断的 Agent</h1></div><div className={`status ${run?.status?.toLowerCase() || "idle"}`}>{run?.status || "IDLE"}</div></header>
      <section className="composer"><div className="composer-head"><div className="agent-config"><label>Agent ID<input value={agentId} onChange={(e) => setAgentId(e.target.value)} /></label><label>发布环境<select value={environment} onChange={(e) => setEnvironment(e.target.value)}><option value="local">local</option><option value="staging">staging</option><option value="production">production</option></select></label></div><span>计划、工具和模型均受发布快照约束</span></div><textarea value={task} onChange={(e) => setTask(e.target.value)} /><div className="actions"><button className="primary" onClick={submit} disabled={!connected || busy || !task.trim()}>开始执行</button>{run && !terminal.has(run.status) && <button className="danger" onClick={async () => setRun(await desktopApi.cancel(run.run_id))}>取消任务</button>}</div></section>
      {error && <div className="error">{error}</div>}
      {capabilities && <section className="readiness"><div><b>本机运行能力</b><span>执行器：{recordArray(capabilities.execution_providers).map((item) => String(item.profile)).join("、") || "未声明"}</span></div><div><b>发布约束</b><span>目录版本：{String(capabilities.catalog_version || "—")} · 运行时能力：{recordArray(capabilities.capability_manifests).length}</span></div><div><b>模型状态</b><span>由发布快照和 Gateway 决定；未配置凭证时不会伪造模型输出。</span></div></section>}
      {run && <section className="execution-summary"><div><span>执行计划</span><b>{String(plan.plan_id || "准备中")}</b><small>{String((plan.intent as Record<string, unknown> | undefined)?.name || "等待 Planner")}</small></div><div><span>检索证据</span><b>{evidence.length}</b><small>{evidence.length ? "已注入决策上下文" : "本次未取得可用证据"}</small></div><div><span>工具调用</span><b>{observations.length}</b><small>{observations.some((item) => item.success === false) ? "存在失败，见工具面板" : "受目录与权限约束"}</small></div><div><span>成本 / 步数</span><b>{String((result.budget as Record<string, unknown> | undefined)?.spent_cost_usd ?? 0)} / {String(result.steps ?? 0)}</b><small>运行账本持续记录</small></div></section>}
      <div className="grid">
        <section className="panel timeline"><h2>执行时间线 <span>{events.length}</span></h2>{events.length === 0 ? <div className="empty">提交任务后，这里展示计划、检索、模型与工具事件。</div> : events.map((event) => <article key={event.event_id}><i></i><div><time>#{event.sequence} · {eventLabels[event.event_type] || event.event_type}</time><p>{event.model_message?.content || String(event.status || event.metadata?.reason || "事件已提交")}</p><small className="raw-event">{event.event_type} · {event.event_id}</small></div></article>)}</section>
        <section className="panel result"><h2>结果与人工控制</h2>{run?.status === "WAITING_APPROVAL" && <div className="approval"><strong>发现需要人工批准的操作</strong><p>审批决定会写入运行账本，拒绝不会绕过原检查点。</p><button className="primary" onClick={() => decide(true)}>批准并继续</button><button className="danger" onClick={() => decide(false)}>拒绝</button></div>}{answer ? <pre>{answer}</pre> : <div className="empty">最终答案、引用及错误会显示在这里。</div>}{run && !terminal.has(run.status) && <div className="steer"><input placeholder="在下一安全边界补充或修正任务" value={steering} onChange={(e) => setSteering(e.target.value)} /><button onClick={async () => { await desktopApi.sendInput(run.run_id, steering); setSteering(""); }}>发送</button></div>}{run && terminal.has(run.status) && <div className="feedback"><strong>这次任务是否真正解决了问题？</strong><input placeholder="可选：失败原因或改进建议（本地保存并自动脱敏）" value={feedbackNote} onChange={(e) => setFeedbackNote(e.target.value)} />{feedbackSaved ? <span>反馈已记录，可用于下一轮回归集。</span> : <div><button onClick={() => saveFeedback("positive")}>有帮助</button><button onClick={() => saveFeedback("negative")}>需改进</button></div>}</div>}{run && <button className="secondary" onClick={async () => { const exported = await desktopApi.exportRun(run, events); setExportedPath(exported?.path || ""); }}>导出本次运行诊断包</button>}{exportedPath && <p className="muted">已导出：{exportedPath}</p>}<dl><dt>Run ID</dt><dd>{run?.run_id || "—"}</dd><dt>状态</dt><dd>{run?.status || "—"}</dd><dt>事件游标</dt><dd>{events.at(-1)?.sequence || 0}</dd></dl></section>
      </div>
      {run && <div className="inspection-grid"><section className="panel compact"><h2>计划与治理</h2><dl><dt>计划哈希</dt><dd>{String(plan.plan_hash || "—")}</dd><dt>路由</dt><dd>{String((plan.route as Record<string, unknown> | undefined)?.route || "—")}</dd><dt>快照</dt><dd>{String((run as Record<string, unknown>).snapshot_id || "—")}</dd><dt>错误码</dt><dd>{String(run.error || result.error_code || "—")}</dd></dl></section><section className="panel compact"><h2>证据</h2>{evidence.length ? evidence.map((item, index) => <article className="detail-item" key={String(item.evidence_id || index)}><b>{String(item.evidence_id || item.document_id || `证据 ${index + 1}`)}</b><p>{String(item.content || item.snippet || item.text || "已检索证据（内容受脱敏与上限约束）")}</p></article>) : <div className="empty small">没有可展示的知识库证据。离线或无匹配时会明确降级。</div>}</section><section className="panel compact"><h2>工具与安全</h2>{observations.length ? observations.map((item, index) => <article className={`detail-item ${item.success === false ? "failed" : ""}`} key={`${String(item.tool || "tool")}-${index}`}><b>{String(item.tool || "受控工具")} · {item.success === false ? "失败" : "完成"}</b><pre>{shortJson(item.result ?? item.error ?? item)}</pre></article>) : <div className="empty small">本次没有工具执行。工具不会因界面请求而绕过发布快照、权限或审批。</div>}</section></div>}
      {!run && <section className="idle-observability panel" style={{ marginTop: 18 }}>
        <h2>运行事实观察清单</h2>
        <p>当前尚未创建 Run。点击左侧“验证连接（开始前必做）”后可提交任务；任务开始后，此清单会被本次实际事件、证据和审计记录替换为下方的完整运行事实。</p>
        <div className="observability-list" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10, marginTop: 14 }}>{observabilityChecklist.map(([number, title, description]) => <div key={number} style={{ border: "1px solid #293957", background: "#0d1527", borderRadius: 8, padding: 10 }}><b style={{ display: "block", color: "#cbd7f2", fontSize: 12 }}>{number} · {title}</b><span style={{ display: "block", color: "#8d9bb5", fontSize: 11, lineHeight: 1.4, marginTop: 4 }}>{description}</span><em style={{ display: "block", color: "#7185b2", fontSize: 10, fontStyle: "normal", marginTop: 6 }}>等待任务</em></div>)}</div>
      </section>}
      {run && <section className="full-detail">
        <h2>完整运行事实</h2>
        <div className="inspection-grid">
          <section className="panel compact"><h2>1–2. Planner 与 Harness</h2><p className="detail-note">Planner 计划与 Harness 选择的执行器均来自已发布快照，不能由界面覆盖。</p><pre>{shortJson({ plan_id: plan.plan_id, intent: plan.intent, entities: plan.entities, source_plan: plan.source_plan, complexity: plan.complexity, sla: plan.sla, cost: plan.cost, route: plan.route, executor_profile: plan.executor_profile, execution_mode: plan.execution_mode, execution_requirements: plan.execution_requirements, admission: planAdmission?.metadata })}</pre></section>
          <section className="panel compact"><h2>3. RAG 与 Evidence</h2><p className="detail-note">展示本次实际检索结果；空数组不是“有证据”，而是未检索/无匹配/已降级。</p><pre>{shortJson({ evidence, retrieval_observations: observations.filter((item) => item.type === "retrieval"), retrieval_policy: plan.retrieval_policy })}</pre></section>
          <section className="panel compact"><h2>4. 模型路由与降级</h2><p className="detail-note">本地离线决策也会保留冻结的逻辑路由，但不等于实际消耗了该模型 Token。</p><pre>{shortJson({ model_policy_version: plan.model_policy_version, fallback_chain: (plan.route as Record<string, unknown> | undefined)?.fallback_chain, epochs: epochs.map((event) => event.metadata), llm_calls: (result.budget as Record<string, unknown> | undefined)?.llm_calls })}</pre></section>
          <section className="panel compact"><h2>5–6. 工具、参数与权限</h2><p className="detail-note">参数取自工具意图账本；权限与风险检查取自计划准入，执行事实取自工具结果。</p><pre>{shortJson({ intents: toolIntents.map((event) => ({ metadata: event.metadata, arguments: event.model_message?.content })), dispatches: toolDispatches.map((event) => event.metadata), tool_scope_check: recordArray(planAdmission?.metadata?.checks).filter((item) => item.check === "tool_scope" || item.check === "risk"), results: observations.filter((item) => item.type === "tool") })}</pre></section>
          <section className="panel compact"><h2>7. 成本、Token 与预算</h2><p className="detail-note">Token 只在真实 Gateway 响应提供时计入；离线决策显示为 0 次 LLM 调用。</p><pre>{shortJson({ latency_ms: result.latency_ms, budget: result.budget, remaining: { cost_usd: Math.max(0, Number((result.budget as Record<string, unknown> | undefined)?.max_cost_usd || 0) - Number((result.budget as Record<string, unknown> | undefined)?.spent_cost_usd || 0)), steps: Math.max(0, Number((result.budget as Record<string, unknown> | undefined)?.max_steps || 0) - Number((result.budget as Record<string, unknown> | undefined)?.step_count || 0)) } })}</pre></section>
          <section className="panel compact"><h2>8–9. 审批、恢复与控制</h2><p className="detail-note">审批原因由中断对象给出；Steering 写入邮箱，取消经状态机处理，均不会直接修改历史结果。</p><pre>{shortJson({ status: run.status, termination_reason: result.termination_reason, approval_interrupts: result.interrupts, available_actions: run.status === "WAITING_APPROVAL" ? ["approve", "reject", "cancel"] : terminal.has(run.status) ? ["export", "feedback"] : ["steering", "cancel"] })}</pre></section>
          <section className="panel compact"><h2>10. Governance 审计事件</h2><p className="detail-note">仅查询当前用户拥有的 Run；Runtime 代理该受限读取，桌面端不持有治理审计员密钥。</p><pre>{shortJson({ status: auditStatus, received: auditEvents, pending_projection: auditProjection.map((event) => ({ governance_event_id: event.event_type === "runtime.run.state_changed" ? `gov_${event.event_id}` : event.event_id, source_event: event.event_type, sequence: event.sequence, status: event.status })) })}</pre></section>
          <section className="panel compact"><h2>11–12. Context 与 Release</h2><p className="detail-note">Context 仅展示被选中历史的角色、时间与内容哈希，不把完整历史复制到诊断面板。</p><pre>{shortJson({ context_event: contextEvent?.metadata, selected_history: contextSummary.selected_history, release: { agent_id: run.agent_id, agent_version: (run as Record<string, unknown>).context && ((run as Record<string, unknown>).context as Record<string, unknown>).agent_version, snapshot_id: run.snapshot_id, graph_version: ((run as Record<string, unknown>).context as Record<string, unknown> | undefined)?.graph_version, model_policy_version: ((run as Record<string, unknown>).context as Record<string, unknown> | undefined)?.model_policy_version } })}</pre></section>
        </div>
      </section>}
    </main>
  </div>;
}
