#!/usr/bin/env python3
"""
Karing 配置同步与修复工具
需要用户手动授权访问 Karing 数据目录
"""
import os
import sys
from pathlib import Path

def main():
    print("=" * 60)
    print("Karing macOS 配置修复工具")
    print("=" * 60)
    print()

    # 检查 Karing 数据目录权限
    karing_dir = Path.home() / "Library/Group Containers/group.com.nebula.karing"

    print(f"📁 Karing 数据目录: {karing_dir}")

    if not karing_dir.exists():
        print("❌ 错误: Karing 数据目录不存在")
        print("   请确保 Karing 已安装并至少运行过一次")
        return 1

    # 测试访问权限
    test_file = karing_dir / "service.json"

    try:
        with open(test_file, 'r') as f:
            f.read(1)
        print("✅ 权限检查通过")
        print()
    except PermissionError:
        print("❌ 权限错误: 无法访问 Karing 数据目录")
        print()
        print("🔧 解决方法:")
        print("1. macOS 系统设置 → 隐私与安全性 → 完全磁盘访问权限")
        print("2. 添加 'Terminal' 或你使用的终端应用")
        print("3. 或者直接运行:")
        print()
        print("   sudo .vfox/sdks/python/bin/python3 fix_karing.py")
        print()
        return 1

    # 检查配置文件
    print("🔍 检查配置状态...")
    print()

    # 导入并运行检查
    import karing_mac as km
    import sync_rules

    try:
        result = sync_rules.report()

        if result['source_ok'] and result['generated_ok']:
            print("✅ 配置完全同步，无需修复")
        else:
            print("⚠️  发现配置漂移:")
            if not result['source_ok']:
                print(f"   源文件漂移: {result['source_drift']}")
            if not result['generated_ok']:
                print(f"   生成文件漂移: {result['generated_drift']}")

            print()
            print("🔧 开始应用修复...")

            apply_result = sync_rules.apply()

            if apply_result['changed']:
                print(f"✅ 已修复: {apply_result['changed']}")
                print(f"📦 备份位置: {apply_result['backup']}")
                print()
                print("⚠️  重要: 请重新连接 Karing 以使配置生效")
            else:
                print("✅ 无需修改")

        print()
        print("📊 当前配置状态:")
        print(f"   源文件: {'✅ 正常' if result['source_ok'] else '❌ 漂移'}")
        print(f"   生成文件: {'✅ 正常' if result['generated_ok'] else '❌ 漂移'}")

    except Exception as e:
        print(f"❌ 错误: {e}")
        return 1

    print()
    print("=" * 60)
    print("修复完成！")
    print("=" * 60)

    return 0

if __name__ == '__main__':
    sys.exit(main())
