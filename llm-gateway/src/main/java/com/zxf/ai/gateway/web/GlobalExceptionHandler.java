package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.integration.PlatformServiceClient;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
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
    @ExceptionHandler(PlatformServiceClient.PlatformServiceException.class)
    public ResponseEntity<JsonNode> platformService(PlatformServiceClient.PlatformServiceException error) {
        return ResponseEntity.status(error.status()).body(error.body());
    }
    @ExceptionHandler(GatewayException.class)
    public ResponseEntity<Map<String, Object>> gatewayException(GatewayException exception) {
        return ResponseEntity.status(exception.status()).body(error(exception.status(), exception.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, Object>> illegalArgument(IllegalArgumentException exception) {
        return ResponseEntity.badRequest().body(error(HttpStatus.BAD_REQUEST, exception.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, Object>> unknown(Exception exception) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(error(HttpStatus.INTERNAL_SERVER_ERROR, exception.getMessage()));
    }

    private Map<String, Object> error(HttpStatus status, String message) {
        return Map.of(
                "timestamp", Instant.now().toString(),
                "status", status.value(),
                "error", status.getReasonPhrase(),
                "message", message == null ? "" : message
        );
    }
}
