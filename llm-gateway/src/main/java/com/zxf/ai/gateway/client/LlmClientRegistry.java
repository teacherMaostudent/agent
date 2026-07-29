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

    public LlmClientRegistry(List<LlmProviderClient> clients) {
        this.clients = clients.stream()
                .collect(Collectors.toMap(client -> normalize(client.protocol()), Function.identity()));
    }

    public LlmProviderClient resolve(ModelEndpoint endpoint) {
        String protocol = normalize(endpoint.provider().getProtocol());
        LlmProviderClient client = clients.get(protocol);
        if (client == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST,
                    "Unsupported provider protocol: " + endpoint.provider().getProtocol());
        }
        return client;
    }

    private String normalize(String protocol) {
        return protocol == null || protocol.isBlank()
                ? "openai-compatible"
                : protocol.toLowerCase(Locale.ROOT);
    }
}
