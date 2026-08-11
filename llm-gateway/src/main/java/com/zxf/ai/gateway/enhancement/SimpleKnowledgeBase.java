package com.zxf.ai.gateway.enhancement;

import dev.langchain4j.rag.content.Content;
import dev.langchain4j.rag.content.retriever.ContentRetriever;
import dev.langchain4j.rag.query.Query;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * 演示用轻量知识库。
 *
 * <p>为了让 RAG 能在本地直接跑起来，这里先使用内存文档和简单关键词打分。
 * 生产环境可以把这个类替换成 Milvus、Elasticsearch、pgvector、Redis Vector、
 * 阿里云 OpenSearch 或华为云 CSS，并使用 embedding 相似度检索。</p>
 */
@Component
@ConditionalOnProperty(prefix = "enhancement.langchain4j", name = "enabled", havingValue = "true")
public class SimpleKnowledgeBase implements ContentRetriever {
    private final List<KnowledgeDocument> documents = List.of(
            new KnowledgeDocument("gateway-architecture", """
                    LLM Gateway 分为 Web 接入层、业务编排层、模型路由层、上游模型客户端层、
                    用量成本层和配置观测层。Web 接入层暴露 OpenAI 兼容接口；业务编排层负责
                    token 估算、限额、fallback、成本统计和日志；模型路由层把逻辑模型解析成
                    provider:model；客户端层调用 DeepSeek/OpenAI 等 OpenAI Compatible API。
                    """),
            new KnowledgeDocument("rag-design", """
                    RAG 检索增强生成适合企业知识库问答。典型链路是：用户问题 -> 关键词或向量检索 ->
                    召回相关文档片段 -> 拼接上下文 -> 调用大模型生成答案。它能降低幻觉，并让模型
                    回答私有知识、项目文档、接口说明、运维手册等内容。
                    """),
            new KnowledgeDocument("tool-calling-design", """
                    Tool Calling 适合把确定性后端能力暴露给大模型，例如查询订单、读取用户配额、
                    计算成本、查询模型状态、调用内部 API。模型负责判断是否需要工具，工具方法负责
                    返回可靠数据，最终由模型把工具结果组织成自然语言。
                    """),
            new KnowledgeDocument("webflux-reason", """
                    Spring WebFlux 适合 LLM Gateway，因为大模型调用是 IO 密集型场景，且流式输出
                    会保持较长连接。WebFlux 使用 Mono/Flux 和非阻塞 IO，可以在等待上游模型响应时
                    减少线程占用，更适合并发聊天和 SSE 输出。
                    """)
    );

    /**
     * LangChain4j RAG 标准入口。
     *
     * <p>AiServices 调用模型前会先调用这个方法取回相关内容，再把内容注入到 prompt 中。
     * 这里返回的是 Content 列表，后续替换成向量数据库时 Controller 和 Service 都不用改。</p>
     */
    @Override
    public List<Content> retrieve(Query query) {
        String normalizedQuestion = normalize(query.text());
        return documents.stream()
                .map(document -> new ScoredDocument(document, score(normalizedQuestion, document)))
                .filter(scored -> scored.score() > 0)
                .sorted(Comparator.comparingInt(ScoredDocument::score).reversed())
                .limit(3)
                .map(scored -> Content.from("文档ID: " + scored.document().id() + "\n" + scored.document().content()))
                .toList();
    }

    /** 将召回内容合成为可展示上下文，并对返回条数施加调用方给定上限。 */
    public String retrieveContext(String question, int limit) {
        List<Content> contents = retrieve(Query.from(question)).stream()
                .limit(limit)
                .toList();
        if (contents.isEmpty()) {
            return "未检索到强相关知识库片段，请基于通用后端和大模型工程经验回答。";
        }
        return contents.stream()
                .map(content -> content.textSegment().text())
                .reduce((left, right) -> left + "\n---\n" + right)
                .orElse("");
    }

    /** 按归一化关键词重合计算演示检索分数，不作为生产语义相关性算法。 */
    private int score(String question, KnowledgeDocument document) {
        String content = normalize(document.content());
        int score = 0;
        for (String token : question.split("\\s+")) {
            if (!token.isBlank() && content.contains(token)) {
                score++;
            }
        }
        if (content.contains(question)) {
            score += 5;
        }
        return score;
    }

    /** 归一化文本以消除大小写和常见标点对演示检索的影响。 */
    private String normalize(String text) {
        return text == null ? "" : text.toLowerCase(Locale.ROOT)
                .replaceAll("[，。！？、；：,.!?;:()（）/\\\\-]", " ")
                .replaceAll("\\s+", " ")
                .trim();
    }

    private record KnowledgeDocument(String id, String content) {
    }

    private record ScoredDocument(KnowledgeDocument document, int score) {
    }
}
