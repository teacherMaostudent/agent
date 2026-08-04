package agent_platform.llm

default allow := {"allow": false, "reason": "authenticated tenant is required"}

allow := {"allow": true} if {
    input.subject.tenant_id != ""
    input.subject.user_id != ""
    input.request.path == "/v1/chat/completions"
}
