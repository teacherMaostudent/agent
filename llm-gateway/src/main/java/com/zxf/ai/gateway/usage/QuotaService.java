package com.zxf.ai.gateway.usage;

import com.zxf.ai.gateway.model.GatewayUsage;

import java.math.BigDecimal;
import java.util.Map;

/**
 * Distributed quota boundary for model usage.
 *
 * <p>{@link #reserve} is a reservation, not a read-only check.  Implementations
 * must make it atomic so concurrent gateway replicas cannot oversubscribe a
 * tenant's daily token or cost limit.</p>
 */
public interface QuotaService {
    /** Reserve the estimated cost before contacting an upstream provider. */
    UsageReservation reserve(String userId, String requestId, long estimatedPromptTokens,
                             long estimatedCompletionTokens, BigDecimal estimatedCost);

    /** Settle the reservation with provider-reported usage after success. */
    void settle(String userId, UsageReservation reservation, GatewayUsage gatewayUsage);

    /** Release a reservation when no billable upstream result was produced. */
    void release(String userId, UsageReservation reservation);

    /** Return an operational snapshot; it is not an authorization decision. */
    Map<String, Object> snapshot(String userId);
}
