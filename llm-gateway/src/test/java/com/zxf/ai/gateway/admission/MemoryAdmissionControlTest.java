package com.zxf.ai.gateway.admission;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zxf.ai.gateway.config.GatewayProperties;
import com.zxf.ai.gateway.model.GatewayRequestContext;
import com.zxf.ai.gateway.model.ModelEndpoint;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThat;

/** 验证本地准入实现的关键拒绝、并发释放和输入保护语义。 */
class MemoryAdmissionControlTest {
    private final GatewayProperties properties = new GatewayProperties();

    @Test
    void rejectsSecondRequestWhenUserRpmIsExhausted() {
        properties.getAdmission().setUserRequestsPerMinute(1);
        MemoryAdmissionControl admission = new MemoryAdmissionControl(properties);
        GatewayRequestContext context = GatewayRequestContext.create("request", "tenant", "user", "model", false);

        admission.admitIngress(context).release();

        assertThatThrownBy(() -> admission.admitIngress(context))
                .isInstanceOfSatisfying(AdmissionRejectedException.class, rejected -> {
                    assertThat(rejected.reasonCode()).isEqualTo("ADMISSION_USER_RPM");
                    assertThat(rejected.scope()).isEqualTo("user");
                    assertThat(rejected.configuredLimit()).isEqualTo(1);
                    assertThat(rejected.observedUsage()).isEqualTo(1);
                });
    }

    @Test
    void concurrentLeaseIsReleasedOnlyOnce() {
        properties.getAdmission().setUserMaxConcurrency(1);
        MemoryAdmissionControl admission = new MemoryAdmissionControl(properties);
        GatewayRequestContext context = GatewayRequestContext.create("request", "tenant", "user", "model", false);
        AdmissionLease first = admission.admitIngress(context);

        assertThatThrownBy(() -> admission.admitIngress(context))
                .isInstanceOfSatisfying(AdmissionRejectedException.class,
                        rejected -> assertThat(rejected.reasonCode()).isEqualTo("ADMISSION_USER_CONCURRENCY"));
        first.release();
        first.release();
        assertThatCode(() -> admission.admitIngress(context).release()).doesNotThrowAnyException();
    }

    @Test
    void rejectsRequestAboveConfiguredMessageOrTokenBounds() throws Exception {
        properties.getAdmission().setMaxMessages(1);
        properties.getAdmission().setMaxPromptTokens(10);
        MemoryAdmissionControl admission = new MemoryAdmissionControl(properties);
        ObjectMapper mapper = new ObjectMapper();

        assertThatThrownBy(() -> admission.validateRequest(mapper.readTree("{\"messages\":[{},{}]}")))
                .hasMessageContaining("Message count");
        assertThatThrownBy(() -> admission.validateTokenBounds(11, 1)).hasMessageContaining("Prompt tokens");
    }
}
