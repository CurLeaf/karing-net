#!/usr/bin/env python3
"""
Karing 日常维护快捷命令
使用: python3 karing.py [命令]
"""
import sys
import subprocess
from pathlib import Path

PYTHON = Path('.vfox/sdks/python/bin/python3')

COMMANDS = {
    'status': {
        'desc': '查看系统状态',
        'cmd': f'{PYTHON} optimize.py'
    },
    'check': {
        'desc': '检查配置同步',
        'cmd': f'{PYTHON} sync_rules.py check'
    },
    'apply': {
        'desc': '应用配置修复',
        'cmd': f'{PYTHON} sync_rules.py apply'
    },
    'restart': {
        'desc': '重启后台服务',
        'cmd': f'{PYTHON} manage_services.py install'
    },
    'stop': {
        'desc': '停止后台服务',
        'cmd': f'{PYTHON} manage_services.py uninstall'
    },
    'logs': {
        'desc': '查看最近日志',
        'cmd': f'tail -50 ~/Library/Application\\ Support/karing-net/*.log'
    },
    'logs-live': {
        'desc': '实时查看日志',
        'cmd': f'tail -f ~/Library/Application\\ Support/karing-net/tunnel-watch.log'
    },
    'test': {
        'desc': '运行单元测试',
        'cmd': f'{PYTHON} -m unittest discover -s tests -v'
    },
    'test-network': {
        'desc': '网络验收测试',
        'cmd': f'{PYTHON} selfcheck.py --network'
    },
    'service-status': {
        'desc': '后台服务详细状态',
        'cmd': f'{PYTHON} manage_services.py status'
    },
}

def print_help():
    print("""
╔══════════════════════════════════════════════════════════════╗
║           Karing macOS 维护工具 - 快捷命令                   ║
╚══════════════════════════════════════════════════════════════╝

用法: python3 karing.py [命令]

可用命令:
""")
    for cmd, info in COMMANDS.items():
        print(f"  {cmd:15} - {info['desc']}")

    print("""
常用操作流程:

  1. 查看系统状态:
     python3 karing.py status

  2. 检查并修复配置:
     python3 karing.py check
     python3 karing.py apply

  3. 重启服务应用更新:
     python3 karing.py restart

  4. 查看日志:
     python3 karing.py logs
     python3 karing.py logs-live  (实时)

  5. 运行测试:
     python3 karing.py test
     python3 karing.py test-network

详细文档:
  - QUICK_START.md      快速入门
  - ARCHITECTURE.md     技术架构
  - OPTIMIZATION_REPORT.md  优化报告
""")

def run_command(cmd_name):
    if cmd_name not in COMMANDS:
        print(f"❌ 未知命令: {cmd_name}")
        print_help()
        return 1

    info = COMMANDS[cmd_name]
    print(f"🔧 执行: {info['desc']}")
    print(f"{'='*60}")

    result = subprocess.run(info['cmd'], shell=True)
    return result.returncode

def main():
    if len(sys.argv) < 2:
        print_help()
        return 0

    cmd = sys.argv[1]

    if cmd in ('help', '-h', '--help'):
        print_help()
        return 0

    return run_command(cmd)

if __name__ == '__main__':
    sys.exit(main())
