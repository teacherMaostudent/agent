package com.zxf.ai.gateway.prompt;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.util.Iterator;
import java.util.Map;

@Component
public class PromptTemplateService {
    private final GatewayProperties properties;
    private final ObjectMapper objectMapper;

    public PromptTemplateService(GatewayProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
    }

    /**
     * 将请求中的 prompt_template/template_id + variables 渲染成标准 OpenAI messages。
     *
     * <p>调用方有两种写法：</p>
     * <ul>
     *     <li>直接传 messages：适合已经自己拼好 system/user/assistant 对话的调用方。</li>
     *     <li>传 prompt_template + variables：适合把固定 Prompt 模板沉淀在网关配置里，业务方只传变量。</li>
     * </ul>
     *
     * <p>prompt_template/template_id 是模板编号，例如 application.yml 中的 interview-answer。
     * 它决定“这次请求要套用哪一套 system prompt 和 user prompt”。</p>
     *
     * <p>variables 是模板变量，例如 {"topic":"模型路由","project":"LLM Gateway"}。
     * 它只提供可变业务信息，不负责描述角色、格式、约束。角色、格式、输出风格等稳定约束应放在模板里。</p>
     *
     * <p>渲染后会把模板生成的 system/user message 放在原 messages 前面。
     * 这样既能保留统一 Prompt 约束，也允许调用方继续追加本轮上下文。</p>
     */
    public JsonNode apply(JsonNode request) {
        String templateId = request.path("prompt_template").asText(request.path("template_id").asText(""));
        if (templateId.isBlank()) {
            // 没有指定模板时，说明调用方希望完全自己控制 messages，网关不改写 Prompt。
            return request;
        }
        GatewayProperties.PromptTemplate template = properties.getPromptTemplates().get(templateId);
        if (template == null) {
            throw new GatewayException(HttpStatus.BAD_REQUEST, "Unknown prompt template: " + templateId);
        }
        ObjectNode copy = request.deepCopy();
        // variables 必须是 JSON Object。非 Object 时视为空变量，避免数组或字符串误参与模板替换。
        ObjectNode variables = copy.path("variables").isObject() ? (ObjectNode) copy.path("variables") : objectMapper.createObjectNode();
        ArrayNode messages = objectMapper.createArrayNode();
        if (template.getSystem() != null && !template.getSystem().isBlank()) {
            // system 模板一般放角色、边界、输出风格、禁止事项等稳定约束。
            messages.addObject().put("role", "system").put("content", render(template.getSystem(), variables));
        }
        if (template.getUser() != null && !template.getUser().isBlank()) {
            // user 模板一般放具体任务问法，并用 {{变量名}} 接收业务字段。
            messages.addObject().put("role", "user").put("content", render(template.getUser(), variables));
        }
        if (copy.path("messages").isArray()) {
            // 原始 messages 追加在模板后面，表示模板是统一前置约束，原 messages 是本次请求上下文。
            copy.path("messages").forEach(messages::add);
        }
        copy.set("messages", messages);
        // 删除模板专用字段，保证发给上游模型的是标准 OpenAI-compatible payload。
        copy.remove("prompt_template");
        copy.remove("template_id");
        copy.remove("variables");
        return copy;
    }

    /**
     * 管理端查看当前所有 Prompt 模板。
     *
     * <p>这里返回的是配置快照，面试时可以说 Prompt 模板已经从业务代码里抽离，
     * 业务只传 template id 和变量，便于统一调整和版本治理。</p>
     */
    public Map<String, GatewayProperties.PromptTemplate> snapshot() {
        return properties.getPromptTemplates();
    }

    /**
     * 执行最轻量的 {{变量名}} 字符串替换。
     *
     * <p>如果变量值是字符串，就取 asText；如果是对象或数组，就转成 JSON 字符串。
     * 这样可以把复杂结构，例如检索结果列表、CAPA 字段对象，作为模板变量传入。</p>
     */
    private String render(String template, ObjectNode variables) {
        String rendered = template;
        Iterator<Map.Entry<String, JsonNode>> fields = variables.fields();
        while (fields.hasNext()) {
            Map.Entry<String, JsonNode> field = fields.next();
            String value = field.getValue().isTextual() ? field.getValue().asText() : field.getValue().toString();
            rendered = rendered.replace("{{" + field.getKey() + "}}", value);
        }
        return rendered;
    }
}
