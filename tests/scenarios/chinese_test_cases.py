"""Thirty realistic Chinese memory scenarios for extraction/recall evaluation."""

CHINESE_TEST_CASES = [
    {"input": "我喜欢深色模式", "expected": "深色模式", "status": "superseded"},
    {"input": "以后用浅色模式", "expected": "浅色模式", "status": "active"},
    {"input": "api-x 服务现在挂了", "expected": "api-x", "status": "expired"},
    {"input": "监控显示 api-x 已恢复", "expected": "已恢复", "status": "active"},
    {"input": "配置中心说端口是 8080", "expected": "8080", "status": "disputed"},
    {"input": "部署文档说端口是 9090", "expected": "9090", "status": "disputed"},
    {"input": "忘掉我的家庭地址", "expected": "家庭地址", "status": "expired"},
    {"input": "记住发布前先跑回归测试", "expected": "回归测试", "status": "active"},
    {"input": "这次迁移沿用上次的灰度方案", "expected": "灰度方案", "status": "active"},
    {"input": "那个项目下周上线", "expected": "下周上线", "status": "active"},
    {"input": "项目是支付网关", "expected": "支付网关", "status": "active"},
    {"input": "数据库使用 PG", "expected": "PostgreSQL", "status": "active"},
    {"input": "postgres 开启逻辑复制", "expected": "PostgreSQL", "status": "active"},
    {"input": "PostgreSQL 主库在上海", "expected": "PostgreSQL", "status": "active"},
    {"input": "我喜欢简短的回答", "expected": "简短", "status": "active"},
    {"input": "我现在更喜欢详细解释", "expected": "详细解释", "status": "superseded"},
    {"input": "缓存命中率现在只有 20%", "expected": "20%", "status": "expired"},
    {"input": "缓存命中率已恢复到 95%", "expected": "95%", "status": "active"},
    {"input": "产品经理确认截止日是周五", "expected": "周五", "status": "active"},
    {"input": "旧邮件写截止日是周三", "expected": "周三", "status": "superseded"},
    {"input": "记住测试环境不能发短信", "expected": "不能发短信", "status": "active"},
    {"input": "忘掉旧的测试账号", "expected": "测试账号", "status": "expired"},
    {"input": "类似任务优先复用数据校验脚本", "expected": "校验脚本", "status": "active"},
    {"input": "那个服务是订单 API", "expected": "订单 API", "status": "active"},
    {"input": "订单 API 目前超时", "expected": "超时", "status": "expired"},
    {"input": "运维说节点有三台", "expected": "三台", "status": "disputed"},
    {"input": "资产系统显示节点有四台", "expected": "四台", "status": "disputed"},
    {"input": "我喜欢用 uv 管理 Python 项目", "expected": "uv", "status": "active"},
    {"input": "记住日志默认做脱敏", "expected": "日志默认做脱敏", "status": "active"},
    {"input": "生产环境现在禁止部署", "expected": "禁止部署", "status": "expired"},
]


def test_chinese_memory_scenarios_have_complete_expectations() -> None:
    """每条中文记忆场景都应包含完整且有效的行为预期。"""
    assert len(CHINESE_TEST_CASES) == 30
    assert all(set(case) == {"input", "expected", "status"} for case in CHINESE_TEST_CASES)
    assert {case["status"] for case in CHINESE_TEST_CASES} == {
        "active",
        "disputed",
        "expired",
        "superseded",
    }
