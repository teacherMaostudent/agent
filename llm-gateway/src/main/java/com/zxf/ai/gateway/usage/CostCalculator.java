package com.zxf.ai.gateway.usage;

import com.fasterxml.jackson.databind.JsonNode;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.MathContext;
import java.util.Comparator;

@Component
public class CostCalculator {
    private final GatewayProperties properties;

    /** 注入包含币种、汇率与价格阶梯的版本化计费配置。 */
    public CostCalculator(GatewayProperties properties) {
        this.properties = properties;
    }
    /**
     * 根据模型单价估算本次调用成本。
     *
     * <p>价格按每 1000 token 配置，因此计算时要除以 1000。这里返回 BigDecimal，
     * 避免使用 double 带来的金额精度问题。</p>
     */
    /** 按模型目录价格估算输入和输出 Token 成本，并使用精确小数避免金额误差。 */
    public BigDecimal estimate(ModelEndpoint endpoint, long promptTokens, long completionTokens) {
        return estimate(endpoint, promptTokens, completionTokens, 0, 0);
    }

    /**
     * 按厂商 usage 中的缓存细分计算成本。OpenAI/DeepSeek 的 prompt_tokens 包含 cache hit，
     * Anthropic 的 input_tokens、cache_read_input_tokens、cache_creation_input_tokens 分列返回。
     */
    /** 读取供应商 Usage 中的缓存读写字段，再按相应阶梯价格核算真实成本。 */
    public BigDecimal estimate(ModelEndpoint endpoint, JsonNode response,
                               long promptTokens, long completionTokens) {
        JsonNode usage = response == null ? null : response.path("usage");
        long cacheRead = usage == null ? 0 : firstLong(usage,
                "prompt_cache_hit_tokens", "cache_read_input_tokens");
        if (cacheRead == 0 && usage != null) {
            cacheRead = usage.path("prompt_tokens_details").path("cached_tokens").asLong(0);
        }
        long cacheWrite = usage == null ? 0 : usage.path("cache_creation_input_tokens").asLong(0);
        return estimate(endpoint, promptTokens, completionTokens, cacheRead, cacheWrite);
    }

    /** 统一计算普通输入、缓存读写和输出 Token 的成本，并折算为平台基础币种。 */
    private BigDecimal estimate(ModelEndpoint endpoint, long promptTokens, long completionTokens,
                                long cacheReadTokens, long cacheWriteTokens) {
        Price price = price(endpoint, promptTokens);
        long normalInputTokens = Math.max(0, promptTokens - cacheReadTokens - cacheWriteTokens);
        BigDecimal input = price.input()
                .multiply(BigDecimal.valueOf(normalInputTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        BigDecimal cacheRead = price.cacheRead().multiply(BigDecimal.valueOf(cacheReadTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        BigDecimal cacheWrite = price.cacheWrite().multiply(BigDecimal.valueOf(cacheWriteTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        BigDecimal output = price.output()
                .multiply(BigDecimal.valueOf(completionTokens))
                .divide(BigDecimal.valueOf(1000), MathContext.DECIMAL64);
        BigDecimal nativeCost = input.add(cacheRead).add(cacheWrite).add(output);
        String nativeCurrency = endpoint.model().getCurrency();
        BigDecimal rate = properties.getBilling().getExchangeRates()
                .getOrDefault(nativeCurrency, BigDecimal.ONE);
        return nativeCost.multiply(rate, MathContext.DECIMAL64);
    }

    /** 返回 Runtime 预算和 Governance 成本比较统一使用的基础币种。 */
    public String baseCurrency() {
        return properties.getBilling().getBaseCurrency();
    }

    /** 从多个供应商字段别名中读取第一个存在的非负用量。 */
    private long firstLong(JsonNode node, String... fields) {
        for (String field : fields) {
            if (node.has(field)) return node.path(field).asLong(0);
        }
        return 0;
    }

    /** 按输入 Token 阈值选择最小适用价格阶梯，并继承缺失的缓存价格。 */
    private Price price(ModelEndpoint endpoint, long promptTokens) {
        GatewayProperties.Model model = endpoint.model();
        GatewayProperties.PriceTier tier = model.getPriceTiers().stream()
                .filter(candidate -> promptTokens <= candidate.getMaxInputTokens())
                .min(Comparator.comparingLong(GatewayProperties.PriceTier::getMaxInputTokens))
                .orElse(null);
        BigDecimal input = tier != null && tier.getInputPricePer1k() != null
                ? tier.getInputPricePer1k() : model.getInputPricePer1k();
        BigDecimal output = tier != null && tier.getOutputPricePer1k() != null
                ? tier.getOutputPricePer1k() : model.getOutputPricePer1k();
        BigDecimal cacheRead = tier != null && tier.getCachedInputPricePer1k() != null
                ? tier.getCachedInputPricePer1k() : model.getCachedInputPricePer1k();
        BigDecimal cacheWrite = tier != null && tier.getCacheWritePricePer1k() != null
                ? tier.getCacheWritePricePer1k() : model.getCacheWritePricePer1k();
        return new Price(input, output,
                cacheRead == null ? input : cacheRead,
                cacheWrite == null ? input : cacheWrite);
    }

    /** 汇总普通输入、输出和缓存读写的每千 Token 单价。 */
    private record Price(BigDecimal input, BigDecimal output,
                         BigDecimal cacheRead, BigDecimal cacheWrite) {
    }
}
