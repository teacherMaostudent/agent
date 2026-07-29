package com.zxf.ai.gateway.model;

import org.springframework.http.HttpStatus;

/**
 * 网关内部统一异常。
 *
 * <p>上游模型、路由、限额、参数校验等错误最终都会转换成这个异常，
 * 由 GlobalExceptionHandler 输出稳定的 JSON 错误格式。</p>
 */
public class GatewayException extends RuntimeException {
    private final HttpStatus status;

    public GatewayException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }
}
