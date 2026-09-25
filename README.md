# karing-net

按平台独立组织，两个目录互不依赖：

- [linux/](linux/)：原有 Linux 脚本、配置示例、测试、工具及说明。
- [mac/](mac/)：macOS 独立目录，包含分流维护、连接回收、线路监控、launchd 服务和验收记录。

Linux 的命令请先进入 `linux/` 再执行。已有服务或脚本中指向旧位置的绝对路径，需要增加 `linux/` 这一层。
