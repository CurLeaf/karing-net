#!/usr/bin/env python3
"""
节点性能查看工具

使用方法：
  python3 node_stats.py              # 查看推荐节点
  python3 node_stats.py --all        # 查看所有节点统计
  python3 node_stats.py --node <name> # 查看特定节点详情
  python3 node_stats.py --summary    # 查看数据库摘要
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from node_tracker import NodePerformanceTracker
import karing_mac as km


def format_bytes(bytes_per_sec: int) -> str:
    """格式化字节速度"""
    if bytes_per_sec >= 1024 * 1024:
        return f'{bytes_per_sec / (1024 * 1024):.2f} MB/s'
    elif bytes_per_sec >= 1024:
        return f'{bytes_per_sec / 1024:.0f} KB/s'
    else:
        return f'{bytes_per_sec} B/s'


def format_latency(seconds: float) -> str:
    """格式化延迟"""
    return f'{seconds * 1000:.0f}ms'


def print_node_stats(node: str, stats: dict):
    """打印节点统计信息"""
    print(f'\n📍 {node}')
    print(f'  评分: {stats["score"]:.1f}/100')
    print(f'  平均延迟: {format_latency(stats["avg_latency"])}')
    print(f'  平均吞吐量: {format_bytes(stats["avg_throughput"])}')
    print(f'  成功率: {stats["success_rate"] * 100:.1f}%')
    print(f'  样本数: {stats["sample_count"]}')

    import time
    age = time.time() - stats['last_seen']
    if age < 60:
        age_str = f'{age:.0f}秒前'
    elif age < 3600:
        age_str = f'{age / 60:.0f}分钟前'
    elif age < 86400:
        age_str = f'{age / 3600:.1f}小时前'
    else:
        age_str = f'{age / 86400:.1f}天前'
    print(f'  最后检测: {age_str}')


def show_recommendations(tracker: NodePerformanceTracker, limit: int = 5):
    """显示推荐节点"""
    print('\n🌟 推荐节点 (按评分排序)')
    print('=' * 70)

    recommendations = tracker.get_recommendations(limit=limit)

    if not recommendations:
        print('\n暂无推荐节点（需要至少3个样本）')
        return

    for i, (node, stats) in enumerate(recommendations, 1):
        print(f'\n#{i}. {node}')
        print(f'    评分: {stats["score"]:.1f}/100')
        print(f'    延迟: {format_latency(stats["avg_latency"])}  '
              f'吞吐: {format_bytes(stats["avg_throughput"])}  '
              f'成功率: {stats["success_rate"] * 100:.0f}%')


def show_all_nodes(tracker: NodePerformanceTracker):
    """显示所有节点统计"""
    print('\n📊 所有节点统计')
    print('=' * 70)

    nodes = []
    for node in tracker.data['nodes']:
        stats = tracker.get_node_stats(node)
        if stats and stats['sample_count'] >= 1:
            nodes.append((node, stats))

    if not nodes:
        print('\n暂无节点数据')
        return

    # 按评分排序
    nodes.sort(key=lambda x: x[1]['score'], reverse=True)

    for node, stats in nodes:
        print_node_stats(node, stats)


def show_node_detail(tracker: NodePerformanceTracker, node: str):
    """显示特定节点详情"""
    stats = tracker.get_node_stats(node)

    if not stats:
        print(f'\n❌ 节点 "{node}" 无数据')
        return

    print(f'\n📍 节点详情: {node}')
    print('=' * 70)
    print_node_stats(node, stats)

    # 显示最近的样本
    node_data = tracker.data['nodes'].get(node)
    if node_data and node_data['samples']:
        print('\n  最近10次采样:')
        samples = node_data['samples'][-10:]
        for i, sample in enumerate(reversed(samples), 1):
            import time
            import datetime
            dt = datetime.datetime.fromtimestamp(sample['timestamp'])
            status = '✅' if sample['success'] else '❌'
            print(f'    {i}. {dt.strftime("%H:%M:%S")} '
                  f'{status} '
                  f'{format_latency(sample["latency"])} '
                  f'{format_bytes(sample["throughput"])}')


def show_summary(tracker: NodePerformanceTracker):
    """显示数据库摘要"""
    summary = tracker.get_summary()

    print('\n📈 数据库摘要')
    print('=' * 70)
    print(f'  总节点数: {summary["total_nodes"]}')
    print(f'  有效节点数: {summary["valid_nodes"]} (样本≥3)')
    print(f'  总样本数: {summary["total_samples"]}')
    print(f'  数据库大小: {summary["db_size_kb"]} KB')

    # 当前监控状态
    try:
        state = km.load_json(km.STATE_DIR / 'tunnel-watch.json')
        print(f'\n  监控状态:')
        print(f'    运行中: {"✅" if state.get("api_ok") else "❌"}')
        print(f'    当前ticks: {state.get("ticks", 0)}')

        selected = state.get('selected', {})
        if selected:
            print(f'    当前节点: {selected.get("urltest_out", "未知")}')

        throughput = state.get('throughput', {})
        if throughput:
            print(f'    最近吞吐量: {format_bytes(throughput.get("bytes_per_second", 0))} '
                  f'({"✅ 正常" if throughput.get("ok") else "⚠️ 慢速"})')
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='查看 Karing 节点性能统计')
    parser.add_argument('--all', action='store_true', help='显示所有节点')
    parser.add_argument('--node', type=str, help='显示特定节点详情')
    parser.add_argument('--summary', action='store_true', help='显示数据库摘要')
    parser.add_argument('--limit', type=int, default=5, help='推荐节点数量（默认5）')

    args = parser.parse_args()

    tracker = NodePerformanceTracker()

    if args.summary:
        show_summary(tracker)
    elif args.all:
        show_all_nodes(tracker)
    elif args.node:
        show_node_detail(tracker, args.node)
    else:
        show_recommendations(tracker, args.limit)

    print()


if __name__ == '__main__':
    main()
