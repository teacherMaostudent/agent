package com.zxf.ai.gateway.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.web.reactive.function.client.WebClient;
import io.netty.handler.ssl.SslContextBuilder;
import reactor.netty.http.client.HttpClient;
import reactor.netty.transport.ProxyProvider;

import java.nio.file.Files;
import java.nio.file.Path;

@Configuration
public class WebClientConfig {
    private static final Logger log = LoggerFactory.getLogger(WebClientConfig.class);

    @Bean
    @Primary
    /**
     * 创建统一连接池、超时、代理和观测过滤器的 WebClient Builder。
    */
    public WebClient.Builder webClientBuilder() {
        HttpClient httpClient = HttpClient.create();
        ProxyConfig proxy = proxyConfig();
        if (proxy.enabled()) {
            // 浏览器能访问外网，不代表 Java WebClient 会自动走系统代理。
            // Reactor Netty 需要显式配置代理，否则在某些网络环境下会出现 Connection timed out。
            log.info("llm_gateway_proxy_enabled host={} port={}", proxy.host(), proxy.port());
            httpClient = httpClient.proxy(spec -> spec
                    .type(ProxyProvider.Proxy.HTTP)
                    .host(proxy.host())
                    .port(proxy.port()));
        }
        return WebClient.builder()
                .clientConnector(new ReactorClientHttpConnector(httpClient));
    }

    /**
     * 为 Governance 与 Control Plane 构建专属 mTLS 客户端。
     *
     * <p>外部模型厂商客户端继续使用普通 {@link #webClientBuilder()}，避免把企业内部 CA 或客户端证书
     * 意外发送给模型供应商。生产缺少任一证书路径时直接拒绝启动，而本地保留普通 HTTP 兼容模式。</p>
     */
    @Bean("platformServiceWebClientBuilder")
    public WebClient.Builder platformServiceWebClientBuilder(
            @Value("${platform-services.mtls.enabled:false}") boolean enabled,
            @Value("${platform-services.mtls.ca-file:}") String caFile,
            @Value("${platform-services.mtls.cert-file:}") String certFile,
            @Value("${platform-services.mtls.key-file:}") String keyFile
    ) {
        if (!enabled) {
            return WebClient.builder();
        }
        try {
            if (caFile.isBlank() || certFile.isBlank() || keyFile.isBlank()
                    || !Files.isRegularFile(Path.of(caFile))
                    || !Files.isRegularFile(Path.of(certFile))
                    || !Files.isRegularFile(Path.of(keyFile))) {
                throw new IllegalStateException("platform service mTLS requires CA, certificate and key files");
            }
            var sslContext = SslContextBuilder.forClient()
                    .trustManager(Path.of(caFile).toFile())
                    .keyManager(Path.of(certFile).toFile(), Path.of(keyFile).toFile())
                    .build();
            HttpClient client = HttpClient.create().secure(ssl -> ssl.sslContext(sslContext));
            return WebClient.builder().clientConnector(new ReactorClientHttpConnector(client));
        } catch (Exception exception) {
            throw new IllegalStateException("cannot initialize platform service mTLS client", exception);
        }
    }

    /**
     * 解析显式 HTTP 代理开关、主机和端口；配置不完整时保持禁用而非猜测。
    */
    private ProxyConfig proxyConfig() {
        // 优先读取 JVM 参数，适合在 IDEA Run Configuration 的 VM options 中配置：
        // -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7897
        // 同时也支持环境变量，方便后续 Docker 或服务器部署。
        String host = firstNonBlank(
                System.getProperty("https.proxyHost"),
                System.getProperty("http.proxyHost"),
                System.getenv("HTTPS_PROXY_HOST"),
                System.getenv("HTTP_PROXY_HOST")
        );
        String portText = firstNonBlank(
                System.getProperty("https.proxyPort"),
                System.getProperty("http.proxyPort"),
                System.getenv("HTTPS_PROXY_PORT"),
                System.getenv("HTTP_PROXY_PORT")
        );
        if (host == null || portText == null) {
            return ProxyConfig.disabled();
        }
        try {
            return new ProxyConfig(true, host, Integer.parseInt(portText));
        } catch (NumberFormatException ex) {
            // 代理端口配置错时不让应用启动失败，而是记录 warning 并回退为直连。
            log.warn("llm_gateway_proxy_invalid_port value={}", portText);
            return ProxyConfig.disabled();
        }
    }

    /**
     * 按配置优先级选择首个非空值，用于显式环境覆盖而不是拼接多个代理来源。
    */
    private String firstNonBlank(String... values) {
        for (String value : values) {
            if (value != null && !value.isBlank()) {
                return value;
            }
        }
        return null;
    }

    /**
     * 解析显式 HTTP 代理开关、主机和端口；配置不完整时保持禁用而非猜测。
    */
    private record ProxyConfig(boolean enabled, String host, int port) {
        /**
         * 创建显式禁用的代理配置，避免使用空主机或隐式系统代理。
        */
        static ProxyConfig disabled() {
            return new ProxyConfig(false, "", 0);
        }
    }
}
