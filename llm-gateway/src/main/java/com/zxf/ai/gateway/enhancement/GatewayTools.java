package com.zxf.ai.gateway.enhancement;

import dev.langchain4j.agent.tool.P;
import dev.langchain4j.agent.tool.Tool;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.MathContext;
import java.time.Instant;
import java.util.Map;

/**
 * 暴露给大模型调用的工具集合。
 *
 * <p>Tool Calling 的核心是把后端确定性能力交给模型编排：模型负责判断何时调用工具，
 * Java 方法负责提供准确结果。生产环境里可以把这些工具扩展成查询订单、检索工单、
 * 查询用户配额、读取监控指标等企业内部能力。</p>
 */
@Component
@ConditionalOnProperty(prefix = "enhancement.langchain4j", name = "enabled", havingValue = "true")
public class GatewayTools {
    /** 返回网关当前时间；该工具没有外部副作用。 */
    @Tool("获取当前网关服务器时间，用于回答和时间相关的问题")
    public String currentGatewayTime() {
        return Instant.now().toString();
    }

    /** 依据调用方提供的单价和 Token 数量进行确定性成本估算，不写入计费账本。 */
    @Tool("估算一次大模型调用成本，输入输出单价均按每 1000 token 计算")
    public BigDecimal estimateLlmCost(
            @P("输入 token 数") long promptTokens,
            @P("输出 token 数") long completionTokens,
            @P("每 1000 输入 token 的价格") BigDecimal inputPricePer1k,
            @P("每 1000 输出 token 的价格") BigDecimal outputPricePer1k
    ) {
        BigDecimal input = inputPricePer1k.multiply(BigDecimal.valueOf(promptTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        BigDecimal output = outputPricePer1k.multiply(BigDecimal.valueOf(completionTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        return input.add(output);
    }

    /** 返回当前增强层公开能力的静态声明，供模型决定是否请求后续工具。 */
    @Tool("查询当前 LLM Gateway 演示环境支持的能力清单")
    public Map<String, Object> gatewayCapabilities() {
        return Map.of(
                "chatCompletions", true,
                "streaming", true,
                "modelRouting", true,
                "fallback", true,
                "timeout", true,
                "retry", true,
                "quota", true,
                "costEstimate", true,
                "rag", true,
                "toolCalling", true
        );
    }
}
