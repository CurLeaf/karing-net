# 用户后台服务

在 `mac/` 中运行：

```bash
.vfox/sdks/python/bin/python3 manage_services.py install
.vfox/sdks/python/bin/python3 manage_services.py status
.vfox/sdks/python/bin/python3 manage_services.py stop
```

安装器使用 `plistlib` 写入当前路径，无需手工替换 XML，支持目录中含空格。`gc` 使用 `--apply --loop` 保留连续流量样本；`watch` 常驻运行。两者使用 `KeepAlive` 和 15 秒启动节流，业务日志由脚本内部轮转。模板中的占位符仅用于展示；实际安装始终由安装器生成。

这是登录用户的 LaunchAgent，未登录时不运行。授权 Python 访问 Karing 数据后可重新执行 `install`。更新源码后执行 `install` 以加载新版本。无需 root 权限，也不控制 Karing 本体的启停。
