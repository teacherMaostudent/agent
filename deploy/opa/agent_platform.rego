package agent_platform

default allow := false

allow if {
    input.subject.tenant_id != ""
    input.subject.user_id != ""
    startswith(input.request.path, "/api/v1/")
}
