package com.zxf.ai.gateway.rag;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.BodyInserters;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import reactor.core.publisher.Mono;

@Component
@ConditionalOnProperty(prefix = "rag-agent", name = "enabled", havingValue = "true")
public class RagAgentClient {
    private final WebClient client;
    private final RagAgentProperties properties;

    /** 按受控配置创建 RAG 服务客户端，并集中挂载迁移期服务凭据。 */
    public RagAgentClient(WebClient.Builder webClientBuilder, RagAgentProperties properties) {
        this.properties = properties;
        this.client = webClientBuilder
                .baseUrl(properties.getBaseUrl())
                .defaultHeader("X-Rag-Agent-Key", properties.getApiKey())
                .build();
    }

    /** 上传文档及其业务元数据到 RAG 摄取 API，并将远程错误规范化为网关异常。 */
    public Mono<JsonNode> uploadDocument(FilePart file, String businessId, String documentType) {
        ensureEnabled();
        MultipartBodyBuilder builder = new MultipartBodyBuilder();
        builder.asyncPart("file", file.content(), org.springframework.core.io.buffer.DataBuffer.class)
                .filename(file.filename());
        addTextPart(builder, "business_id", businessId);
        addTextPart(builder, "document_type", documentType);
        return client.post()
                .uri("/api/v1/documents/upload")
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(BodyInserters.fromMultipartData(builder.build()))
                .retrieve()
                .bodyToMono(JsonNode.class)
                .timeout(properties.getResponseTimeout())
                .onErrorMap(this::toGatewayException);
    }

    /** 转发受租户和用户边界约束的 Agent 请求，不允许客户端指定内部服务权限。 */
    public Mono<JsonNode> runAgent(JsonNode request, String tenantId, String userId, String requestId) {
        ensureEnabled();
        return client.post()
                .uri("/api/v1/agent/run")
                .contentType(MediaType.APPLICATION_JSON)
                .header("X-Tenant-Id", tenantId)
                .header("X-User-Id", userId)
                .header("X-Request-Id", requestId)
                .header("X-Permissions", properties.getPermissions())
                .bodyValue(request)
                .retrieve()
                .bodyToMono(JsonNode.class)
                .timeout(properties.getResponseTimeout())
                .onErrorMap(this::toGatewayException);
    }

    /** 仅在元数据有实际值时添加 multipart 文本字段，避免发送无意义空部件。 */
    private void addTextPart(MultipartBodyBuilder builder, String name, String value) {
        if (value != null && !value.isBlank()) {
            builder.part(name, value);
        }
    }

    /** 在调用网络前校验集成开关，禁用时返回明确的服务不可用错误。 */
    private void ensureEnabled() {
        if (!properties.isEnabled()) {
            throw new GatewayException(HttpStatus.SERVICE_UNAVAILABLE, "rag-agent-service integration is disabled.");
        }
    }

    /** 将 RAG 网络或响应异常映射为不泄露上游实现细节的网关错误。 */
    private Throwable toGatewayException(Throwable throwable) {
        if (throwable instanceof GatewayException) {
            return throwable;
        }
        if (throwable instanceof WebClientResponseException ex) {
            String body = ex.getResponseBodyAsString();
            String message = body == null || body.isBlank() ? ex.getMessage() : body;
            return new GatewayException(HttpStatus.BAD_GATEWAY, "rag-agent-service failed: " + message);
        }
        return new GatewayException(HttpStatus.BAD_GATEWAY, "rag-agent-service unavailable: " + throwable.getMessage());
    }
}
