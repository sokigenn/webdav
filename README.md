# WebDAV Downloader

WebDAV 文件浏览与批量下载工具，支持文件预览、多选下载、并发下载、断点续传。

---

## Windows 本地运行

### 前提条件

安装 **Python 3.12**（压缩包内已附带安装程序 `python-3.12.7-amd64.exe`，或[点此下载](https://www.python.org/downloads/)）

安装时勾选 **"Add Python to PATH"**，否则后续步骤会报错。

### 启动方法

双击项目目录里的 `start.bat`，脚本会自动完成：

1. 检测 / 安装 Python 3.12
2. 创建 Python 虚拟环境
3. 安装所需依赖
4. 启动后端服务并自动打开浏览器 `http://localhost:8765`

> 关闭命令行窗口即停止服务。

---

## macOS 本地运行

### 前提条件

需要 Python 3.10 或更高版本。未安装时可用 Homebrew 一行搞定：

```bash
brew install python@3.12
```

### 启动方法

**首次运行**：右键点击 `start.command` → **打开**（macOS 对下载文件有安全拦截，右键开一次后即解除）

之后直接**双击** `start.command` 即可，会自动打开终端并启动服务，浏览器自动跳转到 `http://localhost:8765`。

也可以在终端手动运行：

```bash
chmod +x start.sh
./start.sh
```

> 关闭终端窗口即停止服务。

---

## NAS 部署（Docker）

详细步骤见 [DEPLOY_NAS.md](DEPLOY_NAS.md)，简要流程如下：

### 前提条件

- NAS 为 **x86（Intel/AMD）架构**
- 电脑已安装并启动 **Docker Desktop**（[点此下载](https://www.docker.com/products/docker-desktop/)）

### 1. 构建镜像

打开 PowerShell 或 CMD：

```bat
cd "项目目录路径\webdav-downloader"
docker build -t webdav-downloader .
docker save webdav-downloader -o webdav-downloader.tar
```

### 2. 导入到 NAS

极空间管理界面 → Docker → 镜像 → 导入 → 选择 `webdav-downloader.tar`

### 3. 创建容器

| 配置项 | 值 |
|--------|-----|
| 镜像 | `webdav-downloader:latest` |
| 端口映射 | `8765` → `8765` |
| 目录挂载 | NAS下载目录 → `/downloads` |
| 环境变量 | `TZ=Asia/Shanghai` |
| 重启策略 | `unless-stopped` |

### 4. 访问

```
http://NAS的IP:8765
```

---

## 功能说明

- **文件浏览**：目录导航、面包屑路径、文件大小和修改时间
- **文件预览**：图片、视频（流式播放）、音频、PDF、文本/代码
- **批量下载**：多选文件，配置并发数（默认 5，最大 20）
- **断点续传**：下载中断后重新触发，自动从上次位置继续
- **下载进度**：整体进度条、速度、预计剩余时间、各文件状态
- **Hosts 覆盖**：连接设置里填写类似 `/etc/hosts` 的规则，绕过运营商 DNS 污染，无需修改系统文件
