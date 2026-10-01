#!/usr/bin/env python3
"""
优化自动切换逻辑 - 修复节点名称解析问题
"""
import re

def safe_node_name(name: str) -> bool:
    """检查节点名称是否安全（不包含特殊节点）"""
    if not isinstance(name, str) or not name:
        return False

    # 排除非实际节点
    exclude_patterns = [
        r'^剩余流量',
        r'^到期时间',
        r'^套餐',
        r'^DIRECT$',
        r'^REJECT$',
        r'^PASS$',
    ]

    for pattern in exclude_patterns:
        if re.search(pattern, name, re.IGNORECASE):
            return False

    return True


def next_proxy_candidate_improved(group: dict, failed: dict[str, float], now: float) -> str | None:
    """改进的节点选择 - 过滤特殊节点"""
    current = group.get('now')
    members = group.get('all') or []

    if not isinstance(current, str) or not current or not isinstance(members, list):
        return None

    # 过滤出有效节点
    valid_members = [m for m in members if isinstance(m, str) and safe_node_name(m)]

    if not valid_members:
        return None

    try:
        start = valid_members.index(current)
    except ValueError:
        start = -1

    # 从当前节点的下一个开始查找
    for offset in range(1, len(valid_members) + 1):
        candidate = valid_members[(start + offset) % len(valid_members)]

        # 检查冷却时间
        from tunnel_watch import FAILED_NODE_COOLDOWN
        if candidate != current and now - failed.get(candidate, -float('inf')) >= FAILED_NODE_COOLDOWN:
            return candidate

    return None


if __name__ == '__main__':
    # 测试
    test_group = {
        'now': '香港-1',
        'all': ['剩余流量：929.27 GB', '香港-1', '香港-2', '台湾-1', 'DIRECT']
    }

    print("测试节点过滤:")
    for name in test_group['all']:
        print(f"  {name}: {'✅ 有效' if safe_node_name(name) else '❌ 跳过'}")

    print("\n下一个候选节点:")
    candidate = next_proxy_candidate_improved(test_group, {}, 0)
    print(f"  从 {test_group['now']} 切换到 {candidate}")
