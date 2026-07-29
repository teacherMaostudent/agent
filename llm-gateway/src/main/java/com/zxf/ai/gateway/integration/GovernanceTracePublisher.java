package com.zxf.ai.gateway.integration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.eval.GatewayTraceEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

/**
 * Non-blocking trace export. Evaluation decisions are made by Governance;
 * failure to export never changes the model response already produced.
 */
@Component
public class GovernanceTracePublisher {
    private static final Logger log = LoggerFactory.getLogger(GovernanceTracePublisher.class);
    private final PlatformServiceClient platform;
    private final ObjectMapper objectMapper;

    public GovernanceTracePublisher(PlatformServiceClient platform, ObjectMapper objectMapper) {
        this.platform = platform;
        this.objectMapper = objectMapper;
    }

    @EventListener
    public void publish(GatewayTraceEvent event) {
        JsonNode payload = objectMapper.valueToTree(event);
        platform.governance("POST", "/v1/governance/evaluations/traces/gateway",
                        payload, event.tenantId(), event.userId())
                .subscribe(
                        ignored -> { },
                        error -> log.warn(
                                "governance_trace_export_failed requestId={} reason={}",
                                event.requestId(), error.getMessage())
                );
    }
}
