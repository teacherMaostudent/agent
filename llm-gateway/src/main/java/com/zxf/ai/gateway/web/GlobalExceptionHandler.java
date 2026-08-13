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
     * 执行 platform service 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public ResponseEntity<JsonNode> platformService(PlatformServiceClient.PlatformServiceException error) {
        return ResponseEntity.status(error.status()).body(error.body());
    }
    @ExceptionHandler(GatewayException.class)
    /**
     * 执行 gateway exception 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public ResponseEntity<Map<String, Object>> gatewayException(GatewayException exception) {
        return ResponseEntity.status(exception.status()).body(error(exception.status(), exception.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    /**
     * 执行 illegal argument 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public ResponseEntity<Map<String, Object>> illegalArgument(IllegalArgumentException exception) {
        return ResponseEntity.badRequest().body(error(HttpStatus.BAD_REQUEST, exception.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    /**
     * 执行 unknown 对应的受控业务步骤，并保持网关边界与状态约束。
    */
    public ResponseEntity<Map<String, Object>> unknown(Exception exception) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(error(HttpStatus.INTERNAL_SERVER_ERROR, exception.getMessage()));
    }

    /**
     * 执行 error 对应的受控业务步骤，并保持网关边界与状态约束。
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
