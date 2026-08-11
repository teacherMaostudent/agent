package com.zxf.ai.gateway;

import com.zxf.ai.gateway.config.GatewayProperties;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

@SpringBootApplication
@EnableConfigurationProperties(GatewayProperties.class)
public class LlmGatewayApplication {
    /** 启动 Spring 容器，并加载网关路由、鉴权、限额与可观测性配置。 */
    public static void main(String[] args) {
        SpringApplication.run(LlmGatewayApplication.class, args);
    }
}
