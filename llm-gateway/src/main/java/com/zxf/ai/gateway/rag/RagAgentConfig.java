package com.zxf.ai.gateway.rag;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(RagAgentProperties.class)
public class RagAgentConfig {
}
