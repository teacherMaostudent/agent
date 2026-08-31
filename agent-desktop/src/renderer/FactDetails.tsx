import { useState } from "react";
import "./fact-details.css";

/** 按需展示服务返回的运行事实；展开前不挂载长 JSON，切换 Run 后由父级 key 重置。 */
export function FactDetails({ title, description, data }: {
  title: string; description: string; data: unknown;
}) {
  const [expanded, setExpanded] = useState(false);
  return <details className="panel fact-details" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary><strong>{title}</strong><span>查看详情</span><small>{description}</small></summary>
    {expanded && <pre aria-label={title + "报文"}>{JSON.stringify(data, null, 2)}</pre>}
  </details>;
}
