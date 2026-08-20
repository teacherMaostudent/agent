package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import com.zxf.ai.gateway.admission.AdmissionRejectedException;
import com.zxf.ai.gateway.model.ProviderRateLimitedException;
import com.zxf.ai.gateway.usage.QuotaExceededException;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;
import java.util.Map;

/**
 * 全局异常处理器。
 *
 * <p>Controller 和 Service 层只需要抛出异常，不需要关心 HTTP 响应格式。
 * 这里统一转换成包含 timestamp/status/error/message 的 JSON，方便前端和调用方解析。</p>
 */
@RestControllerAdvice
public class GlobalExceptionHandler {
    @ExceptionHandler(AdmissionRejectedException.class)
    /** 将准入拒绝返回为带 Retry-After 的 429，指导调用方退避而非盲目重试。 */
    public ResponseEntity<Map<String, Object>> admissionRejected(AdmissionRejectedException exception) {
        return ResponseEntity.status(exception.status())
                .header(HttpHeaders.RETRY_AFTER, String.valueOf(exception.retryAfterSeconds()))
                .body(Map.of(
                        "timestamp", Instant.now().toString(),
                        "status", exception.status().value(),
                        "error", exception.status().getReasonPhrase(),
                        "message", exception.getMessage(),
                        "reasonCode", exception.reasonCode(),
                        "scope", exception.scope(),
                        "retryAfterSeconds", exception.retryAfterSeconds(),
                        "configuredLimit", exception.configuredLimit(),
                        "observedUsage", exception.observedUsage(),
                        "requestedAmount", exception.requestedAmount()
                ));
    }
    @ExceptionHandler(ProviderRateLimitedException.class)
    /** 将供应商 429 与网关本地准入 429 显式区分，客户端可据此选择不同退避和告警策略。 */
    public ResponseEntity<Map<String, Object>> providerRateLimited(ProviderRateLimitedException exception) {
        return ResponseEntity.status(exception.status())
                .header(HttpHeaders.RETRY_AFTER, String.valueOf(exception.retryAfterSeconds()))
                .body(Map.of(
                        "timestamp", Instant.now().toString(),
                        "status", exception.status().value(),
                        "error", exception.status().getReasonPhrase(),
                        "message", exception.getMessage(),
                        "reasonCode", exception.reasonCode(),
                        "retryAfterSeconds", exception.retryAfterSeconds()
                ));
    }
    @ExceptionHandler(QuotaExceededException.class)
    /** 返回不可按秒自动恢复的日配额拒绝，避免客户端把它误当作短时 RPM 限流。 */
    public ResponseEntity<Map<String, Object>> quotaExceeded(QuotaExceededException exception) {
        return ResponseEntity.status(exception.status()).body(Map.of(
                "timestamp", Instant.now().toString(),
                "status", exception.status().value(),
                "error", exception.status().getReasonPhrase(),
                "message", exception.getMessage(),
                "reasonCode", exception.reasonCode(),
                "configuredLimit", exception.configuredLimit(),
                "observedUsage", exception.observedUsage(),
                "requestedAmount", exception.requestedAmount()
        ));
    }
    @ExceptionHandler(PlatformServiceClient.PlatformServiceException.class)
    /**
     * 保留下游平台服务的稳定状态码与可重试标记，同时移除内部响应细节。
    */
    public ResponseEntity<JsonNode> platformService(PlatformServiceClient.PlatformServiceException error) {
        return ResponseEntity.status(error.status()).body(error.body());
    }
    @ExceptionHandler(GatewayException.class)
    /**
     * 把已分类 GatewayException 映射为稳定 HTTP 状态与错误码，保留其可重试语义。
    */
    public ResponseEntity<Map<String, Object>> gatewayException(GatewayException exception) {
        return ResponseEntity.status(exception.status()).body(error(exception.status(), exception.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    /**
     * 把输入或配置校验错误映射为 400，避免以 500 暴露为平台故障。
    */
    public ResponseEntity<Map<String, Object>> illegalArgument(IllegalArgumentException exception) {
        return ResponseEntity.badRequest().body(error(HttpStatus.BAD_REQUEST, exception.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    /**
     * 记录关联 ID 后返回通用 500，禁止把未分类异常和堆栈暴露给调用方。
    */
    public ResponseEntity<Map<String, Object>> unknown(Exception exception) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(error(HttpStatus.INTERNAL_SERVER_ERROR, exception.getMessage()));
    }

    /**
     * 构建不含堆栈和敏感上游正文的稳定错误响应，并附带可关联请求 ID。
    */
    private Map<String, Object> error(HttpStatus status, String message) {
        return Map.of(
                "timestamp", Instant.now().toString(),
                "status", status.value(),
                "error", status.getReasonPhrase(),
                "message", message == null ? "" : message
        );
    }
}
