package com.zxf.ai.gateway.web;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.auth.ApiKeyService;
import com.zxf.ai.gateway.rag.GmpHumanReviewRequest;
import com.zxf.ai.gateway.rag.GmpReviewRequest;
import com.zxf.ai.gateway.rag.GmpReviewService;
import com.zxf.ai.gateway.rag.GmpReviewTask;
import jakarta.validation.Valid;
import org.springframework.http.MediaType;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

import java.util.Map;

@RestController
@RequestMapping
@ConditionalOnProperty(prefix = "rag-agent", name = "enabled", havingValue = "true")
public class GmpReviewController {
    private final GmpReviewService gmpReviewService;
    private final ApiKeyService apiKeyService;

    public GmpReviewController(GmpReviewService gmpReviewService, ApiKeyService apiKeyService) {
        this.gmpReviewService = gmpReviewService;
        this.apiKeyService = apiKeyService;
    }

    @PostMapping(path = "/v1/gmp/documents/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    public Mono<JsonNode> uploadDocument(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestHeader(value = "X-Api-Key", required = false) String xApiKey,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @RequestPart("file") FilePart file,
            @RequestPart(value = "businessId", required = false) String businessId,
            @RequestPart(value = "documentType", required = false) String documentType
    ) {
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(authorization, xApiKey, userId, null);
        return gmpReviewService.uploadDocument(file, businessId, documentType, auth.tenantId(), auth.userId());
    }

    @PostMapping(path = "/v1/gmp/reviews", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    public Mono<GmpReviewTask> startReview(
            @RequestHeader(value = "X-User-Id", required = false) String userId,
            @RequestHeader(value = "X-Api-Key", required = false) String xApiKey,
            @RequestHeader(value = "Authorization", required = false) String authorization,
            @Valid @RequestBody GmpReviewRequest request
    ) {
        ApiKeyService.AuthResult auth = apiKeyService.authenticate(authorization, xApiKey, userId, null);
        return gmpReviewService.startReview(request, auth.tenantId(), auth.userId());
    }

    @GetMapping("/v1/gmp/reviews/{taskId}")
    public GmpReviewTask review(@PathVariable String taskId) {
        return gmpReviewService.task(taskId);
    }

    @PostMapping("/v1/gmp/reviews/{taskId}/refresh")
    public Mono<GmpReviewTask> refresh(@PathVariable String taskId) {
        return gmpReviewService.refresh(taskId);
    }

    @PostMapping("/v1/gmp/reviews/{taskId}/rerun")
    public Mono<GmpReviewTask> rerun(@PathVariable String taskId) {
        return gmpReviewService.rerun(taskId);
    }

    @PostMapping(path = "/v1/gmp/reviews/{taskId}/confirm", consumes = MediaType.APPLICATION_JSON_VALUE, produces = MediaType.APPLICATION_JSON_VALUE)
    public GmpReviewTask confirm(@PathVariable String taskId, @RequestBody GmpHumanReviewRequest request) {
        return gmpReviewService.confirm(taskId, request);
    }

    @GetMapping("/admin/gmp/reviews")
    public Object reviews() {
        return gmpReviewService.tasks();
    }

    @GetMapping("/admin/gmp/reviews/{taskId}")
    public Object adminReview(@PathVariable String taskId) {
        return gmpReviewService.task(taskId);
    }

    @GetMapping("/admin/gmp")
    public Map<String, Object> snapshot() {
        return gmpReviewService.snapshot();
    }
}
