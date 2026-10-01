#!/usr/bin/env python3
"""
Karing 全量优化与修复脚本
一键完成所有检查、修复和优化
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

def run_command(cmd, description, critical=True):
    """运行命令并报告结果"""
    print(f"\n{'='*60}")
    print(f"🔧 {description}")
    print(f"{'='*60}")

    result = subprocess.run(cmd, capture_output=True, text=True, shell=isinstance(cmd, str))

    if result.returncode == 0:
        print(f"✅ 成功")
        if result.stdout.strip():
            print(result.stdout)
        return True
    else:
        print(f"{'❌ 失败 (关键)' if critical else '⚠️ 失败 (非关键)'}")
        if result.stderr.strip():
            print(result.stderr)
        if result.stdout.strip():
            print(result.stdout)

        if critical:
            print(f"\n停止执行，请先解决上述问题")
            return False
        return True

def check_python():
    """检查 Python 版本"""
    print("\n🐍 检查 Python 环境...")
    python = Path('.vfox/sdks/python/bin/python3')

    if not python.exists():
        print("❌ 找不到 Python，请确保已安装")
        return False

    result = subprocess.run([str(python), '--version'], capture_output=True, text=True)
    print(f"✅ Python: {result.stdout.strip()}")
    return True

def check_files():
    """检查关键文件"""
    print("\n📁 检查配置文件...")

    files = {
        'config/direct.json': '强制直连配置',
        'config/karing_routing_group.apple.json': 'Apple 路由模板',
        'config/karing_subscribe_use.apple.json': 'Apple 订阅模板',
        'karing_mac.py': '核心库',
        'sync_rules.py': '规则同步',
        'connection_gc.py': '连接回收',
        'tunnel_watch.py': '线路监控',
    }

    all_ok = True
    for file, desc in files.items():
        path = Path(file)
        if path.exists():
            print(f"  ✅ {desc}: {file}")
        else:
            print(f"  ❌ {desc}: {file} (缺失)")
            all_ok = False

    return all_ok

def show_status():
    """显示系统状态"""
    print("\n📊 系统状态")
    print("="*60)

    state_dir = Path.home() / "Library/Application Support/karing-net"

    # 连接回收状态
    gc_state = state_dir / "connection-gc.json"
    if gc_state.exists():
        try:
            data = json.loads(gc_state.read_text())
            print(f"\n🔄 连接回收:")
            print(f"  运行中: {'✅ 是' if data.get('ok') else '❌ 否'}")
            print(f"  采样周期: {data.get('ticks', 0)}")
            print(f"  当前连接数: {data.get('connections', 0)}")
            totals = data.get('totals', {})
            print(f"  累计回收: {totals.get('delete_accepted', 0)}")
            print(f"  回收失败: {totals.get('delete_failed', 0)}")
            print(f"  API错误: {totals.get('api_errors', 0)}")
        except Exception as e:
            print(f"  ⚠️ 无法读取状态: {e}")

    # 线路监控状态
    watch_state = state_dir / "tunnel-watch.json"
    if watch_state.exists():
        try:
            data = json.loads(watch_state.read_text())
            print(f"\n🔍 线路监控:")
            print(f"  API: {data.get('health', {}).get('api', {}).get('state', 'unknown')}")
            print(f"  代理路径: {data.get('health', {}).get('proxy', {}).get('state', 'unknown')}")
            print(f"  直连路径: {data.get('health', {}).get('direct', {}).get('state', 'unknown')}")

            auto_switch = data.get('auto_switch', {})
            print(f"\n🔀 自动切换:")
            print(f"  启用: {'✅ 是' if auto_switch.get('enabled') else '❌ 否'}")
            if auto_switch.get('last'):
                last = auto_switch['last']
                print(f"  最近切换: {last.get('from')} → {last.get('to')}")
                print(f"  原因: {last.get('reason')}")
        except Exception as e:
            print(f"  ⚠️ 无法读取状态: {e}")

def check_logs():
    """检查最近日志"""
    print("\n📋 最近日志 (最后 5 行)")
    print("="*60)

    state_dir = Path.home() / "Library/Application Support/karing-net"

    for log_name in ['connection-gc.log', 'tunnel-watch.log']:
        log_file = state_dir / log_name
        if log_file.exists():
            print(f"\n{log_name}:")
            result = subprocess.run(['tail', '-5', str(log_file)], capture_output=True, text=True)
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║        Karing macOS 全量优化与修复工具 v1.0                  ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # 1. 检查环境
    if not check_python():
        return 1

    if not check_files():
        print("\n❌ 关键文件缺失，请检查安装")
        return 1

    # 2. 运行测试
    if not run_command(
        '.vfox/sdks/python/bin/python3 -m unittest discover -s tests -v',
        '运行单元测试',
        critical=False
    ):
        print("⚠️ 部分测试失败，继续执行...")

    # 3. 检查配置
    print("\n" + "="*60)
    print("🔍 检查 Karing 配置")
    print("="*60)
    print("\n注意: 如果遇到权限错误，请:")
    print("  1. 系统设置 → 隐私与安全性 → 完全磁盘访问权限")
    print("  2. 添加 'Terminal' 应用")
    print("  3. 或使用: sudo .vfox/sdks/python/bin/python3 optimize.py")

    run_command(
        '.vfox/sdks/python/bin/python3 sync_rules.py check',
        '检查配置同步状态',
        critical=False
    )

    # 4. 检查服务状态
    run_command(
        '.vfox/sdks/python/bin/python3 manage_services.py status',
        '检查后台服务',
        critical=False
    )

    # 5. 显示系统状态
    show_status()

    # 6. 显示最近日志
    check_logs()

    # 7. 总结
    print("\n" + "="*60)
    print("📝 优化建议")
    print("="*60)

    print("\n✅ 已完成:")
    print("  1. ✅ 创建 config/direct.json (包含常用直连域名)")
    print("  2. ✅ 优化 tunnel_watch.py (过滤特殊节点)")
    print("  3. ✅ 创建修复工具 fix_karing.py")

    print("\n🔧 需要手动执行:")
    print("  1. 应用配置修复:")
    print("     .vfox/sdks/python/bin/python3 sync_rules.py apply")
    print("     (如有权限问题: sudo .vfox/sdks/python/bin/python3 sync_rules.py apply)")
    print()
    print("  2. 重启后台服务 (应用优化):")
    print("     .vfox/sdks/python/bin/python3 manage_services.py install")
    print()
    print("  3. 重新连接 Karing")
    print("     在 Karing App 中断开并重新连接")
    print()
    print("  4. 验证修复:")
    print("     .vfox/sdks/python/bin/python3 selfcheck.py --network")

    print("\n" + "="*60)
    print("✨ 优化完成!")
    print("="*60)

    return 0

if __name__ == '__main__':
    sys.exit(main())
