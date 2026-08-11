package com.zxf.ai.gateway.client;

import com.zxf.ai.gateway.model.GatewayException;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 根据 provider.protocol 选择具体客户端实现。
 */
@Component
public class LlmClientRegistry {
    private final Map<String, LlmProviderClient> clients;

    /**
     * 初始化 llm client registry 所需的依赖与运行期状态。
    */
    public LlmClientRegistry(List<LlmProviderClient> clients) {
        this.clients = clients.stream()
                .collect(Collectors.toMap(client -> normalize(client.protocol()), Function.identity()));
    }

    /**
     * 构建或解析 resolve 所需的受控对象，避免调用方依赖内部实现细节。
    */
    public LlmProviderClient resolve(ModelEndpoint endpoint) {
        String protocol = normalize(endpoint.provider().getProtocol());
        LlmProviderClient client = clients.get(protocol);
        if (client == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST,
                    "Unsupported provider protocol: " + endpoint.provider().getProtocol());
        }
        return client;
    }

    /**
     * 执行 normalize 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    private String normalize(String protocol) {
        return protocol == null || protocol.isBlank()
                ? "openai-compatible"
                : protocol.toLowerCase(Locale.ROOT);
    }
}
