package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.knuddels.jtokkit.Encodings;
import com.knuddels.jtokkit.api.Encoding;
import com.knuddels.jtokkit.api.EncodingRegistry;
import com.knuddels.jtokkit.api.EncodingType;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.stereotype.Component;

import java.util.Iterator;
import java.util.Locale;

@Component
public class TokenEstimator {
    private static final int OPENAI_MESSAGE_OVERHEAD = 3;
    private static final int OPENAI_REPLY_PRIMER = 3;
    private static final int OPENAI_NAME_OVERHEAD = 1;
    private static final int ANTHROPIC_MESSAGE_OVERHEAD = 5;
    private static final int GENERIC_MESSAGE_OVERHEAD = 4;

    private final EncodingRegistry registry = Encodings.newDefaultEncodingRegistry();
    private final Encoding cl100k = registry.getEncoding(EncodingType.CL100K_BASE);

    public long estimatePromptTokens(JsonNode request) {
        return estimatePromptTokens(null, request);
    }

    public long estimatePromptTokens(ModelEndpoint endpoint, JsonNode request) {
        TokenStrategy strategy = strategy(endpoint, modelName(endpoint, request));
        JsonNode messages = request.path("messages");
        if (!messages.isArray()) {
            return Math.max(1, strategy.countText(request.toString()));
        }
        long tokens = strategy.replyPrimer();
        for (JsonNode message : messages) {
            tokens += strategy.messageOverhead();
            tokens += strategy.countText(message.path("role").asText(""));
            if (message.hasNonNull("name")) {
                tokens += strategy.nameOverhead();
                tokens += strategy.countText(message.path("name").asText(""));
            }
            tokens += countContent(strategy, message.path("content"));
            if (message.has("tool_calls")) {
                tokens += strategy.countText(message.path("tool_calls").toString());
            }
            if (message.has("tool_call_id")) {
                tokens += strategy.countText(message.path("tool_call_id").asText(""));
            }
        }
        return Math.max(1, tokens);
    }

    public long estimateCompletionTokens(String text) {
        return estimateCompletionTokens(null, text);
    }

    public long estimateCompletionTokens(ModelEndpoint endpoint, String text) {
        if (text == null || text.isBlank()) {
            return 0;
        }
        return Math.max(1, strategy(endpoint, endpoint == null ? "" : endpoint.upstreamModel()).countText(text));
    }

    private long countContent(TokenStrategy strategy, JsonNode content) {
        if (content == null || content.isMissingNode() || content.isNull()) {
            return 0;
        }
        if (content.isTextual()) {
            return strategy.countText(content.asText(""));
        }
        if (content.isArray()) {
            long tokens = 0;
            for (JsonNode part : content) {
                tokens += countContentPart(strategy, part);
            }
            return tokens;
        }
        return strategy.countText(content.toString());
    }

    private long countContentPart(TokenStrategy strategy, JsonNode part) {
        String type = part.path("type").asText("");
        if ("text".equals(type) || "input_text".equals(type)) {
            return strategy.countText(part.path("text").asText(""));
        }
        if ("image_url".equals(type) || "input_image".equals(type)) {
            return estimateImageTokens(part);
        }
        if ("tool_result".equals(type)) {
            return strategy.countText(part.toString());
        }
        return strategy.countText(part.toString());
    }

    private long estimateImageTokens(JsonNode part) {
        String detail = part.path("image_url").path("detail").asText(part.path("detail").asText("auto"));
        return "low".equalsIgnoreCase(detail) ? 85 : 255;
    }

    private TokenStrategy strategy(ModelEndpoint endpoint, String model) {
        String provider = endpoint == null ? "" : endpoint.providerName().toLowerCase(Locale.ROOT);
        String normalizedModel = model == null ? "" : model.toLowerCase(Locale.ROOT);
        if (provider.equals("openai") || normalizedModel.startsWith("gpt-") || normalizedModel.contains("gpt-4o")) {
            return new JtokkitStrategy(cl100k, OPENAI_MESSAGE_OVERHEAD, OPENAI_REPLY_PRIMER, OPENAI_NAME_OVERHEAD);
        }
        if (provider.equals("qwen") || provider.equals("deepseek") || provider.equals("kimi")
                || normalizedModel.contains("qwen") || normalizedModel.contains("deepseek") || normalizedModel.contains("moonshot")) {
            return new CjkAwareStrategy(GENERIC_MESSAGE_OVERHEAD, OPENAI_REPLY_PRIMER, 1);
        }
        if (provider.equals("claude") || provider.equals("anthropic") || normalizedModel.contains("claude")) {
            return new CjkAwareStrategy(ANTHROPIC_MESSAGE_OVERHEAD, 0, 1);
        }
        if (provider.equals("ollama") || provider.equals("vllm") || normalizedModel.contains("llama")) {
            return new CjkAwareStrategy(GENERIC_MESSAGE_OVERHEAD, 0, 1);
        }
        return new CjkAwareStrategy(GENERIC_MESSAGE_OVERHEAD, OPENAI_REPLY_PRIMER, 1);
    }

    private String modelName(ModelEndpoint endpoint, JsonNode request) {
        if (endpoint != null) {
            return endpoint.upstreamModel();
        }
        return request == null ? "" : request.path("model").asText("");
    }

    private interface TokenStrategy {
        long countText(String text);

        int messageOverhead();

        int replyPrimer();

        int nameOverhead();
    }

    private record JtokkitStrategy(
            Encoding encoding,
            int messageOverhead,
            int replyPrimer,
            int nameOverhead
    ) implements TokenStrategy {
        @Override
        public long countText(String text) {
            return text == null || text.isBlank() ? 0 : encoding.countTokens(text);
        }
    }

    private record CjkAwareStrategy(
            int messageOverhead,
            int replyPrimer,
            int nameOverhead
    ) implements TokenStrategy {
        @Override
        public long countText(String text) {
            if (text == null || text.isBlank()) {
                return 0;
            }
            long tokens = 0;
            StringBuilder asciiRun = new StringBuilder();
            Iterator<Integer> codePoints = text.codePoints().iterator();
            while (codePoints.hasNext()) {
                int codePoint = codePoints.next();
                if (isCjk(codePoint)) {
                    tokens += flushAscii(asciiRun);
                    tokens += 1;
                } else if (Character.isWhitespace(codePoint)) {
                    tokens += flushAscii(asciiRun);
                } else if (isAsciiLetterOrDigit(codePoint) || codePoint == '_' || codePoint == '-') {
                    asciiRun.appendCodePoint(codePoint);
                } else {
                    tokens += flushAscii(asciiRun);
                    tokens += 1;
                }
            }
            tokens += flushAscii(asciiRun);
            return tokens;
        }

        private long flushAscii(StringBuilder asciiRun) {
            if (asciiRun.isEmpty()) {
                return 0;
            }
            int length = asciiRun.length();
            asciiRun.setLength(0);
            return Math.max(1, (length + 3) / 4);
        }

        private boolean isAsciiLetterOrDigit(int codePoint) {
            return codePoint < 128 && Character.isLetterOrDigit(codePoint);
        }

        private boolean isCjk(int codePoint) {
            Character.UnicodeScript script = Character.UnicodeScript.of(codePoint);
            return script == Character.UnicodeScript.HAN
                    || script == Character.UnicodeScript.HIRAGANA
                    || script == Character.UnicodeScript.KATAKANA
                    || script == Character.UnicodeScript.HANGUL;
        }
    }
}
