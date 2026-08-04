package com.zxf.ai.gateway.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.UUID;
import java.util.Iterator;
import java.util.Map;
import java.util.Set;

/**
 * Non-blocking trace export. Evaluation decisions are made by Governance;
 * failure to export never changes the model response already produced.
 */
@Component
public class GovernanceTracePublisher {
    private static final Logger log = LoggerFactory.getLogger(GovernanceTracePublisher.class);
    private final PlatformServiceClient platform;
    private final ObjectMapper objectMapper;
    private final boolean captureContent;
    private static final Set<String> CONTENT_FIELDS = Set.of(
            "messages", "content", "prompt", "input", "output", "arguments");

    public GovernanceTracePublisher(
            PlatformServiceClient platform,
            ObjectMapper objectMapper,
            @Value("${gateway.governance.capture-content:false}") boolean captureContent) {
        this.platform = platform;
        this.objectMapper = objectMapper;
        this.captureContent = captureContent;
    }

    @EventListener
    public void publish(GatewayTraceEvent event) {
        // Content policy is applied before either export path.  Trace delivery
        // is intentionally best-effort: a governance outage cannot replay an
        // already completed LLM request or change its client response.
        ObjectNode payload = objectMapper.valueToTree(event);
        if (!captureContent) {
            payload.set("request", redactContent(payload.path("request").deepCopy()));
            payload.set("response", redactContent(payload.path("response").deepCopy()));
        }
        payload.put("dataClassification", captureContent ? "restricted" : "confidential");
        platform.governance("POST", "/v1/governance/evaluations/traces/gateway",
                        payload, event.tenantId(), event.userId())
                .subscribe(
                        ignored -> { },
                        error -> log.warn(
                                "governance_trace_export_failed requestId={} reason={}",
                                event.requestId(), error.getMessage())
                );
        ObjectNode governanceEvent = objectMapper.createObjectNode();
        governanceEvent.put("schema_version", "1.0");
        governanceEvent.put("event_id", "evt_" + UUID.randomUUID().toString().replace("-", ""));
        governanceEvent.put("source_service", "llm-gateway");
        governanceEvent.put("event_type", "llm.request.completed");
        governanceEvent.put("trace_id", event.traceId());
        governanceEvent.put("tenant_id", event.tenantId());
        governanceEvent.put("occurred_at", Instant.now().toString());
        ObjectNode details = governanceEvent.putObject("payload");
        details.put("request_id", event.requestId());
        details.put("run_id", event.runId());
        details.put("agent_id", event.agentId());
        details.put("agent_version", event.agentVersion());
        details.put("model", event.requestedModel());
        details.put("data_region", event.dataRegion());
        details.put("success", event.success());
        details.put("latency_ms", event.latencyMs());
        details.put("cost", event.cost());
        details.put("cost_currency", event.currency());
        details.put("error_type", event.errorType());
        platform.governanceEvent(governanceEvent, event.tenantId(), event.userId())
                .subscribe(
                        ignored -> { },
                        error -> log.warn(
                                "governance_event_export_failed requestId={} reason={}",
                                event.requestId(), error.getMessage())
                );
    }

    private JsonNode redactContent(JsonNode node) {
        if (node instanceof ObjectNode object) {
            Iterator<Map.Entry<String, JsonNode>> fields = object.fields();
            while (fields.hasNext()) {
                Map.Entry<String, JsonNode> field = fields.next();
                String name = field.getKey().toLowerCase();
                if (CONTENT_FIELDS.contains(name)
                        || name.contains("authorization") || name.contains("api_key")
                        || name.contains("password") || name.contains("secret")) {
                    object.put(field.getKey(), "[REDACTED]");
                } else {
                    redactContent(field.getValue());
                }
            }
        } else if (node.isArray()) {
            node.forEach(this::redactContent);
        }
        return node;
    }
}
