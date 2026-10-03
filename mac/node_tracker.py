"""
Node Performance Tracker - 节点性能数据库

记录和分析节点性能，提供智能推荐
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from collections import defaultdict
from typing import Optional

import karing_mac as km


class NodePerformanceTracker:
    """节点性能追踪器"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or km.STATE_DIR / 'node_performance.json'
        self.data = self.load()

    def load(self) -> dict:
        """加载历史数据"""
        if not self.db_path.exists():
            return {'nodes': {}, 'version': 1}

        try:
            return json.loads(self.db_path.read_text())
        except Exception:
            return {'nodes': {}, 'version': 1}

    def save(self):
        """保存数据到磁盘"""
        km.atomic_json(self.db_path, self.data)

    def record_sample(self, node: str, latency: float, throughput: int,
                     success: bool = True):
        """
        记录一次性能采样

        Args:
            node: 节点名称
            latency: 延迟（秒）
            throughput: 吞吐量（bytes/sec）
            success: 是否成功
        """
        if node not in self.data['nodes']:
            self.data['nodes'][node] = {
                'samples': [],
                'total_samples': 0,
                'total_success': 0,
                'total_failure': 0,
                'first_seen': time.time(),
                'last_seen': time.time(),
            }

        node_data = self.data['nodes'][node]

        # 添加样本
        sample = {
            'timestamp': time.time(),
            'latency': round(latency, 3),
            'throughput': throughput,
            'success': success,
        }

        node_data['samples'].append(sample)
        node_data['total_samples'] += 1

        if success:
            node_data['total_success'] += 1
        else:
            node_data['total_failure'] += 1

        node_data['last_seen'] = time.time()

        # 只保留最近 100 个样本
        if len(node_data['samples']) > 100:
            node_data['samples'] = node_data['samples'][-100:]

        self.save()

    def get_node_stats(self, node: str) -> Optional[dict]:
        """
        获取节点统计信息

        Returns:
            {
                'avg_latency': float,
                'avg_throughput': int,
                'success_rate': float,
                'sample_count': int,
                'last_seen': float,
                'score': float,
            }
        """
        if node not in self.data['nodes']:
            return None

        node_data = self.data['nodes'][node]
        samples = node_data['samples']

        if not samples:
            return None

        # 只统计成功的样本
        success_samples = [s for s in samples if s['success']]

        if not success_samples:
            return {
                'avg_latency': float('inf'),
                'avg_throughput': 0,
                'success_rate': 0.0,
                'sample_count': len(samples),
                'last_seen': node_data['last_seen'],
                'score': 0.0,
            }

        # 计算平均值（最近 20 个样本）
        recent = success_samples[-20:]
        avg_latency = sum(s['latency'] for s in recent) / len(recent)
        avg_throughput = sum(s['throughput'] for s in recent) / len(recent)

        # 成功率
        total = node_data['total_samples']
        success = node_data['total_success']
        success_rate = success / total if total > 0 else 0.0

        # 计算评分（越高越好）
        # 延迟：< 0.1s = 100分，每增加0.1s减10分
        latency_score = max(0, 100 - (avg_latency - 0.1) * 100)

        # 吞吐量：> 2MB/s = 100分，线性递减
        throughput_score = min(100, (avg_throughput / (2 * 1024 * 1024)) * 100)

        # 成功率：直接映射到 0-100
        success_score = success_rate * 100

        # 加权评分
        score = (
            0.3 * latency_score +      # 延迟权重 30%
            0.5 * throughput_score +    # 吞吐量权重 50%
            0.2 * success_score         # 成功率权重 20%
        )

        return {
            'avg_latency': round(avg_latency, 3),
            'avg_throughput': int(avg_throughput),
            'success_rate': round(success_rate, 3),
            'sample_count': len(samples),
            'last_seen': node_data['last_seen'],
            'score': round(score, 1),
        }

    def get_recommendations(self, limit: int = 5, min_samples: int = 3) -> list[tuple[str, dict]]:
        """
        获取推荐节点列表

        Args:
            limit: 返回数量
            min_samples: 最少样本数

        Returns:
            [(节点名, 统计信息), ...]，按评分降序排列
        """
        ranked = []

        for node in self.data['nodes']:
            stats = self.get_node_stats(node)

            if not stats or stats['sample_count'] < min_samples:
                continue

            # 排除太久没见的节点（超过 7 天）
            age = time.time() - stats['last_seen']
            if age > 7 * 24 * 3600:
                continue

            ranked.append((node, stats))

        # 按评分排序
        ranked.sort(key=lambda x: x[1]['score'], reverse=True)

        return ranked[:limit]

    def get_summary(self) -> dict:
        """获取数据库摘要"""
        total_nodes = len(self.data['nodes'])
        total_samples = sum(n['total_samples'] for n in self.data['nodes'].values())

        # 有足够样本的节点数
        valid_nodes = sum(
            1 for n in self.data['nodes'].values()
            if len(n['samples']) >= 3
        )

        return {
            'total_nodes': total_nodes,
            'valid_nodes': valid_nodes,
            'total_samples': total_samples,
            'db_size_kb': self.db_path.stat().st_size // 1024 if self.db_path.exists() else 0,
        }

    def cleanup_old_data(self, days: int = 30):
        """清理超过指定天数的旧数据"""
        cutoff = time.time() - days * 24 * 3600

        for node, data in list(self.data['nodes'].items()):
            # 删除旧样本
            data['samples'] = [
                s for s in data['samples']
                if s['timestamp'] > cutoff
            ]

            # 如果节点完全没有样本了，删除节点
            if not data['samples'] and data['last_seen'] < cutoff:
                del self.data['nodes'][node]

        self.save()


if __name__ == '__main__':
    # 测试
    tracker = NodePerformanceTracker()

    # 模拟一些数据
    import random
    nodes = ['香港HK-HY2', '新加坡SG-HY2', '台湾-优化2-GPT', '韩国KR-HY2']

    for _ in range(20):
        node = random.choice(nodes)
        latency = random.uniform(0.1, 0.5)
        throughput = random.randint(500_000, 3_000_000)
        tracker.record_sample(node, latency, throughput)

    print('数据库摘要:')
    summary = tracker.get_summary()
    for k, v in summary.items():
        print(f'  {k}: {v}')

    print('\n推荐节点:')
    for node, stats in tracker.get_recommendations():
        print(f'  {node}')
        print(f'    评分: {stats["score"]:.1f}')
        print(f'    延迟: {stats["avg_latency"]*1000:.0f}ms')
        print(f'    吞吐: {stats["avg_throughput"]/1024:.0f} KB/s')
        print(f'    成功率: {stats["success_rate"]*100:.0f}%')
