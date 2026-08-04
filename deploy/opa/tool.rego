package agent_platform.tool

default allow := {"allow": false, "reason": "tool permission is required"}

allow := {"allow": true} if {
    input.subject.tenant_id != ""
    input.subject.user_id != ""
    permission := sprintf("tool:%s", [input.resource.name])
    permission in input.subject.permissions
}

allow := {"allow": true} if {
    "tool:*" in input.subject.permissions
}
