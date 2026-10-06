"""
Node Performance Tracker - 节点性能数据库

记录和分析节点性能，提供智能推荐
跨平台通用版本（无依赖）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from collections import defaultdict
from typing import Optional


def atomic_json(path: Path, data: dict):
    """原子写入 JSON 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


class NodePerformanceTracker:
    """节点性能追踪器"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.home() / '.local/share/karing-net/node_performance.json'
        self.db_path = db_path
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
        atomic_json(self.db_path, self.data)

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
                'first_seen': time.time(),
                'last_seen': time.time()
            }

        node_data = self.data['nodes'][node]
        node_data['last_seen'] = time.time()

        # 保留最近 100 个样本
        samples = node_data['samples']
        samples.append({
            'timestamp': time.time(),
            'latency': round(latency, 3),
            'throughput': throughput,
            'success': success
        })

        if len(samples) > 100:
            node_data['samples'] = samples[-100:]

        self.save()

    def get_node_stats(self, node: str) -> dict | None:
        """获取节点统计信息"""
        if node not in self.data['nodes']:
            return None

        node_data = self.data['nodes'][node]
        samples = node_data['samples']

        if not samples:
            return None

        # 计算统计数据
        latencies = [s['latency'] for s in samples]
        throughputs = [s['throughput'] for s in samples]
        successes = sum(1 for s in samples if s['success'])

        return {
            'node': node,
            'sample_count': len(samples),
            'avg_latency': round(sum(latencies) / len(latencies), 3),
            'avg_throughput': round(sum(throughputs) / len(throughputs)),
            'success_rate': round(successes / len(samples), 2),
            'first_seen': node_data['first_seen'],
            'last_seen': node_data['last_seen']
        }

    def calculate_score(self, node: str) -> float:
        """
        计算节点综合评分（0-100）

        评分模型：
        - 延迟权重 30%：< 0.1s = 100 分，每增加 0.1s 减 10 分
        - 吞吐量权重 50%：> 2MB/s = 100 分，线性递减
        - 成功率权重 20%：直接映射到 0-100
        """
        stats = self.get_node_stats(node)
        if not stats:
            return 0.0

        # 延迟评分（0-100）
        latency_score = max(0, 100 - (stats['avg_latency'] - 0.1) * 100)

        # 吞吐量评分（0-100）
        throughput_mbps = stats['avg_throughput'] / (1024 * 1024)
        throughput_score = min(100, throughput_mbps / 2 * 100)

        # 成功率评分（0-100）
        success_score = stats['success_rate'] * 100

        # 加权总分
        total_score = (
            latency_score * 0.3 +
            throughput_score * 0.5 +
            success_score * 0.2
        )

        return round(total_score, 1)

    def get_recommendations(self, limit: int = 5) -> list[dict]:
        """
        获取推荐节点列表（按评分降序）

        排除条件：
        - 7 天未见的节点
        - 样本数 < 3 的节点
        """
        now = time.time()
        week_ago = now - 7 * 24 * 3600

        candidates = []
        for node in self.data['nodes']:
            node_data = self.data['nodes'][node]

            # 排除太久未见的节点
            if node_data['last_seen'] < week_ago:
                continue

            # 排除样本不足的节点
            if len(node_data['samples']) < 3:
                continue

            score = self.calculate_score(node)
            stats = self.get_node_stats(node)

            candidates.append({
                'node': node,
                'score': score,
                'avg_latency': stats['avg_latency'],
                'avg_throughput': stats['avg_throughput'],
                'success_rate': stats['success_rate'],
                'sample_count': stats['sample_count']
            })

        # 按评分降序排序
        candidates.sort(key=lambda x: x['score'], reverse=True)

        return candidates[:limit]

    def get_all_nodes(self) -> list[str]:
        """获取所有节点名称"""
        return list(self.data['nodes'].keys())

    def summary(self) -> dict:
        """数据库摘要"""
        total_nodes = len(self.data['nodes'])
        total_samples = sum(len(n['samples']) for n in self.data['nodes'].values())

        return {
            'total_nodes': total_nodes,
            'total_samples': total_samples,
            'db_path': str(self.db_path),
            'db_size_kb': round(self.db_path.stat().st_size / 1024, 2) if self.db_path.exists() else 0
        }
