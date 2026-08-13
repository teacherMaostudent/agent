package com.zxf.ai.gateway;

import com.zxf.ai.gateway.enhancement.EnhancedChatService;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import com.zxf.ai.gateway.memory.AgentMemoryService;
import com.zxf.ai.gateway.rag.RagAgentClient;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Guards the production responsibility boundary at Spring container level.
 *
 * <p>Workflow implementations are not present in Gateway. Compatibility
 * controllers delegate to the independently deployed platform services.</p>
 */
@SpringBootTest(properties = {
        "gateway.persistence.enabled=false",
        "spring.sql.init.mode=never",
        "management.health.redis.enabled=false",
        "enhancement.langchain4j.enabled=false",
        "rag-agent.enabled=false",
        "gateway.compatibility.agent-memory.enabled=false",
        "gateway.admission.store=memory"
})
class GatewayBoundaryContextTest {
    @Autowired
    private ApplicationContext applicationContext;

    @Test
    void defaultGatewayContextExcludesAgentRagAndMemoryBeans() {
        assertThat(applicationContext.getBeansOfType(EnhancedChatService.class)).isEmpty();
        assertThat(applicationContext.getBeansOfType(RagAgentClient.class)).isEmpty();
        assertThat(applicationContext.getBeansOfType(AgentMemoryService.class)).isEmpty();
        assertThat(applicationContext.getBean(PlatformServiceClient.class)).isNotNull();
    }
}
