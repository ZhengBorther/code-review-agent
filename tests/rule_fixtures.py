from review_agent.rules import ReviewRule

VALID_GO_RULE = """---
id: GO-STYLE-001
title: 函数参数超过4个时必须使用结构体封装传参
language: go
domains: [STYLE]
severity: warning
prompt_hint: >
  检查新增/修改的函数签名：当参数数量大于4个时必须封装。
deprecated: false
---
# GO-STYLE-001 函数参数超过4个时必须使用结构体封装传参

## 规则说明

- 当函数参数数量大于 4 个时，必须使用结构体封装传参。
"""

def make_rule(rule_id="GO-STYLE-001", language="go", deprecated=False, **changes):
    values = dict(id=rule_id, title="Rule title", language=language,
                  domains=("STYLE",), severity="warning",
                  prompt_hint="Check the changed code", deprecated=deprecated,
                  body="# Rule body", source=f"{rule_id}.mdr")
    values.update(changes)
    return ReviewRule(**values)

GO_DIFF = ("diff --git a/internal/user.go b/internal/user.go\n"
           "--- a/internal/user.go\n+++ b/internal/user.go\n"
           "+func CreateUser(name string, age int, role string, active bool, region string) {}\n")
PY_DIFF = ("diff --git a/service.py b/service.py\n--- a/service.py\n+++ b/service.py\n"
           "+def create_user(): pass\n")
MIXED_DIFF = GO_DIFF + PY_DIFF
